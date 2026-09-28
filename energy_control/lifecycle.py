"""Pure commissioning lifecycle gate; fake-tested, not hardware-qualified.

The root-side harness must supply authoritative boot/driver/owner identities,
durable-run state and GPU evidence matching the guard's locally fixed mode. API claims are never
acceptable substitutes. This module performs no device or workload I/O except
through injected admission and abort adapters.
"""

from dataclasses import dataclass
from math import isfinite
import re
from time import monotonic
from typing import Protocol

from .abort import AbortCoordinator, AbortResult
from .run_catalog import RunCatalogResult
from .safety import Snapshot
from .trial_plan import TrialProposal
from .gpu_evidence import GpuSetterEvidence
from .limits import GPU_HARD_MAX_MHZ


MAX_PREFLIGHT_SAMPLE_AGE_S = 0.5
MAX_GPU_PROOF_AGE_S = 0.5


class AdmissionControl(Protocol):
    def arm_admission(self) -> None: ...
    def close_admission(self) -> None: ...


@dataclass(frozen=True)
class GpuLimitReading:
    """Numeric effective-limit result from an independently trusted reader."""

    accepted_max_mhz: float
    observed_monotonic_s: float
    boot_id: str
    driver_epoch: str
    owner_epoch: str


class NumericGpuLimitReader(Protocol):
    def read(self) -> GpuLimitReading | GpuSetterEvidence | None: ...


class IndependentGuardHeartbeat(Protocol):
    def heartbeat(self, run_id: str) -> bool: ...
    def verify_trial(self, run_id: str, proposal: TrialProposal) -> bool: ...


@dataclass(frozen=True)
class LifecycleResult:
    state: str
    armed: bool
    reasons: tuple[str, ...]
    abort: AbortResult | None = None


