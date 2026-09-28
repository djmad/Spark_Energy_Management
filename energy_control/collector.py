"""Read-only Lenovo/GB10 telemetry collector; never applies device settings.

The fan reader is a replaceable platform adapter. Do not call potentially
blocking firmware-backed reads in an emergency-control thread.
"""

from dataclasses import dataclass
from http.client import HTTPConnection
from math import isfinite
import os
from pathlib import Path
import re
import subprocess
from time import monotonic, monotonic_ns, time_ns
from typing import Protocol

from .history import GraphSample
from .recorder import TelemetryRecord, TemperatureRecord


GPU_QUERY = ("/usr/bin/nvidia-smi", "-i", "0",
             "--query-gpu=temperature.gpu,clocks.gr,clocks.applications.gr,"
             "clocks.max.gr,utilization.gpu,power.draw,clocks_event_reasons.active",
             "--format=csv,noheader,nounits")
_POLICY_NAME = re.compile(r"policy[0-9]+\Z")
_ZONE_NAME = re.compile(r"thermal_zone[0-9]+\Z")
LENOVO_ACPI_PATHS = (r"\_TZ_.TSOC", r"\_TZ_.TS0E", r"\_TZ_.TS0P",
                     r"\_TZ_.TS1E", r"\_TZ_.TS1P", r"\_TZ_.TGPU",
                     r"\_TZ_.TUNC")


class TelemetryUnavailable(RuntimeError):
    pass


def _number(text: str, *, low=0, high=1000000):
    if text.strip() in {"N/A", "[N/A]", "Not Supported"}:
        return None
    try:
        value = float(text.strip())
    except ValueError as exc:
        raise TelemetryUnavailable("invalid numeric telemetry") from exc
    if not isfinite(value) or not low <= value <= high:
        raise TelemetryUnavailable("numeric telemetry outside plausible range")
    return value


def _read_int(path: Path, *, low=0, high=100000000):
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError, UnicodeError) as exc:
        raise TelemetryUnavailable(f"unreadable {path.name}") from exc
    if not low <= value <= high:
        raise TelemetryUnavailable(f"implausible {path.name}")
    return value


@dataclass(frozen=True)
class GpuReadout:
    temperature_c: float
    measured_mhz: float
    applications_mhz: float | None
    hardware_max_mhz: float
    utilization_pct: float
    reported_power_w: float | None
    # NVML clock event (throttle) reasons bitmask: when the vendor's own
    # regulation acts (operator, 27 September 2026: "ab wann der Hardware-
    # Regler eingreift"). None when the driver does not report it.
    event_reasons: int | None = None


# NVML clocks-event (throttle) reason bits, as nvidia-smi reports them.
GPU_EVENT_REASONS = {0x1: "gpu_idle", 0x2: "applications_clocks", 0x4: "sw_power_cap",
                     0x8: "hw_slowdown", 0x10: "sync_boost", 0x20: "sw_thermal_slowdown",
                     0x40: "hw_thermal_slowdown", 0x80: "hw_power_brake_slowdown",
                     0x100: "display_clocks"}
# The vendor's own regulation (not our lock, not idle).
GPU_VENDOR_REASONS = 0x4 | 0x8 | 0x20 | 0x40 | 0x80


def gpu_event_names(mask):
    if mask is None:
        return None
    return [name for bit, name in GPU_EVENT_REASONS.items() if mask & bit]


@dataclass(frozen=True)
class CpuPolicy:
    index: int
    hardware_max_mhz: float
    requested_max_mhz: float
    measured_mhz: float
    hardware_min_mhz: float | None = None


@dataclass(frozen=True)
class FanReadout:
    platform: str
    floor_state: int | None
    max_state: int | None
    rpm: tuple[int | None, int | None]
    healthy: bool


class FanTelemetryAdapter(Protocol):
    def read(self) -> FanReadout: ...


class QueueTelemetryAdapter(Protocol):
    def read(self) -> tuple[int, int]: ...  # active, waiting


