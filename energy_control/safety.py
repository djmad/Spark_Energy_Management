"""Pure, fail-closed commissioning guard. No device or process I/O.

This is an independent safety decision primitive, not a complete test abort:
the commissioning harness must terminate and verify every owned workload when
``abort`` is true. Hardware/firmware protection remains authoritative.
"""

from dataclasses import dataclass
from math import isfinite
from .gpu_evidence import GpuSetterEvidence, valid_context
from .limits import ACPI_ABORT_C, GPU_HARD_MAX_MHZ


# Fixed commissioning envelope. A mutable PID policy or API request cannot
# increase these limits. Tightening them in a future hardware qualification is OK.
MAX_GPU_MHZ = float(GPU_HARD_MAX_MHZ)
ABORT_C = ACPI_ABORT_C  # Any ACPI zone / CPU proxy (limits.py; 96 C since 27 September 2026).
GPU_ABORT_C = 85.0  # ``temperature.gpu`` (sensor name "gpu").
MAX_SENSOR_AGE_S = 1.0
MAX_ACTUATOR_AGE_S = 1.0
MIN_AVAILABLE_MEMORY_BYTES = 12 * 1024**3
PREDICTION_S = 2.0
# The slope projection applies only within this band below each fixed limit.
# Far below it a 2 s projection cannot reach the limit at physical rise rates,
# and sensor steps would only cause false aborts. The hard limits are unchanged.
PREDICTION_BAND_C = 10.0
# A projected breach must persist this long before it aborts. P-cluster zones
# spike by ~9 C/s for a fraction of a second: a single projected sample aborted
# a sustained run at raw 79.9 C (live, 27 September 2026, TS1P). Measured real
# rises are <= ~5 C/s, so 1 s from the 83 C band start stays below 93 C; the
# raw hard limits still abort on the first sample.
PREDICTION_CONFIRM_S = 1.0
# Within this distance of the limit a projected breach still aborts at once.
PREDICTION_IMMEDIATE_C = 3.0
# After a setter reduction the measured clock needs a moment to follow the new
# lock; a lost lock (e.g. GPU reset) stays above the request far longer.
# Counted from the clock *sample* time. Under full load a lock reduction took
# > 1.5 s to show in the measured clock (live); a lost lock still trips the
# strict 1800 MHz hard check at once.
CLOCK_SETTLE_S = 5.0
# Firmware object identity only; no component mapping or response calibration
# is inferred from these ACPI names.
LENOVO_REQUIRED_TEMPERATURES = frozenset({
    "gpu", "acpi_tsoc", "acpi_ts0e", "acpi_ts0p", "acpi_ts1e",
    "acpi_ts1p", "acpi_tgpu", "acpi_tunc",
})


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def abort_limit_c(name: str) -> float:
    """Fixed per-sensor abort limit; configuration may never raise it."""
    return GPU_ABORT_C if name.lower() == "gpu" else ABORT_C


@dataclass(frozen=True)
class Temperature:
    name: str
    celsius: float
    age_s: float
    rising_c_per_s: float = 0.0
    # Least-squares trend value at the newest sample (None: use celsius). The
    # prediction starts here so a single sensor spike is not projected; the
    # hard limit always applies to the raw celsius value.
    trend_c: float | None = None


@dataclass(frozen=True)
class Snapshot:
    temperatures: tuple[Temperature, ...]
    gpu_requested_max_mhz: float
    gpu_accepted_max_mhz: float | None
    gpu_limit_age_s: float | None
    gpu_measured_mhz: float | None
    gpu_clock_age_s: float
    available_memory_bytes: int
    fan_healthy: bool
    cpu_actuator_healthy: bool
    gpu_actuator_healthy: bool
    workload_control_healthy: bool
    monotonic_s: float
    gpu_setter_evidence: GpuSetterEvidence | None = None


@dataclass(frozen=True)
class Decision:
    abort: bool
    reasons: tuple[str, ...]


