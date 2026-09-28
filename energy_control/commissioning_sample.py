"""Pure, consistency-checked commissioning sample assembly; no device I/O.

The root-side harness must obtain the numeric GPU reading from an independently
qualified source. Measured/app/OEM clocks in HostReadout are never substituted.
"""

from dataclasses import replace
from math import isfinite

from .collector import HostReadout
from .lifecycle import GpuLimitReading
from .recorder import TelemetryRecord, TemperatureRecord
from .safety import Snapshot, Temperature
from .gpu_evidence import GpuSetterEvidence


def build_commissioning_sample(readout: HostReadout, snapshot: Snapshot,
                               gpu_limit: GpuLimitReading | GpuSetterEvidence, *, phase: str,
                               cpu_demand_active: bool | None = None,
                               cpu_work_arrival: bool | None = None,
                               prefill_arrival: bool | None = None,
                               model_loading: bool | None = None) -> TelemetryRecord:
    if (not isinstance(readout, HostReadout) or not isinstance(snapshot, Snapshot)
            or type(gpu_limit) not in (GpuLimitReading, GpuSetterEvidence)):
        raise TypeError("typed host, safety and GPU evidence required")
    setter_mode = type(gpu_limit) is GpuSetterEvidence
    if (type(readout.start_mono_ns) is not int or type(readout.end_mono_ns) is not int
            or readout.start_mono_ns < 0 or readout.end_mono_ns < readout.start_mono_ns):
        raise ValueError("invalid sample acquisition or GPU proof age")
    sample_time = readout.end_mono_ns / 1e9
    if (not isfinite(sample_time) or snapshot.monotonic_s != sample_time
            or snapshot.gpu_measured_mhz != readout.gpu.measured_mhz
            or type(snapshot.gpu_clock_age_s) not in (int, float)
            or not isfinite(snapshot.gpu_clock_age_s)
            or abs(snapshot.gpu_clock_age_s
                   - (readout.end_mono_ns - readout.start_mono_ns) / 1e9) > 0.05):
        raise ValueError("GPU clock or sample timeline is inconsistent")
    if setter_mode:
        if (gpu_limit != snapshot.gpu_setter_evidence
                or snapshot.gpu_accepted_max_mhz is not None or snapshot.gpu_limit_age_s is not None
                or not gpu_limit.matches(snapshot.gpu_requested_max_mhz, sample_time,
                    (gpu_limit.boot_id, gpu_limit.driver_epoch, gpu_limit.owner_epoch, gpu_limit.run_id))):
            raise ValueError("GPU setter evidence is inconsistent")
    elif (gpu_limit.accepted_max_mhz != snapshot.gpu_accepted_max_mhz
            or snapshot.gpu_setter_evidence is not None
            or type(snapshot.gpu_limit_age_s) not in (int, float)
            or gpu_limit.observed_monotonic_s > sample_time
            or not 0 <= sample_time - gpu_limit.observed_monotonic_s <= 0.5
            or not isfinite(snapshot.gpu_limit_age_s)
            or abs(snapshot.gpu_limit_age_s
                   - (sample_time - gpu_limit.observed_monotonic_s)) > 0.05):
        raise ValueError("GPU limit or sample timeline is inconsistent")
    expected = dict(readout.acpi_temperatures)
    expected["gpu"] = readout.gpu.temperature_c
    if (type(snapshot.temperatures) is not tuple
            or len(snapshot.temperatures) != len(expected)
            or any(not isinstance(sensor, Temperature)
                   or sensor.name not in expected
                   or sensor.celsius != expected[sensor.name]
                   for sensor in snapshot.temperatures)
            or len({sensor.name for sensor in snapshot.temperatures}) != len(expected)):
        raise ValueError("temperature identity or value mismatch")
    if snapshot.fan_healthy and readout.fan.healthy is not True:
        raise ValueError("fan health contradicts readback")
    base = readout.telemetry_record()
    temperatures = tuple(TemperatureRecord(sensor.name, sensor.celsius,
                                           sensor.rising_c_per_s, sensor.age_s)
                         for sensor in snapshot.temperatures)
    return replace(base, phase=phase, temperatures=temperatures,
                   model_loading=model_loading,
                   prefill_arrival=prefill_arrival,
                   cpu_demand_active=cpu_demand_active, cpu_work_arrival=cpu_work_arrival,
                   gpu_requested_mhz=snapshot.gpu_requested_max_mhz,
                   gpu_accepted_mhz=None if setter_mode else gpu_limit.accepted_max_mhz,
                   gpu_limit_age_s=None if setter_mode else sample_time - gpu_limit.observed_monotonic_s,
                   gpu_setter_evidence=gpu_limit if setter_mode else None,
                   gpu_clock_age_s=snapshot.gpu_clock_age_s,
                   fan_healthy=snapshot.fan_healthy,
                   cpu_actuator_healthy=snapshot.cpu_actuator_healthy,
                   gpu_actuator_healthy=snapshot.gpu_actuator_healthy,
                   workload_control_healthy=snapshot.workload_control_healthy)