class CommissioningLifecycle:
    """BOOTSTRAP -> ARMED -> FAULT, with no automatic rearm/reset method."""

    def __init__(self, admission: AdmissionControl, abort: AbortCoordinator,
                 *, clock=monotonic, gpu_limit_reader: NumericGpuLimitReader | None = None,
                 independent_guard: IndependentGuardHeartbeat | None = None):
        self.admission = admission
        self.abort = abort
        self.clock = clock
        self.gpu_limit_reader = gpu_limit_reader
        self.independent_guard = independent_guard
        self.state = "BOOTSTRAP"
        self._identity: tuple[str, str, str] | None = None
        self._run_id: str | None = None

    @property
    def armed_identity(self) -> tuple[str, str, str] | None:
        return self._identity if self.state == "ARMED" else None

    def _fresh(self, snapshot: Snapshot) -> bool:
        try:
            now = self.clock()
        except Exception:
            return False
        if (not isinstance(snapshot, Snapshot) or type(now) not in (int, float)
                or not isfinite(now) or type(snapshot.monotonic_s) not in (int, float)
                or not isfinite(snapshot.monotonic_s)):
            return False
        return 0 <= now - snapshot.monotonic_s <= MAX_PREFLIGHT_SAMPLE_AGE_S

    def _trip(self, reason: str) -> LifecycleResult:
        self.state = "FAULT"
        try:
            self.admission.close_admission()
        except Exception:
            pass
        result = self.abort.trip(reason)
        return LifecycleResult("FAULT", False, result.decision.reasons, result)

    def _gpu_limit_verified(self, snapshot: Snapshot,
                                    identity: tuple[str, str, str], run_id: str) -> bool:
        if self.gpu_limit_reader is None or not isinstance(snapshot, Snapshot):
            return False
        try:
            reading = self.gpu_limit_reader.read()
            now = self.clock()
        except Exception:
            return False
        if self.abort.guard.gpu_evidence_mode == "setter_monitor":
            context = (*identity, run_id)
            return (type(reading) is GpuSetterEvidence
                    and context == self.abort.guard.gpu_setter_context
                    and reading == snapshot.gpu_setter_evidence
                    and reading.matches(snapshot.gpu_requested_max_mhz, now, context)
                    and reading.ownership_checked_monotonic_s <= snapshot.monotonic_s
                    and snapshot.gpu_accepted_max_mhz is None)
        if (not isinstance(reading, GpuLimitReading)
                or type(reading.accepted_max_mhz) not in (int, float)
                or not isfinite(reading.accepted_max_mhz)
                or not 0 < reading.accepted_max_mhz <= GPU_HARD_MAX_MHZ
                or type(reading.observed_monotonic_s) not in (int, float)
                or not isfinite(reading.observed_monotonic_s)
                or type(now) not in (int, float) or not isfinite(now)
                or not 0 <= now - reading.observed_monotonic_s <= MAX_GPU_PROOF_AGE_S
                or reading.observed_monotonic_s > snapshot.monotonic_s
                or (reading.boot_id, reading.driver_epoch, reading.owner_epoch) != identity
                or snapshot.gpu_accepted_max_mhz != reading.accepted_max_mhz):
            return False
        return True

    def _independent_guard_ready(self, run_id: str | None) -> bool:
        if (self.independent_guard is None or type(run_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", run_id) is None):
            return False
        try:
            return self.independent_guard.heartbeat(run_id) is True
        except Exception:
            return False

    def arm(self, snapshot: Snapshot, *, boot_id: str, driver_epoch: str,
            owner_epoch: str, run_id: str, previous_run: RunCatalogResult,
            durable_log_ready: bool,
            sensor_latency_qualified: bool = False) -> LifecycleResult:
        if self.state != "BOOTSTRAP":
            return LifecycleResult(self.state, False, ("lifecycle cannot rearm",))
        if (not isinstance(previous_run, RunCatalogResult)
                or previous_run.previous_run not in ("none", "clean")
                or previous_run.unreviewed_run_ids or previous_run.reasons):
            return self._trip("prior commissioning run is incomplete or unknown")
        if (not all(isinstance(value, str) and 1 <= len(value) <= 128
                    for value in (boot_id, driver_epoch, owner_epoch))
                or durable_log_ready is not True):
            return LifecycleResult("BOOTSTRAP", False,
                                   ("ownership or log unverified",))
        if sensor_latency_qualified is not True:
            return LifecycleResult("BOOTSTRAP", False,
                                   ("critical sensor latency unqualified",))
        if not self._independent_guard_ready(run_id):
            return LifecycleResult("BOOTSTRAP", False,
                                   ("independent guard unavailable",))
        if not self._fresh(snapshot):
            return LifecycleResult("BOOTSTRAP", False, ("preflight telemetry stale",))
        if not self._gpu_limit_verified(snapshot,
                                                (boot_id, driver_epoch, owner_epoch), run_id):
            return LifecycleResult("BOOTSTRAP", False,
                                   (("GPU setter evidence unverified" if self.abort.guard.gpu_evidence_mode
                                     == "setter_monitor" else "numeric GPU limit unverified"),))
        candidate = self.abort.guard.clone_unlatched().evaluate(snapshot)
        if candidate.abort:
            return LifecycleResult("BOOTSTRAP", False, candidate.reasons)
        # Only after the stateless preflight passes do we start the persistent
        # run guard. Bootstrap telemetry is allowed to be unavailable before
        # arming, without poisoning a future supervised run.
        monitored = self.abort.evaluate(snapshot)
        if monitored.decision.abort:
            self.state = "FAULT"
            self.admission.close_admission()
            return LifecycleResult("FAULT", False, monitored.decision.reasons, monitored)
        try:
            self.admission.arm_admission()
        except Exception:
            return self._trip("admission arming failed")
        self._identity = (boot_id, driver_epoch, owner_epoch)
        self._run_id = run_id
        self.state = "ARMED"
        return LifecycleResult("ARMED", True, ())

    def observe(self, snapshot: Snapshot, *, boot_id: str, driver_epoch: str,
                owner_epoch: str) -> LifecycleResult:
        if self.state != "ARMED":
            return LifecycleResult(self.state, False, ("lifecycle not armed",))
        if (boot_id, driver_epoch, owner_epoch) != self._identity:
            return self._trip("boot, driver or actuator owner changed")
        if not self._independent_guard_ready(self._run_id):
            return self._trip("independent guard heartbeat lost")
        if not self._fresh(snapshot):
            return self._trip("critical telemetry timeline stale")
        if not self._gpu_limit_verified(snapshot, self._identity, self._run_id):
            return self._trip("GPU setter evidence lost or stale" if self.abort.guard.gpu_evidence_mode
                              == "setter_monitor" else "numeric GPU limit lost or stale")
        result = self.abort.evaluate(snapshot)
        if result.decision.abort:
            self.state = "FAULT"
            self.admission.close_admission()
            return LifecycleResult("FAULT", False, result.decision.reasons, result)
        return LifecycleResult("ARMED", True, ())

    def resume_or_reset(self) -> LifecycleResult:
        """A signalled resume/reset invalidates all prior clock evidence."""
        return self._trip("resume or driver reset requires a new qualified run")

    def trip_external(self, reason: str) -> LifecycleResult:
        """Trusted local guard worker fault; never exposed as an API operation."""
        return self._trip(reason)
