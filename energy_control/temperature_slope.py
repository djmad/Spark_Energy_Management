"""Pure conservative slope estimates for the independent thermal guard.

Sampled slopes do not detect peaks between sensor updates. This module never
reads devices and does not establish the refresh rate of any physical sensor.
"""

from collections import deque
from math import isfinite

from .collector import HostReadout
from .safety import Snapshot, Temperature
from .gpu_evidence import GpuSetterEvidence


class SlopeUnavailable(ValueError):
    pass


class TemperatureSlopeObserver:
    """Track bounded per-sensor positive rise rates over recent observations."""

    def __init__(self, *, maximum_gap_s=1.0, window_s=2.0):
        if (type(maximum_gap_s) not in (int, float) or not isfinite(maximum_gap_s)
                or not 0 < maximum_gap_s <= 1.0
                or type(window_s) not in (int, float) or not isfinite(window_s)
                or not maximum_gap_s <= window_s <= 5.0):
            raise ValueError("invalid slope observation window")
        self.maximum_gap_s = maximum_gap_s
        self.window_s = window_s
        self._history = deque(maxlen=128)
        self._names: frozenset[str] | None = None

    def _fail(self, reason: str):
        self._history.clear()
        self._names = None
        raise SlopeUnavailable(reason)

    def observe(self, time_s: float,
                readings: tuple[tuple[str, float, float], ...]) -> tuple[Temperature, ...]:
        """Return named temperature/slope/age tuples after at least two samples.

        `time_s` is the completion time of a coherent collector pass; each age
        must conservatively include acquisition delay. A gap or identity change
        invalidates the sequence rather than reusing an old derivative.
        """
        if (type(time_s) not in (int, float) or not isfinite(time_s) or time_s < 0
                or type(readings) is not tuple or not 2 <= len(readings) <= 32):
            self._fail("invalid temperature observation")
        current = {}
        for reading in readings:
            if (type(reading) is not tuple or len(reading) != 3):
                self._fail("invalid temperature observation")
            name, celsius, age_s = reading
            if (not isinstance(name, str) or not 1 <= len(name) <= 32
                    or not all(char.isalnum() or char in "-_." for char in name)
                    or name in current or type(celsius) not in (int, float)
                    or not isfinite(celsius) or not -10 < celsius < 150
                    or type(age_s) not in (int, float) or not isfinite(age_s)
                    or not 0 <= age_s <= self.maximum_gap_s):
                self._fail("invalid temperature observation")
            current[name] = (celsius, age_s)
        names = frozenset(current)
        if "gpu" not in names or not any(name.startswith(("acpi", "cpu")) for name in names):
            self._fail("GPU and CPU-proxy temperatures required")
        if self._names is not None and names != self._names:
            self._fail("critical sensor identity changed")
        if self._history:
            gap = time_s - self._history[-1][0]
            if not 0.05 <= gap <= self.maximum_gap_s:
                self._fail("temperature observation gap")
        self._names = names
        while self._history and time_s - self._history[0][0] > self.window_s:
            self._history.popleft()
        if not self._history:
            self._history.append((time_s, current))
            raise SlopeUnavailable("second observation required for slope")
        result = []
        times = [old_time for old_time, _ in self._history] + [time_s]
        mean_t = sum(times) / len(times)
        spread = sum((t - mean_t) ** 2 for t in times)
        for name in sorted(names):
            celsius, age_s = current[name]
            # Least-squares rise over the window. The ACPI core zones step by up
            # to ~5 C per 0.25 s sample; a max pairwise slope turned one step
            # into a 20 C/s "trend" and tripped the prediction at 60 C (live,
            # 26 September 2026). A sustained rise still shows in full.
            values = [previous[name][0] for _, previous in self._history] + [celsius]
            mean_v = sum(values) / len(values)
            slope = sum((t - mean_t) * (v - mean_v) for t, v in zip(times, values)) / spread
            trend = mean_v + slope * (times[-1] - mean_t)
            result.append(Temperature(name, celsius, age_s, max(0.0, slope), trend))
        self._history.append((time_s, current))
        return tuple(result)


def safety_snapshot_from_host(
        readout: HostReadout, observer: TemperatureSlopeObserver, *,
        gpu_requested_max_mhz: float | None = None,
        gpu_accepted_max_mhz: float | None = None,
        gpu_limit_age_s: float | None = float("inf"),
        gpu_setter_evidence: GpuSetterEvidence | None = None,
        fan_actuator_healthy: bool = False,
        cpu_actuator_healthy: bool = False,
        gpu_actuator_healthy: bool = False,
        workload_control_healthy: bool = False) -> Snapshot:
    """Build a guard input from fresh host data and separate trusted proofs.

    No GPU accepted cap or actuator health is inferred from measured clocks,
    RPM alone, or this read-only collector. Defaults are deliberately unsafe.
    """
    if not isinstance(readout, HostReadout) or not isinstance(observer, TemperatureSlopeObserver):
        raise SlopeUnavailable("typed host readout and slope observer required")
    if gpu_setter_evidence is not None:
        if (type(gpu_setter_evidence) is not GpuSetterEvidence
                or gpu_accepted_max_mhz is not None or gpu_limit_age_s is not None):
            raise SlopeUnavailable("setter evidence cannot substitute numeric readback")
    if (type(readout.start_mono_ns) is not int or type(readout.end_mono_ns) is not int
            or readout.start_mono_ns < 0 or readout.end_mono_ns < readout.start_mono_ns):
        raise SlopeUnavailable("invalid host acquisition interval")
    age_s = (readout.end_mono_ns - readout.start_mono_ns) / 1e9
    readings = tuple((name, celsius, age_s) for name, celsius in readout.acpi_temperatures)
    readings += (("gpu", readout.gpu.temperature_c, age_s),)
    temperatures = observer.observe(readout.end_mono_ns / 1e9, readings)
    return Snapshot(
        temperatures=temperatures,
        gpu_requested_max_mhz=gpu_requested_max_mhz,
        gpu_accepted_max_mhz=gpu_accepted_max_mhz,
        gpu_limit_age_s=gpu_limit_age_s,
        gpu_measured_mhz=readout.gpu.measured_mhz,
        gpu_clock_age_s=age_s,
        available_memory_bytes=readout.available_memory_bytes,
        fan_healthy=readout.fan.healthy is True and fan_actuator_healthy is True,
        cpu_actuator_healthy=cpu_actuator_healthy is True,
        gpu_actuator_healthy=gpu_actuator_healthy is True,
        workload_control_healthy=workload_control_healthy is True,
        monotonic_s=readout.end_mono_ns / 1e9,
        gpu_setter_evidence=gpu_setter_evidence,
    )
