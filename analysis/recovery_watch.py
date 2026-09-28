"""One-shot supervised recovery watch. Never launches models or writes clocks.

The operator starts through the existing manager API after WATCH_READY. This
is an operational recovery aid, not qualification of the production controller.
Only the first observed vllm_node identity may be stopped on a fault. No retry.
"""
import argparse
import json
import os
import subprocess
import time
from http.client import HTTPConnection

from energy_control.host_sampler import HostSamplerProcess
from energy_control.llm_container import inspect_llm_container


def safety_decision(sample, previous=None):
    """Explain the unchanged recovery boundaries using acquisition timestamps."""
    readings = dict(sample.acpi_temperatures, gpu=sample.gpu.temperature_c)
    details = dict(sample_start_ns=sample.start_mono_ns, sample_end_ns=sample.end_mono_ns,
                   temperatures=readings, measured_gpu_mhz=sample.gpu.measured_mhz,
                   available_memory_bytes=sample.available_memory_bytes,
                   fan_healthy=sample.fan.healthy, fan_floor=sample.fan.floor_state)
    for reason, condition in (
        ("absolute_temperature", max(readings.values()) >= 80),
        ("memory", sample.available_memory_bytes < 8 * 1024**3),
        ("fan", not sample.fan.healthy or sample.fan.floor_state != 12),
        ("gpu_clock", sample.gpu.measured_mhz > 1800),
    ):
        if condition:
            return dict(details, reason=reason)
    if previous is not None:
        old = dict(previous.acpi_temperatures, gpu=previous.gpu.temperature_c)
        delta = (sample.end_mono_ns - previous.end_mono_ns) / 1e9
        if readings.keys() != old.keys() or not 0 < delta <= 2:
            return dict(details, reason="sensor_identity_or_timing", delta_s=delta)
        if delta >= 1:
            for name, value in readings.items():
                slope = max(0, (value - old[name]) / delta)
                projected = value + 2 * slope
                if projected >= 88:
                    return dict(details, reason="predicted_temperature", sensor=name,
                                previous_end_ns=previous.end_mono_ns,
                                previous_c=old[name], delta_s=delta,
                                rise_c_per_s=slope, projected_c=projected,
                                horizon_s=2, boundary_c=88)
    return None


def unsafe(sample, previous=None):
    return safety_decision(sample, previous) is not None


def ready():
    connection = HTTPConnection("127.0.0.1", 8000, timeout=0.2)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status == 200
    except (OSError, TimeoutError):
        return False
    finally:
        connection.close()


def watch(path):
    if os.geteuid() != 0:
        raise PermissionError("root recovery supervision required")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    source = HostSamplerProcess()
    pinned = None
    started = time.monotonic()
    previous = None
    last_record = last_identity = 0
    healthy_since = None
    announced = False
    abort_decision = None
    def record(kind, **fields):
        data = (json.dumps(dict(kind=kind, utc_ns=time.time_ns(),
                  monotonic_ns=time.monotonic_ns(), **fields), allow_nan=False) + "\n").encode()
        if len(data) > 4096:
            raise ValueError("oversized record")
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("log write failed")
            view = view[count:]
        os.fdatasync(fd)
    try:
        source.start()
        record("recovery_watch", deadline_s=720, abort_c=80, predicted_abort_c=88,
               hard_gpu_max_mhz=1800, requested_gpu_cap_mhz=None)
        while time.monotonic() - started < 720:
            sample = source.read()
            now = time.monotonic()
            if sample is None:
                if not announced and now - started < 2:
                    time.sleep(0.05)
                    continue
                raise RuntimeError("sensor stream unavailable")
            if previous is None or sample.end_mono_ns != previous.end_mono_ns:
                abort_decision = safety_decision(sample, previous)
                if abort_decision is not None:
                    # Retain in memory; do not wait for disk before signalling.
                    raise RuntimeError("recovery safety boundary")
                if previous is None or sample.end_mono_ns - previous.end_mono_ns >= 1_000_000_000:
                    previous = sample
            if not announced:
                if sample.available_memory_bytes < 12 * 1024**3:
                    raise RuntimeError("insufficient startup memory")
                print("WATCH_READY", flush=True)
                announced = True
            if now - last_record >= 1:
                record("sample", temperatures=dict(sample.acpi_temperatures,
                       gpu=sample.gpu.temperature_c), measured_gpu_mhz=sample.gpu.measured_mhz,
                       available_memory_bytes=sample.available_memory_bytes)
                last_record = now
            if now - last_identity >= 1:
                try:
                    identity = inspect_llm_container()
                except RuntimeError:
                    if pinned is not None:
                        raise RuntimeError("pinned container disappeared")
                else:
                    if pinned is None:
                        if identity.status != "running" or identity.restart_policy != "no":
                            raise RuntimeError("unexpected container state")
                        pinned = identity
                        record("container_bound", id=pinned.id, pid=pinned.pid)
                    elif identity != pinned:
                        raise RuntimeError("container identity changed")
                last_identity = now
                if pinned is not None and ready():
                    healthy_since = healthy_since or now
                    if now - healthy_since >= 30:
                        record("recovery_ready", legacy_protection_retained=True,
                               production_controller_qualified=False)
                        print("LLM_READY_30_SECONDS", flush=True)
                        return
                else:
                    healthy_since = None
            time.sleep(0.05)
        raise RuntimeError("startup observation deadline exceeded")
    except BaseException as exc:
        # Protection precedes disk recording. Never target a replacement by name.
        outcome = "no_bound_container"
        if pinned is not None:
            try:
                result = subprocess.run(["/usr/bin/docker", "kill", "--signal=KILL", pinned.id],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=2, check=False)
                outcome = "signal_acknowledged" if result.returncode == 0 else "unverified"
            except Exception:
                outcome = "unverified"
        record("recovery_abort", error=type(exc).__name__, stop_outcome=outcome,
               residual_gpu_work_verified=False, decision=abort_decision)
        raise
    finally:
        source.close()
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervise-recovery", action="store_true", required=True)
    parser.add_argument("--new-log", required=True)
    args = parser.parse_args()
    watch(args.new_log)
