"""Publish dashboard status from trial traces while energy_control is stopped.

Trials stop the service, which is the only writer of
``/run/spark-energy/status.json``. Trial runners installed before 27 September
2026 do not publish status themselves, so this bridge converts the newest row
of the trial's own 4 Hz trace into the same status format. It reads files
only (no hardware), writes only while the service is inactive and the trace is
fresh, and exits when the watched trial process ends.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from energy_control.safety import ABORT_C, GPU_ABORT_C  # noqa: E402
from energy_control.service import STATUS_PATH, write_readiness  # noqa: E402

TRIAL_TRACES = Path("/var/lib/spark-energy/trial-traces")
FAST_CPUS = set(range(5, 10)) | set(range(15, 20))


def service_active():
    result = subprocess.run(["systemctl", "is-active", "--quiet", "energy_control.service"],
                            timeout=2, check=False)
    return result.returncode == 0


def newest_trace(max_age_s):
    candidates = [p for p in TRIAL_TRACES.glob("*/*.jsonl")]
    if not candidates:
        return None
    path = max(candidates, key=lambda p: p.stat().st_mtime)
    return path if time.time() - path.stat().st_mtime <= max_age_s else None


def last_row(path):
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - 8192))
        lines = handle.read().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def payload(row, trial):
    mhz = row.get("cpu_mhz") or []
    fast = [m for i, m in enumerate(mhz) if i in FAST_CPUS]
    slow = [m for i, m in enumerate(mhz) if i not in FAST_CPUS]
    caps = row.get("cpu_caps_mhz") or (None, None)
    cap = row.get("gpu_cap_mhz")
    return {"utc_ns": row["utc_ns"], "mono_ns": row["mono_ns"], "run_id": trial,
            "mode": f"trial:{trial}:{row.get('mode')}", "reason": "trial trace bridge",
            "gpu": {"cap_mhz": cap, "measured_mhz": row.get("gpu_mhz"),
                    "temp_c": row.get("gpu_c"), "power_w": row.get("gpu_w"),
                    "util_pct": row.get("gpu_util_pct")},
            "cpu": {"caps_mhz": {"slow": caps[0], "fast": caps[1]},
                    "util_pct": row.get("cpu_util_pct"),
                    "p_mhz": sum(fast) / len(fast) if fast else None,
                    "e_mhz": sum(slow) / len(slow) if slow else None},
            "fan": {"floor": row.get("fan_floor"), "rpm": list(row.get("fan_rpm") or [])},
            "zones_c": {k.replace("acpi_", ""): v for k, v in (row.get("acpi_c") or {}).items()},
            "board_c": {"nvme": row.get("nvme_c"), "wifi": row.get("wifi_c")},
            "limits": {"acpi_abort_c": ABORT_C, "gpu_abort_c": GPU_ABORT_C,
                       "cpu_target_c": 90.0, "gpu_target_c": 75.0,
                       "gpu_entry_mhz": cap, "gpu_max_mhz": cap}}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pid", type=int, required=True, help="trial process to follow")
    parser.add_argument("--max-age-s", type=float, default=3.0)
    args = parser.parse_args()
    last_written = None
    while True:
        try:
            os.kill(args.pid, 0)
        except ProcessLookupError:
            return 0
        try:
            trace = None if service_active() else newest_trace(args.max_age_s)
            row = last_row(trace) if trace else None
            if row and row.get("utc_ns") != last_written:
                write_readiness(STATUS_PATH, payload(row, trace.parent.name))
                last_written = row["utc_ns"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            print(f"bridge: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