class VllmQueueTelemetry:
    """Read only two vLLM gauges; never retain labels or request content."""

    def __init__(self, *, port=8000):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid vLLM loopback port")
        self.port = port

    def read(self):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=0.5)
        try:
            connection.request("GET", "/metrics")
            response = connection.getresponse()
            if response.status != 200:
                raise TelemetryUnavailable("vLLM metrics unavailable")
            body = response.read(256 * 1024 + 1)
            if len(body) > 256 * 1024:
                raise TelemetryUnavailable("vLLM metrics response too large")
        except (OSError, TimeoutError) as exc:
            raise TelemetryUnavailable("vLLM metrics unavailable") from exc
        finally:
            connection.close()
        return self.parse(body)

    @staticmethod
    def parse(body: bytes):
        try:
            lines = body.decode("utf-8").splitlines()
        except UnicodeError as exc:
            raise TelemetryUnavailable("invalid vLLM metrics encoding") from exc
        totals = {"vllm:num_requests_running": 0, "vllm:num_requests_waiting": 0}
        seen = set()
        for line in lines:
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            name = parts[0].split("{", 1)[0]
            if name not in totals:
                continue
            try:
                value = float(parts[1])
            except ValueError as exc:
                raise TelemetryUnavailable("invalid vLLM queue gauge") from exc
            if not isfinite(value) or not 0 <= value <= 100000 or not value.is_integer():
                raise TelemetryUnavailable("invalid vLLM queue gauge")
            totals[name] += int(value)
            seen.add(name)
        if seen != totals.keys():
            raise TelemetryUnavailable("missing vLLM queue gauge")
        return totals["vllm:num_requests_running"], totals["vllm:num_requests_waiting"]


class LenovoFanTelemetry:
    """Only reads the guarded Lenovo fan-floor driver's public sysfs nodes."""

    def __init__(self, *, thermal_root=Path("/sys/class/thermal"),
                 hwmon_root=Path("/sys/class/hwmon")):
        self.thermal_root = Path(thermal_root)
        self.hwmon_root = Path(hwmon_root)

    def read(self):
        coolers = []
        for path in self.thermal_root.glob("cooling_device*"):
            try:
                if (path / "type").read_text(encoding="ascii").strip() == "dgx_ec_fan_floor":
                    coolers.append(path)
            except (OSError, UnicodeError):
                continue
        hwmons = []
        for path in self.hwmon_root.glob("hwmon*"):
            try:
                if (path / "name").read_text(encoding="ascii").strip() == "dgx_ec_fan":
                    hwmons.append(path)
            except (OSError, UnicodeError):
                continue
        if len(coolers) != 1 or len(hwmons) != 1:
            return FanReadout("lenovo-dgx-ec", None, None, (None, None), False)
        try:
            state = _read_int(coolers[0] / "cur_state", high=12)
            maximum = _read_int(coolers[0] / "max_state", high=12)
            rpm = (_read_int(hwmons[0] / "fan1_input", high=100000),
                   _read_int(hwmons[0] / "fan2_input", high=100000))
        except TelemetryUnavailable:
            return FanReadout("lenovo-dgx-ec", None, None, (None, None), False)
        return FanReadout("lenovo-dgx-ec", state, maximum, rpm,
                          maximum == 12 and all(value > 0 for value in rpm))


