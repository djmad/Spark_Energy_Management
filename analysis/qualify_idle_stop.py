"""Explicit, one-shot IDLE container-stop check; no load or clock commands.

Requires the operator's exclusive-window and LLM stop authorization. This only
qualifies the stop adapter, not the independent guard or residual GPU drain.
Leaves the LLM stopped. Re-running requires a separate reviewed attempt.
"""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
from time import monotonic, monotonic_ns, time_ns

from energy_control.collector import LenovoReadOnlyCollector, VllmQueueTelemetry
from energy_control.llm_container import ExclusiveLlmContainer, inspect_llm_container


def qualify(path):
    if os.geteuid() != 0:
        raise PermissionError("root-owned stop qualification log required")
    collector = LenovoReadOnlyCollector(queue=VllmQueueTelemetry())
    baseline = collector.collect()
    identity = inspect_llm_container()
    if identity.auto_remove is not False:
        raise RuntimeError("auto-removing containers need a separately qualified disappearance verifier")
    hottest = max(baseline.gpu.temperature_c, *(value for _, value in baseline.acpi_temperatures))
    if (baseline.active_jobs != 0 or baseline.queued_jobs != 0 or hottest >= 60
            or baseline.gpu.measured_mhz > 1800 or not baseline.fan.healthy
            or baseline.fan.floor_state != 12 or baseline.available_memory_bytes < 12 * 1024**3):
        raise RuntimeError("idle-stop preflight failed; no stop issued")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    sequence = 0
    def record(kind, **values):
        nonlocal sequence
        sequence += 1
        data = (json.dumps({"sequence": sequence, "utc_ns": time_ns(), "mono_ns": monotonic_ns(),
                           "kind": kind, **values}, allow_nan=False) + "\n").encode()
        if len(data) > 8192:
            raise ValueError("qualification record too large")
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("qualification log write failed")
            view = view[count:]
        os.fdatasync(fd)
    try:
        record("idle_stop_intent", container=asdict(identity), hottest_c=hottest,
               gpu_c=baseline.gpu.temperature_c, gpu_measured_mhz=baseline.gpu.measured_mhz,
               active=0, waiting=0, fan_floor=baseline.fan.floor_state,
               scope="operator_exclusive_window", starts_or_clock_writes=False)
        def exclusive():
            return VllmQueueTelemetry().read() == (0, 0)
        adapter = ExclusiveLlmContainer(identity, exclusive_window=exclusive, enable_stop=True)
        started = monotonic()
        try:
            stopped = adapter.emergency_stop()
        except Exception as exc:
            record("stop_unverified", error_class=type(exc).__name__, elapsed_s=monotonic()-started)
            raise
        record("stop_result", processes_stopped=stopped, elapsed_s=monotonic()-started,
               residual_gpu_work_verified=False)
        # Observations are explicitly not a claim of GPU drain.
        after = LenovoReadOnlyCollector().collect()
        final = inspect_llm_container(identity.id)
        report = {"processes_stopped": stopped and final.status == "exited" and final.pid == 0,
                  "container_status": final.status, "gpu_c": after.gpu.temperature_c,
                  "hottest_acpi_c": max(value for _, value in after.acpi_temperatures),
                  "residual_gpu_work_verified": False, "llm_restarted": False}
        record("idle_stop_observation", **report)
        return report
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-idle-stop", action="store_true", required=True)
    parser.add_argument("--new-log", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(qualify(args.new_log), indent=2))
