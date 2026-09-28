"""Timed stress-ng CPU load with a throughput metric, under the installed service.

For controller A/B runs (doc/48 §0): ``stress-ng --vecfp N`` (the retired
guard's tuning stressor, bogo ops/s comparable only within one build) for a
fixed time while energy_control keeps all protection (guard aborts at
93/85 C, policy, fan). The load is unowned from the service's point of view
(like cpu_step.py): it is killed within about half a second when the
service's readiness file disappears (abort or stop). Phases and the parsed
stress-ng metrics go to a durable JSONL log; nothing here writes an actuator.
Run by the root main agent under the hardware claim only.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

READINESS = Path("/run/spark-energy/entry-ceiling")
RUN_LOG = Path("/var/lib/spark-energy/cpu-load-runs.jsonl")
YAML_DIR = Path("/var/lib/spark-energy/cpu-load")


def phase(record):
    record = {"mono_ns": time.monotonic_ns(), "utc": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              **record}
    RUN_LOG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(RUN_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    print(json.dumps(record), flush=True)


def parse_metrics(path):
    """Minimal parser for stress-ng's --yaml metrics block (no YAML library)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    metrics, current = {}, None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- stressor:"):
            current = stripped.split(":", 1)[1].strip()
            metrics[current] = {}
        elif current and ":" in stripped and not stripped.startswith("-"):
            key, value = (part.strip() for part in stripped.split(":", 1))
            try:
                metrics[current][key] = float(value)
            except ValueError:
                pass
    return metrics or None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Timed stress-ng vecfp load for controller A/B")
    parser.add_argument("--label", required=True)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--method", default="vecfp", choices=("vecfp", "matrix"))
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise SystemExit("run as root under the hardware claim")
    if not 1 <= args.workers <= 20 or not 30 <= args.seconds <= 3600:
        raise SystemExit("1..20 workers and 30..3600 s")
    if not READINESS.exists():
        raise SystemExit("energy_control not ready (no entry-ceiling file)")
    YAML_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    yaml = YAML_DIR / f"{time.strftime('%Y%m%dT%H%M%S')}-{args.label}.yaml"
    command = ["stress-ng", f"--{args.method}", str(args.workers), "--timeout", f"{args.seconds}s",
               "--metrics-brief", "--yaml", str(yaml)]
    phase({"event": "start", "label": args.label, "workers": args.workers,
           "seconds": args.seconds, "method": args.method, "yaml": str(yaml)})
    process = subprocess.Popen(command, start_new_session=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reason = "completed"
    try:
        while process.poll() is None:
            if not READINESS.exists():
                reason = "service not ready: load killed"
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        reason = "interrupted"
        os.killpg(process.pid, signal.SIGKILL)
    finally:
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    metrics = parse_metrics(yaml)
    stressor = (metrics or {}).get(args.method) or {}
    phase({"event": "stop", "label": args.label, "reason": reason,
           "exit_code": process.returncode,
           "bogo_ops": stressor.get("bogo-ops"),
           "bogo_ops_per_s_real": stressor.get("bogo-ops-per-second-real-time"),
           "wall_s": stressor.get("wall-clock-time")})
    return 0 if reason == "completed" and process.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
