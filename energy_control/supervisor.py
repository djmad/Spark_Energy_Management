"""One resident-run supervisor composing guard, actuator owners and policy.

Hardware-free by construction: every actuator, sensor and guard source comes
from injected, child-local factories. Nothing here installs services, stops
legacy writers or selects live adapters; those belong to the reviewed
handoff runbook and the operator's hardware grant.

Fault topology: one spawn-context abort event is shared by the GPU, CPU and
fan owners and is the independent guard's cancellation route. Guard abort,
supervisor death (missed heartbeat, owner EOF), an owner fault or a policy
abort all converge on it, and every owner then runs its own emergency action
(GPU 200-500 MHz, CPU hardware minimum, fan floor 12). Nothing resumes.
"""
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from math import isfinite
from multiprocessing import get_context
import os
import socket
import sys
from threading import RLock
from time import monotonic, monotonic_ns, sleep

from .broker import Config, config_fingerprint
from .gpu_owner_session import GpuOwnerSession
from .guard_host_source import guard_host_source
from .guard_ownership_process import GuardOwnershipProcess
from .limit_owner_process import LimitOwnerSession
from .host_sampler import AbortRouteHealthy
from .policy import PolicyInput, ProposedLimits, ShadowPolicy
from .process_http_transport import GuardHttpCancellation, ProcessHttpLlmTransport
from .request_gateway import OwnedRequestDispatcher
from .recorder import CommissioningRecorder
from .safety import Snapshot
from .temperature_slope import SlopeUnavailable
from .trial_plan import SERVICE_STAGE


def _unverified(identifier):
    return False  # HTTP-observed mode: server drain is never claimed.



MAX_POLICY_GAP_S = 3.0  # policy tick gap tolerated (guard remains the safety authority)

