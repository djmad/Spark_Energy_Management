"""Pure reference of the installed 2026-09-26 CPU guard control law.

This is for offline comparisons only. It has no sensor discovery or device I/O,
and its 93 C target is *not* a safe commissioning target.
"""

from dataclasses import dataclass
from math import isfinite


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def safety_ceiling(temperature_c: float) -> float:
    """Exact stepwise ceiling in spark-cpu-thermal-guard.js."""
    if not isfinite(temperature_c):
        raise ValueError("finite temperature required")
    for threshold, ceiling in ((97, 0), (95, .25), (93, .50),
                               (92, .65), (90, .80), (88, .95)):
        if temperature_c >= threshold:
            return ceiling
    return 1.0


def class_ratios(ratio: float, temperature_c: float, mode: str = "fast-first") -> tuple[float, float]:
    """Return (fast, slow) normalized policy caps, not measured clocks."""
    if not isfinite(ratio) or not isfinite(temperature_c) or mode not in ("uniform", "fast-first"):
        raise ValueError("invalid legacy CPU class input")
    fast = _clamp(ratio, 0, 1)
    slow = _clamp(fast / .75, 0, 1) if mode == "fast-first" else fast
    if temperature_c >= 95:
        slow = min(slow, safety_ceiling(temperature_c))
    return fast, slow


@dataclass(frozen=True)
class LegacyCpuStep:
    temperature_c: float
    dt_s: float
    derivative_c_s: float
    integral: float
    cap_ratio: float
    fast_ratio: float
    slow_ratio: float


class LegacyCpuPid:
    """Faithful per-tick arithmetic; caller supplies hottest legacy ACPI value.

    The installed loop uses Date.now(), not a monotonic clock. ``now_ms``
    deliberately reflects that legacy behavior, including its 0.1..2 s dt
    clamp. Never use this reference as an actuator policy.
    """

    def __init__(self, mode: str = "fast-first"):
        if mode not in ("uniform", "fast-first"):
            raise ValueError("invalid legacy CPU cap mode")
        self.mode = mode
        self.integral = 1.0
        self.last_c: float | None = None
        self.last_time_ms: float | None = None
        self.cap = 1.0

    def step(self, temperature_c: float, now_ms: float) -> LegacyCpuStep:
        if not isfinite(temperature_c) or not isfinite(now_ms):
            raise ValueError("finite legacy input required")
        dt = (.5 if self.last_time_ms is None else
              _clamp((now_ms - self.last_time_ms) / 1000, .1, 2))
        derivative = 0.0 if self.last_c is None else (temperature_c - self.last_c) / dt
        error = 93 - temperature_c
        provisional = self.integral + .075 * error - .06 * derivative
        if ((0 < provisional < 1) or (provisional >= 1 and error < 0)
                or (provisional <= 0 and error > 0)):
            self.integral = _clamp(self.integral + .012 * error * dt, 0, 1)
        wanted = _clamp(self.integral + .075 * error - .06 * derivative, 0, 1)
        wanted = min(wanted, safety_ceiling(temperature_c))
        self.cap = min(wanted, self.cap + .015)
        self.last_c = temperature_c
        self.last_time_ms = now_ms
        fast, slow = class_ratios(self.cap, temperature_c, self.mode)
        return LegacyCpuStep(temperature_c, dt, derivative, self.integral,
                             self.cap, fast, slow)