class CommissioningGuard:
    """Latches any unsafe observation; intentionally has no remote reset method."""

    def __init__(self, *, required_temperature_names=LENOVO_REQUIRED_TEMPERATURES,
                 gpu_evidence_mode="numeric_readback", gpu_setter_context=None):
        if (gpu_evidence_mode not in ("numeric_readback", "setter_monitor")
                or (gpu_evidence_mode == "setter_monitor" and not valid_context(gpu_setter_context))
                or (gpu_evidence_mode == "numeric_readback" and gpu_setter_context is not None)):
            raise ValueError("explicit trusted GPU evidence mode/context required")
        self._gpu_evidence_mode = gpu_evidence_mode
        self._gpu_setter_context = gpu_setter_context
        if (type(required_temperature_names) is not frozenset
                or not 2 <= len(required_temperature_names) <= 32
                or any(not isinstance(name, str) or not 1 <= len(name) <= 32
                       or not all(char.isalnum() or char in "-_." for char in name)
                       for name in required_temperature_names)):
            raise ValueError("invalid required temperature identities")
        normalized = frozenset(name.lower() for name in required_temperature_names)
        if (len(normalized) != len(required_temperature_names) or "gpu" not in normalized
                or not any(name.startswith(("acpi", "cpu")) for name in normalized)):
            raise ValueError("GPU and CPU-proxy temperature identities required")
        self.required_temperature_names = normalized
        self._latched_reasons: tuple[str, ...] = ()
        self._last_monotonic_s: float | None = None
        self._projected_since: dict[str, float] = {}

    def clone_unlatched(self):
        """Use the same immutable sensor profile for stateless preflight."""
        return CommissioningGuard(required_temperature_names=self.required_temperature_names,
                                  gpu_evidence_mode=self._gpu_evidence_mode,
                                  gpu_setter_context=self._gpu_setter_context)

    @property
    def gpu_evidence_mode(self):
        return self._gpu_evidence_mode

    @property
    def gpu_setter_context(self):
        return self._gpu_setter_context

    def trip(self, reason: str) -> Decision:
        """Latch an internal non-telemetry fault without a synthetic snapshot."""
        if not isinstance(reason, str) or not 1 <= len(reason) <= 120:
            raise ValueError("invalid guard fault reason")
        if not self._latched_reasons:
            self._latched_reasons = (reason,)
        return Decision(True, self._latched_reasons)

    def evaluate(self, snapshot: Snapshot) -> Decision:
        if not isinstance(snapshot, Snapshot):
            if not self._latched_reasons:
                self._latched_reasons = ("invalid safety snapshot",)
            return Decision(True, self._latched_reasons)
        reasons: list[str] = []
        if not _finite(snapshot.monotonic_s) or snapshot.monotonic_s < 0:
            reasons.append("invalid monotonic time")
        elif (self._last_monotonic_s is not None
              and snapshot.monotonic_s <= self._last_monotonic_s):
            reasons.append("stalled or reversed telemetry time")
        else:
            self._last_monotonic_s = snapshot.monotonic_s

        if (type(snapshot.temperatures) is not tuple
                or not snapshot.temperatures
                or not all(isinstance(sensor, Temperature) for sensor in snapshot.temperatures)):
            reasons.append("missing critical temperatures")
            temperatures = ()
        else:
            temperatures = snapshot.temperatures
            names = {sensor.name.lower() for sensor in snapshot.temperatures
                     if isinstance(sensor.name, str)}
            if (len(names) != len(temperatures)
                    or not self.required_temperature_names.issubset(names)):
                reasons.append("missing or duplicate pinned critical temperature")
        for sensor in temperatures:
            if not isinstance(sensor.name, str) or not sensor.name or not all(_finite(x) for x in
                (sensor.celsius, sensor.age_s, sensor.rising_c_per_s)):
                reasons.append("invalid critical temperature")
                continue
            if sensor.age_s < 0 or sensor.age_s > MAX_SENSOR_AGE_S:
                reasons.append(f"stale critical temperature: {sensor.name}")
            if not -10 < sensor.celsius < 150:
                reasons.append(f"implausible critical temperature: {sensor.name}")
            elif sensor.celsius >= abort_limit_c(sensor.name):
                reasons.append(f"temperature at abort boundary: {sensor.name} "
                               f"{sensor.celsius:.1f} C")
            else:
                basis = sensor.celsius
                if _finite(sensor.trend_c) and -10 < sensor.trend_c < 150:
                    basis = sensor.trend_c
                projected = (basis >= abort_limit_c(sensor.name) - PREDICTION_BAND_C
                             and basis + PREDICTION_S * max(0, sensor.rising_c_per_s)
                             >= abort_limit_c(sensor.name))
                now = snapshot.monotonic_s if _finite(snapshot.monotonic_s) else None
                if not projected or now is None:
                    self._projected_since.pop(sensor.name, None)
                else:
                    since = self._projected_since.setdefault(sensor.name, now)
                    if (now - since >= PREDICTION_CONFIRM_S
                            or basis >= abort_limit_c(sensor.name) - PREDICTION_IMMEDIATE_C):
                        reasons.append(f"projected temperature breach: {sensor.name} "
                                       f"raw {sensor.celsius:.1f} C trend {basis:.1f} C "
                                       f"{sensor.rising_c_per_s:+.1f} C/s for "
                                       f"{now - since:.1f} s")

        requested = snapshot.gpu_requested_max_mhz
        accepted = snapshot.gpu_accepted_max_mhz
        if not _finite(requested) or requested <= 0 or requested > MAX_GPU_MHZ:
            reasons.append("GPU requested limit outside hard envelope")
        setter_mode = self._gpu_evidence_mode == "setter_monitor"
        if setter_mode:
            evidence = snapshot.gpu_setter_evidence
            if (type(evidence) is not GpuSetterEvidence or
                    not evidence.matches(requested, snapshot.monotonic_s, self._gpu_setter_context)):
                reasons.append("GPU setter evidence missing, stale or mismatched")
            if accepted is not None:
                reasons.append("setter mode must not claim numeric GPU readback")
        elif not _finite(accepted) or accepted <= 0 or accepted > MAX_GPU_MHZ:
            reasons.append("GPU accepted limit unverified or outside hard envelope")
        elif _finite(requested) and 0 < requested <= MAX_GPU_MHZ and accepted > requested:
            reasons.append("GPU accepted limit above requested limit")
        if not setter_mode and (not _finite(snapshot.gpu_limit_age_s) or snapshot.gpu_limit_age_s < 0
                or snapshot.gpu_limit_age_s > MAX_ACTUATOR_AGE_S):
            reasons.append("stale GPU limit verification")
        # A low instantaneous clock cannot prove the locked ceiling. A fresh
        # reading above it is, however, direct evidence of a violation.
        if not _finite(snapshot.gpu_measured_mhz) or snapshot.gpu_measured_mhz <= 0:
            reasons.append("GPU measured clock unavailable")
        elif snapshot.gpu_measured_mhz > MAX_GPU_MHZ:
            reasons.append("GPU measured clock above hard envelope")
        elif _finite(accepted) and 0 < accepted <= MAX_GPU_MHZ and snapshot.gpu_measured_mhz > accepted:
            reasons.append("GPU measured clock above accepted limit")
        elif (setter_mode and _finite(requested) and snapshot.gpu_measured_mhz > requested
              and not (type(snapshot.gpu_setter_evidence) is GpuSetterEvidence
                       and _finite(snapshot.gpu_clock_age_s)
                       and snapshot.monotonic_s - snapshot.gpu_clock_age_s
                       - snapshot.gpu_setter_evidence.completed_monotonic_s < CLOCK_SETTLE_S)):
            reasons.append("GPU measured clock above setter request")
        if (not _finite(snapshot.gpu_clock_age_s) or snapshot.gpu_clock_age_s < 0
                or snapshot.gpu_clock_age_s > MAX_SENSOR_AGE_S):
            reasons.append("stale GPU measured clock")
        if (type(snapshot.available_memory_bytes) is not int
                or snapshot.available_memory_bytes < MIN_AVAILABLE_MEMORY_BYTES):
            reasons.append("insufficient available memory")
        for healthy, name in (
            (snapshot.fan_healthy, "fan"),
            (snapshot.cpu_actuator_healthy, "CPU actuator"),
            (snapshot.gpu_actuator_healthy, "GPU actuator"),
            (snapshot.workload_control_healthy, "workload control"),
        ):
            if healthy is not True:
                reasons.append(f"{name} unhealthy")

        if reasons and not self._latched_reasons:
            self._latched_reasons = tuple(reasons)
        return Decision(bool(self._latched_reasons), self._latched_reasons)