class GuardAbortReporter(GuardHttpCancellation):
    """Guard-child abort route: set the shared abort event and report why.

    The reason goes to stderr (the service journal); it never contains
    request content. Server drain is still never claimed (returns False).
    """

    def __call__(self, owned, reason):
        try:
            print(f"energy_control guard: abort: {reason} (owned={len(owned)})",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        return super().__call__(owned, reason)


class ResidentSupervisor:
    def __init__(self, recorder, config, *, gpu_factory, cpu_factory, fan_factory,
                 policy_thermal, driver_epoch, owner_epoch,
                 guard_source_factory=guard_host_source, guard_deadline_s=1.0,
                 gpu_refresh_s=None, clock=None):
        if type(recorder) is not CommissioningRecorder or type(config) is not Config:
            raise ValueError("supervisor recorder and validated configuration required")
        proposal = recorder.trial_proposal
        if not recorder.plan_written or proposal is None or proposal.stage == 0:
            raise ValueError("durable actuating trial plan required")
        if (config.gpu_max_mhz > proposal.gpu_max_mhz
                or config.gpu_entry_mhz > proposal.gpu_entry_mhz
                or config.cpu_fast_max_mhz > proposal.cpu_fast_max_mhz
                or config.cpu_slow_max_mhz > proposal.cpu_slow_max_mhz):
            raise ValueError("configuration exceeds the durable trial envelope")
        if ((proposal.config_digest is not None
             and proposal.config_digest != config_fingerprint(config))
                or (proposal.fan_min_state is not None
                    and config.fan_min_state < proposal.fan_min_state)):
            raise ValueError("configuration is not the one bound by the durable trial plan")
        if not callable(policy_thermal) or not hasattr(policy_thermal, "fan_sensor_healthy"):
            raise ValueError("policy-side thermal sampler with fan sensor health required")
        self.service = proposal.stage == SERVICE_STAGE
        self.recorder, self.config = recorder, config
        self._thermal = policy_thermal
        # Re-assert the GPU lock periodically: a GPU reset without a driver
        # reload is invisible to the driver epoch and silently drops the lock.
        if gpu_refresh_s is not None and (type(gpu_refresh_s) not in (int, float)
                                          or not 5 <= gpu_refresh_s <= 600):
            raise ValueError("GPU lock refresh must be 5..600 s")
        self._gpu_refresh_s = gpu_refresh_s
        self._clock = clock or monotonic
        # Status publishing while the service arms (start, settle, warm-up), so
        # the published telemetry never pauses (operator, 27 September 2026:
        # "we will never be blind"). Called with the latest raw readout.
        self.on_readout = None
        self._published_at = None
        self._gpu_applied_at = None
        self._creator = os.getpid()
        self.abort = get_context("spawn").Event()
        self._feeds = {kind: socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                       for kind in ("gpu", "cpu", "fan")}
        self.gpu = GpuOwnerSession(recorder, gpu_factory, self.abort, driver_epoch=driver_epoch,
                                   owner_epoch=owner_epoch, guard_evidence=self._feeds["gpu"][1],
                                   service=self.service)
        self.cpu = LimitOwnerSession("cpu", recorder, cpu_factory, self.abort,
                                     owner_epoch=owner_epoch + "-cpu",
                                     guard_evidence=self._feeds["cpu"][1])
        self.fan = LimitOwnerSession("fan", recorder, fan_factory, self.abort,
                                     owner_epoch=owner_epoch + "-fan",
                                     guard_evidence=self._feeds["fan"][1])
        self.policy = ShadowPolicy(config, gpu_evidence_mode="setter_monitor",
                                   gpu_setter_context=self.gpu.context)
        source = partial(guard_source_factory,
                         evidence_channel=self._feeds["gpu"][0], gpu_context=self.gpu.context,
                         cpu_evidence=(self._feeds["cpu"][0], self.cpu.context),
                         fan_evidence=(self._feeds["fan"][0], self.fan.context),
                         workload_healthy=AbortRouteHealthy(self.abort))
        self.guard = GuardOwnershipProcess(
            GuardAbortReporter(self.abort), _unverified, None, sample_factory=source,
            # Service guard: same fixed limits, no trial deadline or admission budget.
            # The first reply waits for the guard's own sampler to start.
            deadline_s=guard_deadline_s, reply_timeout_s=2.0, run_id=recorder.run_id,
            trial_proposal=None if self.service else proposal,
            gpu_evidence_mode="setter_monitor", gpu_setter_context=self.gpu.context,
            completion_mode="http_observed")
        self.applied = {"gpu": None, "cpu": None, "fan": None}
        self.state = "NEW"
        self.reasons = ()
        self._last_time = None
        # Serializes control ticks with owned-request entry (prefill protection).
        self._control = RLock()
        self._pending_prefill = False
        self.dispatcher = None
        # Policy-side grace for a transiently missing thermal frame; the
        # independent guard remains the safety authority.
        self._thermal_missing_since = None
        self._last_limits = None

    def update_config(self, config):
        """Apply an operator-committed configuration between control ticks.

        Only fields the running owners can honour change here (the broker
        refuses raising CPU maxima, GPU maximum or entry above start values);
        the policy keeps its PID/ramp state (``ShadowPolicy.update_config``).
        """
        with self._control:
            self.policy.update_config(config)
            self.config = config

    def attach_dispatcher(self, connection_factory, token_measure, *, request_deadline_s=180):
        """Owned LLM requests through the guard; call before start().

        Each request is registered with the guard, durably admitted, held
        until the GPU entry ceiling is applied and verified, then authorized
        by the guard. The shared abort event cancels every HTTP worker.
        Clients that bypass this dispatcher are unowned background load.
        """
        if self.state != "NEW" or self.dispatcher is not None:
            raise RuntimeError("dispatcher must be attached once before start")
        if self.service:
            raise RuntimeError("owned test loads run in commissioning mode, not in the service")
        transport = ProcessHttpLlmTransport(guard_cancelled=self.abort,
                                            connection_factory=connection_factory,
                                            deadline_s=request_deadline_s)
        self.dispatcher = OwnedRequestDispatcher(
            transport, self.recorder, guard_ownership=self.guard,
            terminal_timeout_s=request_deadline_s,
            trial_proposal=self.recorder.trial_proposal, token_measure=token_measure,
            prepare_entry=self._prepare_entry, completion_mode="http_observed",
            on_fault=lambda reason: self._fail("dispatcher: " + reason))
        return self.dispatcher

    @contextmanager
    def _prepare_entry(self, workload_id):
        """Apply and verify the entry ceiling before a new prefill starts.

        Held across the guard's start authorization, so the policy cannot
        raise the ceiling in between. The next tick re-arms the ramp.
        """
        with self._control:
            entry = self.config.gpu_entry_mhz
            verified = self.state == "RUNNING" and not self.abort.is_set()
            if verified and self.applied["gpu"] > entry:
                self.gpu.apply(minimum_mhz=200, maximum_mhz=entry)
                self.applied["gpu"] = entry
                self._gpu_applied_at = self._clock()
            proof = self.gpu.read()
            verified = verified and proof is not None and proof.requested_max_mhz <= entry
            self._pending_prefill = True
            yield verified

    def start(self):
        """Owners first, then the GPU entry ceiling and fan floor 12, then the guard."""
        if self.state != "NEW" or os.getpid() != self._creator:
            raise RuntimeError("supervisor cannot restart or migrate")
        self.state = "STARTING"
        try:
            for session in (self.gpu, self.cpu, self.fan):
                session.start()
            locked_ns = monotonic_ns()
            self.gpu.apply(minimum_mhz=200, maximum_mhz=self.config.gpu_entry_mhz)
            self.applied["gpu"] = self.config.gpu_entry_mhz
            self._gpu_applied_at = self._clock()
            self.fan.apply((12,))
            self.applied["fan"] = (12,)
            self._await_gpu_settled(locked_ns)
            proof = self.cpu.read()
            if proof is None:
                raise RuntimeError("CPU owner evidence unavailable at start")
            self.applied["cpu"] = proof.requested
            self.guard.start()
            for kind in self._feeds:
                self._feeds[kind][0].close()  # The guard child holds its own copies.
            if not self.guard.heartbeat(self.recorder.run_id):
                raise RuntimeError("guard refused the run binding")
            self.state = "RUNNING"
            if self.dispatcher is not None:
                self.dispatcher.arm_admission()  # Only after guard and entry ceiling.
        except BaseException:
            self._fail("supervisor start failed")
            raise

    def _await_gpu_settled(self, locked_ns, timeout_s=5.0):
        """Before arming the guard, wait (bounded) for a telemetry frame sampled
        after the entry lock whose measured GPU clock is under the ceiling.

        After a power cycle the GPU runs at vendor clocks until the first lock;
        the guard's strict "measured clock above hard envelope" check (meant to
        catch a lost lock) saw the pre-lock clock and aborted the first start
        (live cold boot, 27 September 2026). Samplers without raw readouts
        (test fakes) skip the wait. A clock that does not settle fails the
        start into the safe state, as before.
        """
        from .temperature_slope import SlopeUnavailable
        if not hasattr(self._thermal, "last_readout"):
            return
        limit = self.config.gpu_entry_mhz + 50
        deadline = monotonic() + timeout_s
        while True:
            try:
                self._thermal()
            except SlopeUnavailable:
                pass
            readout = self._thermal.last_readout
            self._publish_arming(readout)
            if (readout is not None and readout.start_mono_ns >= locked_ns
                    and readout.gpu.measured_mhz is not None
                    and 0 < readout.gpu.measured_mhz <= limit):
                return
            if monotonic() >= deadline:
                raise RuntimeError("GPU clock did not settle under the entry ceiling")
            sleep(0.05)

    def warm_up(self, timeout_s=2.0):
        """After start: wait (bounded, heartbeating) for a fresh policy snapshot.

        The policy sampler is not read during the multi-second start; its slope
        estimator needs a new two-frame sequence before the first tick.
        """
        from .temperature_slope import SlopeUnavailable
        deadline = monotonic() + timeout_s
        while True:
            if not self.guard.heartbeat(self.recorder.run_id):
                raise RuntimeError("guard heartbeat refused during warm-up")
            try:
                self._thermal()
                return
            except SlopeUnavailable:
                self._publish_arming(getattr(self._thermal, "last_readout", None))
                if monotonic() >= deadline:
                    raise
                sleep(0.05)

    def _publish_arming(self, readout):
        """Rate-limited (1 Hz) status while arming; never affects control."""
        hook = getattr(self, "on_readout", None)
        if hook is None or readout is None:
            return
        now = monotonic()
        last = getattr(self, "_published_at", None)
        if last is not None and now - last < 1.0:
            return
        self._published_at = now
        try:
            hook(readout)
        except Exception:
            pass

    def _fail(self, reason):
        if not self.reasons:
            self.reasons = (reason,)
        self.state = "ABORTED"
        self.abort.set()
        if self.dispatcher is not None:
            try:
                self.dispatcher.close_admission()
                self.dispatcher.cancel_owned_requests()
            except Exception:
                pass  # Workers also observe the shared abort event directly.
        try:
            self.recorder.write_event("abort")
        except Exception:
            pass  # Protection never waits for a healthy disk.

    def _snapshot(self):
        thermal = self._thermal()
        if type(thermal) is not Snapshot:
            raise RuntimeError("typed policy thermal snapshot required")
        gpu, cpu, fan = self.gpu.read(), self.cpu.read(), self.fan.read()
        return replace(
            thermal,
            gpu_requested_max_mhz=float("nan") if gpu is None else gpu.requested_max_mhz,
            gpu_accepted_max_mhz=None, gpu_limit_age_s=None, gpu_setter_evidence=gpu,
            gpu_actuator_healthy=gpu is not None, cpu_actuator_healthy=cpu is not None,
            fan_healthy=self._thermal.fan_sensor_healthy is True and fan is not None,
            workload_control_healthy=not self.abort.is_set())

    def tick(self, *, gpu_util_pct, cpu_util_pct=None, **signals) -> ProposedLimits:
        """One control cycle. Any failure aborts the run; nothing retries."""
        with self._control:
            return self._tick(gpu_util_pct, cpu_util_pct, signals)

    def _tick(self, gpu_util_pct, cpu_util_pct, signals):
        if self.state != "RUNNING" or os.getpid() != self._creator:
            raise RuntimeError("supervisor not running")
        if self._pending_prefill:
            signals = {**signals, "prefill_arrival": True}
            self._pending_prefill = False
        try:
            if self.abort.is_set():
                raise RuntimeError("shared abort latched")
            if not self.guard.heartbeat(self.recorder.run_id):
                raise RuntimeError("guard heartbeat refused")
            try:
                snapshot = self._snapshot()
            except SlopeUnavailable:
                now = monotonic()
                self._thermal_missing_since = self._thermal_missing_since or now
                if self._last_limits is None or now - self._thermal_missing_since > MAX_POLICY_GAP_S:
                    raise
                return self._last_limits  # Skip this tick: no actuator change.
            self._thermal_missing_since = None
            now = snapshot.monotonic_s
            elapsed = 0.25 if self._last_time is None else now - self._last_time
            # Up to MAX_POLICY_GAP_S between ticks is tolerated (slow frames
            # under heavy load, live 27 September 2026); actuators hold during a
            # gap and PIDs integrate at most 1 s. The independent guard keeps
            # its own 1 s sensor-age limits as the safety authority.
            if not isfinite(elapsed) or not 0 < elapsed <= MAX_POLICY_GAP_S:
                raise RuntimeError("policy thermal interval invalid")
            self._last_time = now
            limits = self.policy.step(PolicyInput(snapshot, gpu_util_pct, min(elapsed, 1.0),
                                                  cpu_util_pct=cpu_util_pct, **signals))
            if limits.abort_owned_loads:
                self._fail("policy abort: " + (limits.reasons[0] if limits.reasons else "unknown"))
                return limits
            self._apply(limits)
            self._last_limits = limits
            return limits
        except BaseException as exc:
            self._fail(f"control cycle failed: {type(exc).__name__}: {exc}"[:240])
            raise

    def _apply(self, limits):
        """Protective changes (lower clocks, more fan) before the others."""
        protective, other = [], []
        gpu = limits.gpu_max_mhz
        refresh = (self._gpu_refresh_s is not None and self._gpu_applied_at is not None
                   and self._clock() - self._gpu_applied_at >= self._gpu_refresh_s)
        if gpu != self.applied["gpu"] or refresh:
            step = ("gpu", gpu, lambda: self.gpu.apply(minimum_mhz=200, maximum_mhz=gpu))
            (protective if gpu <= self.applied["gpu"] else other).append(step)
        cpu = limits.cpu_clusters()  # (E0, P0, E1, P1)
        if cpu != self.applied["cpu"]:
            step = ("cpu", cpu, lambda: self.cpu.apply(cpu))
            lower = all(new <= old for new, old in zip(cpu, self.applied["cpu"]))
            (protective if lower else other).append(step)
        fan = (limits.fan_min_state,)
        if fan != self.applied["fan"]:
            step = ("fan", fan, lambda: self.fan.apply(fan))
            (protective if fan > self.applied["fan"] else other).append(step)
        for kind, value, action in protective + other:
            action()
            self.applied[kind] = value
            if kind == "gpu":
                self._gpu_applied_at = self._clock()

    def close(self):
        """Leave the known safe state: every owner runs its emergency action.

        Returns each component's exit code; None means still live and must be
        observed, never restarted or replaced by a second owner.
        """
        if os.getpid() != self._creator:
            raise RuntimeError("supervisor must be closed by its creator")
        if self.state not in ("NEW", "ABORTED"):
            self._fail("supervisor closed")
        self.abort.set()
        self.state = "CLOSED"
        if self.dispatcher is not None:
            self.dispatcher.join_workers(3)
        started_guard = self.guard._started
        self.guard.close()
        released = {kind: session.close() for kind, session in
                    (("gpu", self.gpu), ("cpu", self.cpu), ("fan", self.fan))}
        if started_guard:
            try:
                self.guard.join(3)
            except RuntimeError:
                pass
        for pair in self._feeds.values():
            for end in pair:
                end.close()
        return {"guard": self.guard.exitcode if started_guard else None,
                **{kind: (session.exitcode if released[kind] else None)
                   for kind, session in (("gpu", self.gpu), ("cpu", self.cpu), ("fan", self.fan))}}
