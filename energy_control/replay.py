"""Offline replay of bounded commissioning records through the shadow policy.

Only the valid durable prefix is replayed. A crash/torn tail is reported as
incomplete evidence, never interpreted as a clean or successful experiment.
"""

from dataclasses import MISSING, dataclass, fields
from pathlib import Path

from .broker import Config
from .policy import PolicyInput, ProposedLimits, ShadowPolicy
from .recorder import TelemetryRecord, TemperatureRecord, inspect_run
from .safety import Snapshot, Temperature
from .gpu_evidence import GpuSetterEvidence


@dataclass(frozen=True)
class ReplayPoint:
    record_seq: int
    monotonic_s: float
    limits: ProposedLimits


@dataclass(frozen=True)
class ReplayResult:
    points: tuple[ReplayPoint, ...]
    clean_end: bool
    incomplete_tail: bool
    corrupt_record: bool
    pending_intents: tuple[int, ...]


def _sample(record):
    data = {field.name: record.get(field.name, field.default)
            if field.default is not MISSING else record[field.name]
            for field in fields(TelemetryRecord)}
    data["temperatures"] = tuple(TemperatureRecord(**item) for item in data["temperatures"])
    data["fan_rpm"] = tuple(data["fan_rpm"])
    if data["gpu_setter_evidence"] is not None:
        data["gpu_setter_evidence"] = GpuSetterEvidence(**data["gpu_setter_evidence"])
    return TelemetryRecord(**data)


def replay_run(path: Path, config: Config, *, gpu_evidence_mode="numeric_readback",
               gpu_setter_context=None) -> ReplayResult:
    evidence = inspect_run(path)
    policy = ShadowPolicy(config, gpu_evidence_mode=gpu_evidence_mode,
                          gpu_setter_context=gpu_setter_context)
    points = []
    previous_ns = None
    previous_phase = None
    explicit_prefill_seen = False
    explicit_loading_seen = False
    for row in evidence["records"]:
        if row.get("kind") != "sample":
            continue
        try:
            sample = _sample(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid recorded sample at sequence {row['seq']}") from exc
        mono_ns = sample.sample_mono_ns if sample.sample_mono_ns is not None else row["mono_ns"]
        dt_s = 0.5 if previous_ns is None else (mono_ns - previous_ns) / 1e9
        snapshot = Snapshot(
            temperatures=tuple(Temperature(t.sensor, t.celsius, t.age_s,
                                            t.slope_c_per_s)
                               for t in sample.temperatures),
            gpu_requested_max_mhz=sample.gpu_requested_mhz,
            gpu_accepted_max_mhz=sample.gpu_accepted_mhz,
            gpu_limit_age_s=sample.gpu_limit_age_s,
            gpu_measured_mhz=sample.gpu_measured_mhz,
            gpu_clock_age_s=sample.gpu_clock_age_s,
            available_memory_bytes=sample.available_memory_bytes,
            fan_healthy=sample.fan_healthy,
            cpu_actuator_healthy=sample.cpu_actuator_healthy,
            gpu_actuator_healthy=sample.gpu_actuator_healthy,
            workload_control_healthy=sample.workload_control_healthy,
            monotonic_s=mono_ns / 1e9, gpu_setter_evidence=sample.gpu_setter_evidence)
        prefill = sample.prefill_arrival
        if prefill is not None:
            explicit_prefill_seen = True
        elif not explicit_prefill_seen:
            prefill = sample.phase == "prefill" and previous_phase != "prefill"
        # After explicit tracking begins, None remains invalid to the policy:
        # a missing admission signal must not silently become "no arrival".
        loading = sample.model_loading
        if loading is not None:
            explicit_loading_seen = True
        elif not explicit_loading_seen:
            loading = False  # legacy trace; no lifecycle evidence supplied
        # Once explicit lifecycle tracking starts, missing data is a fault,
        # never an implicit readiness transition.
        limits = policy.step(PolicyInput(
            snapshot, sample.gpu_util_pct, dt_s,
            prefill_arrival=prefill,
            workload_done=sample.phase == "cooldown" and previous_phase != "cooldown",
            cpu_demand_active=sample.cpu_demand_active,
            cpu_work_arrival=sample.cpu_work_arrival is True,
            model_loading=loading))
        points.append(ReplayPoint(row["seq"], mono_ns / 1e9, limits))
        previous_ns, previous_phase = mono_ns, sample.phase
    return ReplayResult(tuple(points), evidence["clean_end"],
                        evidence["incomplete_tail"], evidence["corrupt_record"],
                        tuple(sorted(evidence["pending_intents"])))
