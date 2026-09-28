"""One serialized CPU/GPU/fan apply path; not a deployed service.

Injected adapters retain their durable-intent/readback responsibilities. This
coordinator never launches work, starts services or manages a RAM guard.
An independent process must still abort loads if any adapter or disk call hangs.
"""
from threading import Lock
from contextlib import contextmanager
from dataclasses import replace
from time import monotonic

from .broker import Config
from .cpu_actuation import GuardedCpuStep
from .fan import FanFloorStatus
from .gpu_command import SetterAttempt
from .gpu_evidence import GpuSetterEvidence, valid_context
from .policy import PolicyInput, ProposedLimits, ShadowPolicy
from .prefill_hold import PrefillHold
from .limits import GPU_HARD_MAX_MHZ


class UnifiedActuation:
    def __init__(self, config, cpu, gpu, fan, *, read_safety, verify_ownership,
                 record_candidate, gpu_context, clock=monotonic):
        if (type(config) is not Config or type(cpu) is not GuardedCpuStep
                or not valid_context(gpu_context)
                or not all(callable(f) for f in (read_safety, verify_ownership, record_candidate, clock))):
            raise ValueError("validated configuration and trusted local adapters required")
        self.config, self.cpu, self.gpu, self.fan = config, cpu, gpu, fan
        self._sample, self._owner = read_safety, verify_ownership
        self._record, self._context, self._clock = record_candidate, gpu_context, clock
        self._lock = Lock()
        self.faulted = False
        self._committed = None

    def _verify_committed_readback(self):
        if self._committed is None:
            return
        wanted = self._committed
        if self._owner() is not True:
            raise RuntimeError("committed actuator ownership lost")
        self.cpu._check_policies(self.cpu.adapter.readback(),
            {"slow": wanted.cpu_slow_max_mhz, "fast": wanted.cpu_fast_max_mhz})
        self._fan_floor(wanted.fan_min_state)
        self._gpu_proof(wanted.gpu_max_mhz)
        if self._owner() is not True:
            raise RuntimeError("ownership changed during readback")

    def _trip_locked(self, reason):
        if self.faulted:
            return
        self.faulted = True
        self.cpu.faulted = True
        self.cpu.abort.trip(reason)
        try:
            self.cpu.recorder.write_event("abort")
        except Exception:
            pass

    def abort(self, reason="unified control cycle failed"):
        with self._lock:
            self._trip_locked(reason)

    def _check(self):
        if self._owner() is not True:
            raise RuntimeError("unified actuator ownership lost")
        snapshot = self._sample()
        if self.cpu.abort.evaluate(snapshot).decision.abort:
            raise RuntimeError("independent safety boundary")
        return snapshot

    def _gpu_proof(self, wanted=None):
        proof = self.gpu.read()
        if (type(proof) is not GpuSetterEvidence
                or not proof.matches(proof.requested_max_mhz if wanted is None else wanted,
                                     self._clock(), self._context)):
            raise RuntimeError("GPU setter evidence missing or stale")
        return proof

    def _fan_floor(self, wanted=None):
        status = self.fan.read_floor()
        if (type(status) is not FanFloorStatus or status.maximum_state != 12
                or type(status.requested_minimum_state) is not int
                or not 0 <= status.requested_minimum_state <= 12
                or (wanted is not None and status.requested_minimum_state != wanted)):
            raise RuntimeError("fan floor readback mismatch")
        return status.requested_minimum_state

    def _cpu_apply(self, slow, fast):
        if self._owner() is not True:
            raise RuntimeError("unified actuator ownership lost")
        # GuardedCpuStep evaluates this fresh frame itself. Evaluating it here
        # as well would reuse a timestamp and correctly trip freshness checks.
        result = self.cpu.apply(self._sample(), slow_mhz=slow, fast_mhz=fast)
        if not result.applied:
            raise RuntimeError("CPU transaction failed")

    def apply(self, proposal):
        # This path is root-internal; it is not an API authorization mechanism.
        with self._lock:
            if self.faulted:
                raise RuntimeError("unified actuator fault is latched")
            try:
                c = self.config
                plan = self.cpu.trial_proposal
                if (type(proposal) is not ProposedLimits or proposal.hardware_qualified is not False
                        or proposal.abort_owned_loads is not False
                        or any(type(v) is not int for v in (proposal.gpu_max_mhz,
                            proposal.cpu_fast_max_mhz, proposal.cpu_slow_max_mhz, proposal.fan_min_state))
                        or not 500 <= proposal.gpu_max_mhz <= c.gpu_max_mhz <= GPU_HARD_MAX_MHZ
                        or not 1378 <= proposal.cpu_fast_max_mhz <= c.cpu_fast_max_mhz <= 3900
                        or not 338 <= proposal.cpu_slow_max_mhz <= c.cpu_slow_max_mhz <= 2808
                        or not max(c.fan_min_state, plan.fan_min_state) <= proposal.fan_min_state <= 12
                        or proposal.gpu_max_mhz > plan.gpu_max_mhz
                        or proposal.cpu_fast_max_mhz > plan.cpu_fast_max_mhz
                        or proposal.cpu_slow_max_mhz > plan.cpu_slow_max_mhz
                        or self.cpu.recorder.trial_proposal != plan
                        or self.cpu.recorder.plan_written is not True):
                    raise ValueError("proposal outside unified safety envelope")
                self._check()
                self._verify_committed_readback()
                proof = self._gpu_proof()
                floor = self._fan_floor()
                before = self.cpu.adapter.readback()
                self.cpu._check_policies(before)
                self._record(proposal)  # Durable candidate before any actuation.
                self._check()
                if proposal.fan_min_state > floor:
                    self.fan.set_minimum(proposal.fan_min_state)
                    self._fan_floor(proposal.fan_min_state)
                targets = {"slow": proposal.cpu_slow_max_mhz, "fast": proposal.cpu_fast_max_mhz}
                reduced = {kind: min(targets[kind], min(p.requested_max_khz // 1000
                            for p in before if p.cpu_class == kind)) for kind in targets}
                # CPU reductions precede any GPU increase. Mixed-class changes
                # are split so no class is raised during this first phase.
                if any(p.requested_max_khz != reduced[p.cpu_class] * 1000 for p in before):
                    self._cpu_apply(reduced["slow"], reduced["fast"])
                if proposal.gpu_max_mhz != proof.requested_max_mhz:
                    self._check()
                    result = self.gpu.apply(minimum_mhz=200, maximum_mhz=proposal.gpu_max_mhz)
                    if (type(result) is not SetterAttempt or result.status != "success"
                            or type(result.exit_code) is not int or result.exit_code != 0
                            or result.process_reaped is not True):
                        raise RuntimeError("GPU setter completion unverified")
                    self._gpu_proof(proposal.gpu_max_mhz)
                if targets != reduced:
                    self._cpu_apply(targets["slow"], targets["fast"])
                self._check()
                if proposal.fan_min_state < floor:
                    self.fan.set_minimum(proposal.fan_min_state)
                self._fan_floor(proposal.fan_min_state)
                self._gpu_proof(proposal.gpu_max_mhz)
                self.cpu._check_policies(self.cpu.adapter.readback(), targets)
                self._check()
                self._committed = proposal
            except BaseException:
                # Do not fight another actuator owner or reset hardware on error.
                self._trip_locked("unified CPU/GPU/fan transaction failed")
                raise