@dataclass(frozen=True)
class HostReadout:
    start_mono_ns: int
    end_mono_ns: int
    utc_ns: int
    acpi_temperatures: tuple[tuple[str, float], ...]
    gpu: GpuReadout
    cpu_policies: tuple[CpuPolicy, ...]
    cpu_util_pct: float | None
    fan: FanReadout
    available_memory_bytes: int
    active_jobs: int | None = None
    queued_jobs: int | None = None
    logical_cpu_count: int | None = None

    def graph_sample(self):
        return GraphSample(self.end_mono_ns / 1e9, self.utc_ns,
                           max(value for _, value in self.acpi_temperatures),
                           self.gpu.temperature_c, self.cpu_util_pct,
                           self.gpu.utilization_pct)

    def telemetry_record(self):
        age = (self.end_mono_ns - self.start_mono_ns) / 1e9
        temperatures = tuple(TemperatureRecord(name, value, None, age)
                             for name, value in self.acpi_temperatures)
        temperatures += (TemperatureRecord("gpu", self.gpu.temperature_c, None, age),)
        fast = [policy for policy in self.cpu_policies if policy.hardware_max_mhz > 3000]
        slow = [policy for policy in self.cpu_policies if policy.hardware_max_mhz <= 3000]
        def class_values(policies):
            if not policies:
                return None, None, None, None, None
            caps = {policy.requested_max_mhz for policy in policies}
            minima = {policy.hardware_min_mhz for policy in policies}
            maxima = {policy.hardware_max_mhz for policy in policies}
            cap = caps.pop() if len(caps) == 1 else None
            minimum = minima.pop() if len(minima) == 1 else None
            maximum = maxima.pop() if len(maxima) == 1 else None
            ratio = ((cap - minimum) / (maximum - minimum)
                     if cap is not None and minimum is not None and maximum is not None
                     and maximum > minimum else None)
            return (cap, sum(policy.measured_mhz for policy in policies) / len(policies),
                    minimum, maximum, ratio)
        fast_cap, fast_measured, fast_min, fast_max, fast_ratio = class_values(fast)
        slow_cap, slow_measured, slow_min, slow_max, slow_ratio = class_values(slow)
        return TelemetryRecord(
            phase="unclassified", temperatures=temperatures,
            gpu_requested_mhz=None, gpu_accepted_mhz=None,
            gpu_measured_mhz=self.gpu.measured_mhz,
            cpu_fast_requested_mhz=fast_cap, cpu_fast_measured_mhz=fast_measured,
            cpu_slow_requested_mhz=slow_cap, cpu_slow_measured_mhz=slow_measured,
            fan_floor_state=self.fan.floor_state, fan_rpm=self.fan.rpm,
            available_memory_bytes=self.available_memory_bytes,
            cpu_util_pct=self.cpu_util_pct, gpu_util_pct=self.gpu.utilization_pct,
            queued_jobs=self.queued_jobs, active_jobs=self.active_jobs,
            gpu_reported_power_w=self.gpu.reported_power_w,
            system_input_power_w=None,
            gpu_application_mhz=self.gpu.applications_mhz,
            gpu_hardware_max_mhz=self.gpu.hardware_max_mhz,
            gpu_clock_age_s=age,
            sample_mono_ns=self.end_mono_ns, sample_utc_ns=self.utc_ns,
            cpu_logical_count=self.logical_cpu_count,
            cpu_policy_count=len(self.cpu_policies),
            cpu_fast_hardware_min_mhz=fast_min, cpu_fast_hardware_max_mhz=fast_max,
            cpu_fast_cap_ratio=fast_ratio,
            cpu_slow_hardware_min_mhz=slow_min, cpu_slow_hardware_max_mhz=slow_max,
            cpu_slow_cap_ratio=slow_ratio)


class BackgroundRpmTelemetry:
    """Fan health from the driver's cached RPM telemetry, off the sampling path.

    Never reads ``cur_state``: on this driver every such read is an uncached
    EC transaction, and several readers made the EC mailbox time out (1.1 s)
    or return EBUSY, stalling temperature sampling (live, 26 September 2026).
    A background thread polls ``fan{1,2}_input`` every ``poll_s``; a failed
    poll keeps the last good values for at most ``hold_s``. Construct it in
    the process that reads it (its thread is not inherited or pickled).
    """

    def __init__(self, *, hwmon_root=Path("/sys/class/hwmon"), poll_s=1.0, hold_s=3.0):
        from threading import Lock
        self.hwmon_root, self.poll_s, self.hold_s = Path(hwmon_root), poll_s, hold_s
        self._lock = Lock()
        self._rpm, self._at = (None, None), None
        self._thread = None

    def _poll_once(self):
        try:
            hwmons = [path for path in self.hwmon_root.glob("hwmon*")
                      if (path / "name").read_text(encoding="ascii").strip() == "dgx_ec_fan"]
            if len(hwmons) != 1:
                return
            rpm = (_read_int(hwmons[0] / "fan1_input", high=100000),
                   _read_int(hwmons[0] / "fan2_input", high=100000))
        except (OSError, UnicodeError, TelemetryUnavailable):
            return
        with self._lock:
            self._rpm, self._at = rpm, monotonic()

    def _run(self):
        from time import sleep
        while True:
            sleep(self.poll_s)
            self._poll_once()

    def read(self):
        if self._thread is None:
            from threading import Thread
            self._poll_once()  # First read is synchronous; no false start-up fault.
            self._thread = Thread(target=self._run, daemon=True, name="energy-fan-rpm")
            self._thread.start()
        with self._lock:
            rpm, at = self._rpm, self._at
        healthy = (at is not None and monotonic() - at <= self.hold_s
                   and all(type(value) is int and value > 0 for value in rpm))
        return FanReadout("lenovo-dgx-ec", None, 12, rpm, healthy)


