"""Guarded commissioning CPU step; injected adapters only, not deployed.

This connects the independent abort guard and durable intent recorder to the
CPU maxima adapter. It does not establish exclusive hardware ownership or
qualify a live trial. No GPU/fan control is performed here.
"""

from dataclasses import dataclass
from typing import Protocol

from .abort import AbortCoordinator, AbortResult
from .cpu_frequency import CpuPolicy, LenovoGb10CpuMaxima
from .safety import Snapshot
from .trial_plan import TrialProposal, validate_trial_proposal


class CpuAdapter(Protocol):
    def readback(self) -> tuple[CpuPolicy, ...]: ...
    def set_maxima(self, *, slow_mhz: int, fast_mhz: int) -> tuple[CpuPolicy, ...]: ...


class CpuIntentRecorder(Protocol):
    plan_written: bool
    trial_proposal: TrialProposal | None
    def write_intent(self, kind: str, *, requested_mhz: float | None = None,
                     workload_id: str | None = None) -> int: ...
    def write_outcome(self, intent_seq: int, *, accepted_mhz: float | None = None,
                      measured_mhz: float | None = None, verified: bool): ...
    def write_event(self, kind: str) -> int: ...


@dataclass(frozen=True)
class CpuStepResult:
    applied: bool
    policies: tuple[CpuPolicy, ...]
    abort: AbortResult | None


class GuardedCpuStep:
    """A fault poisons this step permanently; a new qualified run is required."""

    def __init__(self, adapter: CpuAdapter, recorder: CpuIntentRecorder,
                 abort: AbortCoordinator, *, verify_ownership):
        proposal = getattr(recorder, "trial_proposal", None)
        if getattr(recorder, "plan_written", None) is not True:
            raise ValueError("durable trial plan required for CPU commissioning")
        validate_trial_proposal(proposal)
        if proposal.stage == 0:
            raise ValueError("read-only stage cannot actuate CPU")
        if not callable(verify_ownership):
            raise ValueError("independent CPU ownership verifier required")
        self.adapter = adapter
        self.recorder = recorder
        self.abort = abort
        self.trial_proposal = proposal
        self.faulted = False
        self._verify_ownership = verify_ownership

    def _check_ownership(self):
        # The verifier must bind the live lease, fenced prior writer and fresh
        # supervisor health. A sysfs readback alone is not ownership evidence.
        if self._verify_ownership() is not True:
            raise RuntimeError("CPU actuator ownership unavailable")

    def _fault(self, reason: str) -> CpuStepResult:
        self.faulted = True
        result = self.abort.trip(reason)
        try:
            self.recorder.write_event("abort")
        except Exception:
            pass  # Workload protection is not contingent on a healthy disk.
        return CpuStepResult(False, (), result)

    @staticmethod
    def _check_policies(policies, wanted=None):
        if (type(policies) is not tuple or len(policies) != 20
                or not all(isinstance(p, CpuPolicy) for p in policies)):
            raise ValueError("incomplete CPU policy readback")
        if sum(p.cpu_class == "slow" for p in policies) != 10 or sum(
                p.cpu_class == "fast" for p in policies) != 10:
            raise ValueError("unexpected CPU policy classes")
        if {p.name for p in policies} != {f"policy{index}" for index in range(20)}:
            raise ValueError("unexpected CPU policy identity")
        for policy in policies:
            if ((policy.hardware_min_khz, policy.hardware_max_khz)
                    != LenovoGb10CpuMaxima.BOUNDS[policy.cpu_class]
                    or policy.requested_min_khz != policy.hardware_min_khz
                    or not policy.requested_min_khz <= policy.requested_max_khz
                           <= policy.hardware_max_khz
                    or policy.governor != "conservative"
                    or (wanted is not None
                        and policy.requested_max_khz != wanted[policy.cpu_class] * 1000)):
                raise ValueError("CPU policy readback mismatch")

    def apply(self, snapshot: Snapshot, *, slow_mhz: int, fast_mhz: int) -> CpuStepResult:
        if self.faulted:
            return self._fault("CPU step already faulted")
        safety = self.abort.evaluate(snapshot)
        if safety.decision.abort:
            self.faulted = True
            return CpuStepResult(False, (), safety)
        try:
            self._check_ownership()
            if (type(slow_mhz) is not int or type(fast_mhz) is not int
                    or not 338 <= slow_mhz <= 2808
                    or not 1378 <= fast_mhz <= 3900):
                raise ValueError("CPU maxima outside pinned Lenovo envelope")
            if (self.recorder.plan_written is not True
                    or self.recorder.trial_proposal != self.trial_proposal
                    or slow_mhz > self.trial_proposal.cpu_slow_max_mhz
                    or fast_mhz > self.trial_proposal.cpu_fast_max_mhz):
                raise ValueError("CPU maxima exceed durable trial plan")
            before = self.adapter.readback()
            self._check_policies(before)
            wanted = {"slow": slow_mhz, "fast": fast_mhz}
            intents = []
            # An upward change is never sent before its own synced intent.
            # The recorder contract returns only after fdatasync succeeds.
            for cpu_class in ("slow", "fast"):
                target = wanted[cpu_class]
                if any(p.cpu_class == cpu_class and p.requested_max_khz < target * 1000
                       for p in before):
                    intents.append((cpu_class, self.recorder.write_intent(
                        f"raise_cpu_{cpu_class}_cap", requested_mhz=target)))
            self._check_ownership()  # Recheck after potentially slow intent sync.
            reported = self.adapter.set_maxima(slow_mhz=slow_mhz, fast_mhz=fast_mhz)
            self._check_policies(reported, wanted)
            after = self.adapter.readback()
            self._check_policies(after, wanted)
            identity = lambda policies: {(p.name, p.cpu_class, p.hardware_min_khz,
                                          p.hardware_max_khz) for p in policies}
            if identity(after) != identity(before):
                raise ValueError("CPU policy identity changed during actuation")
            self._check_ownership()
            for cpu_class, sequence in intents:
                self.recorder.write_outcome(sequence, accepted_mhz=wanted[cpu_class],
                                            measured_mhz=None, verified=True)
            self._check_ownership()
        except Exception:
            return self._fault("CPU actuation or durable intent failed")
        return CpuStepResult(True, after, None)