class UnifiedController:
    """Policy-to-actuator integration; caller supplies the qualified run harness.

    A tick is not workload admission. Startup limits must already be verified
    before the external manager is asked to load a model. No retry/reset here.
    """
    def __init__(self, policy, actuators, *, prefill_hold=None):
        if (type(policy) is not ShadowPolicy or type(actuators) is not UnifiedActuation
                or policy.config != actuators.config):
            raise ValueError("one matching policy and actuator configuration required")
        if prefill_hold is not None and type(prefill_hold) is not PrefillHold:
            raise ValueError("typed prefill execution hold required")
        self.policy, self.actuators = policy, actuators
        self.prefill_hold = prefill_hold
        self._lock = Lock()

    @contextmanager
    def prefill_entry(self, read_sample, *, workload_id=None):
        """Hold normal policy through bounded dispatch; guard stays independent.

        Acquire a fresh sample after obtaining the lock. Direct actuator writers
        must be fenced by the deployment; this lock only serializes this owner.
        Transport start must return after dispatch, not wait for generation.
        """
        with self._lock:
            try:
                sample = read_sample()
                if (type(sample) is not PolicyInput or self.actuators.faulted
                        or self.policy.config != self.actuators.config):
                    raise RuntimeError("prefill control state unavailable")
                if self.prefill_hold is not None:
                    self.prefill_hold.register(workload_id)
                proposal = self.policy.step(replace(sample, prefill_arrival=True))
                if (proposal.abort_owned_loads
                        or proposal.gpu_max_mhz > self.policy.config.gpu_entry_mhz):
                    raise RuntimeError("prefill entry proposal unsafe")
                self.actuators.apply(proposal)
                yield True
            except BaseException:
                self.actuators.abort("prefill entry transaction failed")
                raise

    def tick(self, sample):
        with self._lock:
            if self.actuators.faulted:
                raise RuntimeError("unified controller requires fault review")
            try:
                if self.policy.config != self.actuators.config:
                    raise RuntimeError("policy and actuator configuration diverged")
                if self.prefill_hold is not None and self.prefill_hold.pending(sample.safety.monotonic_s):
                    sample = replace(sample, prefill_arrival=True)
                proposal = self.policy.step(sample)
                if proposal.abort_owned_loads:
                    self.actuators.abort("unified policy requested workload abort")
                else:
                    self.actuators.apply(proposal)
                return proposal
            except BaseException:
                self.actuators.abort()
                raise