class VllmTokenCounters:
    """Background 1 Hz poll of vLLM's cumulative token counters for traces.

    Measurement only (throughput vs clocks for the twin): never used for
    control, never blocks the caller, keeps no labels or request content.
    ``latest()`` is None until a poll succeeded and after 3 s without one.
    """

    NAMES = {"vllm:generation_tokens_total": "gen", "vllm:prompt_tokens_total": "prompt",
             "vllm:prompt_tokens_cached_total": "cached"}

    def __init__(self, *, port=8000, period_s=1.0, fetch=None):
        from threading import Event, Lock, Thread
        self._fetch = fetch or partial_fetch(port)
        self._period_s, self._lock, self._stop = period_s, Lock(), Event()
        self._latest, self._at = None, None
        self._thread = Thread(target=self._run, daemon=True, name="vllm-token-counters")
        self._thread.start()

    @classmethod
    def parse(cls, body: bytes):
        totals = {}
        for line in body.decode("utf-8", "replace").splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            key = cls.NAMES.get(parts[0].split("{", 1)[0]) if len(parts) >= 2 else None
            if key is None:
                continue
            try:
                value = float(parts[-1])
            except ValueError:
                continue
            if isfinite(value) and value >= 0:
                totals[key] = totals.get(key, 0.0) + value
        return totals if "gen" in totals else None

    def _run(self):
        while not self._stop.wait(0 if self._at is None else self._period_s):
            try:
                totals = self.parse(self._fetch())
            except Exception:
                totals = None
            if totals is not None:
                with self._lock:
                    self._latest, self._at = totals, monotonic()
            elif self._at is None:
                self._at = monotonic() - 10.0  # Start the regular period after a failure.

    def latest(self):
        with self._lock:
            if self._latest is None or monotonic() - self._at > 3.0:
                return None
            return dict(self._latest)

    def close(self):
        self._stop.set()


def partial_fetch(port):
    def fetch():
        connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
        try:
            connection.request("GET", "/metrics")
            response = connection.getresponse()
            if response.status != 200:
                raise TelemetryUnavailable("vLLM metrics unavailable")
            return response.read(512 * 1024)
        finally:
            connection.close()
    return fetch


class BackgroundQueueTelemetry:
    """vLLM queue gauges polled off the thermal sampling path.

    A synchronous /metrics poll (0.5 s timeout) inside the collector delayed
    thermal frames to 1.0-1.4 s under 12 concurrent requests and tripped the
    policy's interval check (live, 27 September 2026). A background thread
    polls every ``poll_s``; ``read`` returns the latest gauges if they are at
    most ``hold_s`` old, else raises TelemetryUnavailable. Construct it in
    the process that reads it (threads are not inherited).
    """

    def __init__(self, source=None, *, poll_s=0.5, hold_s=3.0):
        from threading import Lock
        self._source = source or VllmQueueTelemetry()
        self.poll_s, self.hold_s = poll_s, hold_s
        self._lock = Lock()
        self._value, self._at = None, None
        self._thread = None

    def _poll_once(self):
        try:
            value = self._source.read()
        except (TelemetryUnavailable, OSError, ValueError):
            return
        with self._lock:
            self._value, self._at = value, monotonic()

    def _run(self):
        from time import sleep
        while True:
            sleep(self.poll_s)
            self._poll_once()

    def read(self):
        if self._thread is None:
            from threading import Thread
            self._poll_once()  # First read synchronous: no false start-up gap.
            self._thread = Thread(target=self._run, daemon=True, name="vllm-queue")
            self._thread.start()
        with self._lock:
            value, at = self._value, self._at
        if value is None or monotonic() - at > self.hold_s:
            raise TelemetryUnavailable("vLLM queue gauges unavailable")
        return value


