"""Bounded read-only Stage-0 observation of the existing host stack.

No workload, controller, broker, GPU lock, CPU policy or fan write is made.
Only aggregate counters and extrema are retained; no prompt or raw time series
is stored. This cannot establish the effective GPU lock or thermal response.
"""

import argparse
from dataclasses import asdict, dataclass
import json
from math import isfinite
from time import monotonic, sleep

from .collector import HostReadout, LenovoReadOnlyCollector, TelemetryUnavailable, VllmQueueTelemetry
from .limits import GPU_HARD_MAX_MHZ


@dataclass
class PassiveSummary:
    requested_samples: int
    interval_s: float
    successful_samples: int = 0
    failed_samples: int = 0
    first_utc_ns: int | None = None
    last_utc_ns: int | None = None
    maximum_acquisition_s: float | None = None
    minimum_acpi_c: float | None = None
    maximum_acpi_c: float | None = None
    minimum_gpu_c: float | None = None
    maximum_gpu_c: float | None = None
    maximum_measured_gpu_mhz: float | None = None
    maximum_gpu_util_pct: float | None = None
    minimum_gpu_reported_power_w: float | None = None
    maximum_gpu_reported_power_w: float | None = None
    minimum_available_memory_bytes: int | None = None
    maximum_active_jobs: int | None = None
    maximum_queued_jobs: int | None = None
    queue_metric_missing_samples: int = 0
    minimum_cpu_policy_count: int | None = None
    maximum_cpu_policy_count: int | None = None
    unexpected_cpu_class_samples: int = 0
    minimum_cpu_fast_requested_mhz: float | None = None
    maximum_cpu_fast_requested_mhz: float | None = None
    minimum_cpu_slow_requested_mhz: float | None = None
    maximum_cpu_slow_requested_mhz: float | None = None
    minimum_fan_floor_state: int | None = None
    maximum_fan_floor_state: int | None = None
    minimum_fan_rpm: int | None = None
    maximum_fan_rpm: int | None = None
    fan_unhealthy_samples: int = 0
    observed_gpu_clock_above_1800: bool = False

    def add(self, readout: HostReadout):
        if (not isinstance(readout, HostReadout) or not readout.acpi_temperatures
                or readout.end_mono_ns < readout.start_mono_ns):
            raise ValueError("invalid passive host readout")
        acquisition = (readout.end_mono_ns - readout.start_mono_ns) / 1e9
        acpi = [value for _, value in readout.acpi_temperatures]
        values = (*acpi, readout.gpu.temperature_c, readout.gpu.measured_mhz,
                  readout.gpu.utilization_pct, acquisition)
        if not all(isfinite(value) for value in values):
            raise ValueError("non-finite passive telemetry")
        def lower(old, new):
            return new if old is None else min(old, new)
        def upper(old, new):
            return new if old is None else max(old, new)
        self.successful_samples += 1
        if self.first_utc_ns is None:
            self.first_utc_ns = readout.utc_ns
        self.last_utc_ns = readout.utc_ns
        self.maximum_acquisition_s = upper(self.maximum_acquisition_s, acquisition)
        self.minimum_acpi_c = lower(self.minimum_acpi_c, min(acpi))
        self.maximum_acpi_c = upper(self.maximum_acpi_c, max(acpi))
        self.minimum_gpu_c = lower(self.minimum_gpu_c, readout.gpu.temperature_c)
        self.maximum_gpu_c = upper(self.maximum_gpu_c, readout.gpu.temperature_c)
        self.maximum_measured_gpu_mhz = upper(self.maximum_measured_gpu_mhz,
                                              readout.gpu.measured_mhz)
        self.maximum_gpu_util_pct = upper(self.maximum_gpu_util_pct,
                                           readout.gpu.utilization_pct)
        if readout.gpu.reported_power_w is not None:
            self.minimum_gpu_reported_power_w = lower(
                self.minimum_gpu_reported_power_w, readout.gpu.reported_power_w)
            self.maximum_gpu_reported_power_w = upper(
                self.maximum_gpu_reported_power_w, readout.gpu.reported_power_w)
        self.minimum_available_memory_bytes = lower(
            self.minimum_available_memory_bytes, readout.available_memory_bytes)
        if readout.active_jobs is None or readout.queued_jobs is None:
            self.queue_metric_missing_samples += 1
        if readout.active_jobs is not None:
            self.maximum_active_jobs = upper(self.maximum_active_jobs, readout.active_jobs)
        if readout.queued_jobs is not None:
            self.maximum_queued_jobs = upper(self.maximum_queued_jobs, readout.queued_jobs)
        self.minimum_cpu_policy_count = lower(self.minimum_cpu_policy_count,
                                               len(readout.cpu_policies))
        self.maximum_cpu_policy_count = upper(self.maximum_cpu_policy_count,
                                               len(readout.cpu_policies))
        fast_count = sum(policy.hardware_max_mhz > 3000 for policy in readout.cpu_policies)
        if len(readout.cpu_policies) != 20 or fast_count != 10:
            self.unexpected_cpu_class_samples += 1
        for policy in readout.cpu_policies:
            if policy.hardware_max_mhz > 3000:
                self.minimum_cpu_fast_requested_mhz = lower(
                    self.minimum_cpu_fast_requested_mhz, policy.requested_max_mhz)
                self.maximum_cpu_fast_requested_mhz = upper(
                    self.maximum_cpu_fast_requested_mhz, policy.requested_max_mhz)
            else:
                self.minimum_cpu_slow_requested_mhz = lower(
                    self.minimum_cpu_slow_requested_mhz, policy.requested_max_mhz)
                self.maximum_cpu_slow_requested_mhz = upper(
                    self.maximum_cpu_slow_requested_mhz, policy.requested_max_mhz)
        if readout.fan.floor_state is not None:
            self.minimum_fan_floor_state = lower(self.minimum_fan_floor_state,
                                                 readout.fan.floor_state)
            self.maximum_fan_floor_state = upper(self.maximum_fan_floor_state,
                                                 readout.fan.floor_state)
        for rpm in readout.fan.rpm:
            if rpm is not None:
                self.minimum_fan_rpm = lower(self.minimum_fan_rpm, rpm)
                self.maximum_fan_rpm = upper(self.maximum_fan_rpm, rpm)
        if readout.fan.healthy is not True:
            self.fan_unhealthy_samples += 1
        if readout.gpu.measured_mhz > GPU_HARD_MAX_MHZ:
            self.observed_gpu_clock_above_1800 = True