def service_collector(vllm_queue=False):
    """Collector for the installed service: cached-RPM fan health, no EC floor reads,
    vLLM queue gauges from a background poll."""
    return LenovoReadOnlyCollector(fan=BackgroundRpmTelemetry(),
                                   queue=BackgroundQueueTelemetry() if vllm_queue else None)


class LenovoReadOnlyCollector:
    def __init__(self, *, thermal_root=Path("/sys/class/thermal"),
                 cpufreq_root=Path("/sys/devices/system/cpu/cpufreq"),
                 proc_root=Path("/proc"), fan: FanTelemetryAdapter | None = None,
                 queue: QueueTelemetryAdapter | None = None,
                 gpu_query=None, minimum_acpi_zones=7, minimum_cpu_policies=20,
                 expected_acpi_paths=LENOVO_ACPI_PATHS):
        self.thermal_root = Path(thermal_root)
        self.cpufreq_root = Path(cpufreq_root)
        self.proc_root = Path(proc_root)
        self.fan = fan or LenovoFanTelemetry(thermal_root=self.thermal_root)
        self.queue = queue
        self.gpu_query = gpu_query or self._query_gpu
        self._previous_cpu: tuple[int, int] | None = None
        if (type(minimum_acpi_zones) is not int or minimum_acpi_zones < 1
                or type(minimum_cpu_policies) is not int or minimum_cpu_policies < 1):
            raise ValueError("minimum sensor counts must be positive integers")
        self.minimum_acpi_zones = minimum_acpi_zones
        self.minimum_cpu_policies = minimum_cpu_policies
        if (type(expected_acpi_paths) is not tuple
                or len(expected_acpi_paths) != minimum_acpi_zones
                or len(set(expected_acpi_paths)) != len(expected_acpi_paths)
                or any(not isinstance(path, str) or not re.fullmatch(
                    r"\\_TZ_\.[A-Z0-9]{4}", path) for path in expected_acpi_paths)):
            raise ValueError("invalid pinned ACPI zone paths")
        self.expected_acpi_paths = expected_acpi_paths
        self._expected_acpi: frozenset[str] | None = None
        self._expected_policies: frozenset[int] | None = None

    @staticmethod
    def _query_gpu():
        try:
            result = subprocess.run(GPU_QUERY, capture_output=True, text=True,
                                    check=True, timeout=1.0)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TelemetryUnavailable("GPU telemetry unavailable") from exc
        return result.stdout

    def _gpu(self):
        lines = self.gpu_query().strip().splitlines()
        if len(lines) != 1:
            raise TelemetryUnavailable("expected one GPU telemetry row")
        parts = [part.strip() for part in lines[0].split(",")]
        if len(parts) != 7:
            raise TelemetryUnavailable("unexpected GPU telemetry fields")
        temperature, measured, applications, hardware_max, utilization, power, reasons = parts
        try:
            event_reasons = int(reasons, 16) if reasons.lower().startswith("0x") else None
        except ValueError:
            event_reasons = None  # informational only; never fails the sample
        parsed = GpuReadout(_number(temperature, low=-10, high=150),
                            _number(measured, high=4000),
                            _number(applications, high=4000),
                            _number(hardware_max, high=4000),
                            _number(utilization, high=100),
                            _number(power, high=1000),
                            event_reasons)
        if any(value is None for value in (parsed.temperature_c, parsed.measured_mhz,
                                            parsed.hardware_max_mhz, parsed.utilization_pct)):
            raise TelemetryUnavailable("missing critical GPU telemetry")
        return parsed

    def _acpi(self):
        readings = []
        for path in sorted(self.thermal_root.glob("thermal_zone*")):
            if not _ZONE_NAME.fullmatch(path.name):
                continue
            try:
                zone_type = (path / "type").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError):
                continue
            if zone_type != "acpitz":
                continue
            index = int(path.name.removeprefix("thermal_zone"))
            if index >= len(self.expected_acpi_paths):
                raise TelemetryUnavailable("unexpected ACPI zone identity")
            try:
                acpi_path = (path / "device" / "path").read_text(encoding="ascii").strip()
            except (OSError, UnicodeError) as exc:
                raise TelemetryUnavailable("ACPI firmware path unavailable") from exc
            if acpi_path != self.expected_acpi_paths[index]:
                raise TelemetryUnavailable("ACPI firmware path changed")
            try:
                value = _read_int(path / "temp", low=-10000, high=150000) / 1000
            except TelemetryUnavailable:
                continue
            readings.append(("acpi_" + acpi_path.rsplit(".", 1)[-1], value))
        names = frozenset(name for name, _ in readings)
        if (len(readings) != self.minimum_acpi_zones or len(names) != len(readings)):
            raise TelemetryUnavailable("incomplete pinned ACPI temperatures")
        if self._expected_acpi is not None and names != self._expected_acpi:
            raise TelemetryUnavailable("ACPI sensor identity changed or disappeared")
        self._expected_acpi = names
        return tuple(readings)

    def _cpu_policies(self):
        policies = []
        for path in sorted(self.cpufreq_root.glob("policy*")):
            if not _POLICY_NAME.fullmatch(path.name):
                continue
            index = int(path.name.removeprefix("policy"))
            try:
                minimum = _read_int(path / "cpuinfo_min_freq") / 1000
                hardware = _read_int(path / "cpuinfo_max_freq") / 1000
                requested = _read_int(path / "scaling_max_freq") / 1000
                measured = _read_int(path / "scaling_cur_freq") / 1000
            except TelemetryUnavailable:
                continue
            if not minimum <= requested <= hardware:
                raise TelemetryUnavailable("CPU policy cap outside hardware bounds")
            policies.append(CpuPolicy(index, hardware, requested, measured, minimum))
        indices = frozenset(policy.index for policy in policies)
        if len(policies) < self.minimum_cpu_policies:
            raise TelemetryUnavailable("insufficient readable CPU policies")
        if self._expected_policies is not None and indices != self._expected_policies:
            raise TelemetryUnavailable("CPU policy identity changed or disappeared")
        self._expected_policies = indices
        return tuple(policies)

    def _memory(self):
        try:
            lines = (self.proc_root / "meminfo").read_text(encoding="ascii").splitlines()
        except (OSError, UnicodeError) as exc:
            raise TelemetryUnavailable("memory telemetry unavailable") from exc
        for line in lines:
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) == 3 and parts[2] == "kB":
                    try:
                        return int(parts[1]) * 1024
                    except ValueError:
                        break
        raise TelemetryUnavailable("MemAvailable unavailable")

    def _cpu_utilization(self):
        try:
            line = (self.proc_root / "stat").read_text(encoding="ascii").splitlines()[0]
            parts = line.split()
            if parts[0] != "cpu" or len(parts) < 9:
                raise ValueError
            values = [int(value) for value in parts[1:9]]
        except (OSError, UnicodeError, ValueError, IndexError) as exc:
            raise TelemetryUnavailable("CPU utilization counters unavailable") from exc
        total = sum(values)
        idle = values[3] + values[4]
        previous = self._previous_cpu
        self._previous_cpu = (total, idle)
        if previous is None or total <= previous[0]:
            return None
        busy = (total - previous[0]) - (idle - previous[1])
        return max(0.0, min(100.0, 100 * busy / (total - previous[0])))

    def collect(self):
        start = monotonic_ns()
        gpu = self._gpu()
        t_gpu = monotonic_ns()
        acpi = self._acpi()
        policies = self._cpu_policies()
        available = self._memory()
        cpu_util = self._cpu_utilization()
        t_host = monotonic_ns()
        fan = self.fan.read()
        t_fan = monotonic_ns()
        active = queued = None
        if self.queue is not None:
            try:
                active, queued = self.queue.read()
            except TelemetryUnavailable:
                pass
        end = monotonic_ns()
        # Per-part acquisition time (ms), for slow-frame diagnostics only.
        self.last_timings = {"gpu": (t_gpu - start) // 10**6, "host": (t_host - t_gpu) // 10**6,
                             "fan": (t_fan - t_host) // 10**6, "queue": (end - t_fan) // 10**6}
        return HostReadout(start, end, time_ns(), acpi, gpu, policies,
                           cpu_util, fan, available, active, queued, os.cpu_count())