def run_probe(collector, *, samples: int, interval_s: float,
              clock=monotonic, sleeper=sleep) -> PassiveSummary:
    if (type(samples) is not int or not 1 <= samples <= 120
            or type(interval_s) not in (int, float) or not isfinite(interval_s)
            or not 0.25 <= interval_s <= 10):
        raise ValueError("probe requires 1..120 samples at 0.25..10 s")
    result = PassiveSummary(samples, interval_s)
    next_tick = clock()
    for index in range(samples):
        try:
            result.add(collector.collect())
        except (TelemetryUnavailable, ValueError, OSError):
            result.failed_samples += 1
        if index + 1 < samples:
            next_tick += interval_s
            sleeper(max(0.0, next_tick - clock()))
            if clock() > next_tick + interval_s:
                next_tick = clock()  # no catch-up burst after a slow read
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only aggregate Spark baseline")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args(argv)
    collector = LenovoReadOnlyCollector(queue=VllmQueueTelemetry())
    result = run_probe(collector, samples=args.samples, interval_s=args.interval)
    print(json.dumps({"read_only": True, "load_started": False,
                      "gpu_effective_lock_verified": False,
                      "thermal_model_calibrated": False, **asdict(result)},
                     separators=(",", ":")))
    return 0 if result.successful_samples else 1


if __name__ == "__main__":
    raise SystemExit(main())
