"""Per-cluster CPU step identification under the installed service's control.

For each cluster (GB10: P0 = CPUs 5-9, P1 = 15-19, E0 = 0-4, E1 = 10-14) run
busy loops pinned to its five cores for ``--load-s``, then rest ``--rest-s``.
The service keeps all protection (CPU PID, guard aborts at 93/85 C, fan
staging) and its 1 Hz trace records temperatures, clocks, utilisation and
fan speed; this script only adds a phase log with monotonic timestamps. The
load is unowned from the service's point of view (like sustained_load.py):
it stops within about a second when energy_control's readiness file
disappears. Synthetic busy loops only. Run by the root main agent under the
hardware claim, never while a trial runner holds the actuators.
"""
import argparse
import json
from multiprocessing import get_context
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

READINESS = Path("/run/spark-energy/entry-ceiling")
PHASE_LOG = Path("/var/lib/spark-energy/cpu-step-runs.jsonl")
CLUSTERS = {"P0": (5, 6, 7, 8, 9), "P1": (15, 16, 17, 18, 19),
            "E0": (0, 1, 2, 3, 4), "E1": (10, 11, 12, 13, 14)}


def busy(cpu, stop):
    os.sched_setaffinity(0, {cpu})
    while not stop.is_set():
        for _ in range(20000):
            pass


def phase(record):
    record = {"mono_ns": time.monotonic_ns(), "utc": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              **record}
    with open(PHASE_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def wait(seconds):
    """Sleep while the service stays ready; False if its readiness vanished."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not READINESS.exists():
            return False
        time.sleep(0.25)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--clusters", nargs="+", default=list(CLUSTERS), choices=list(CLUSTERS))
    parser.add_argument("--load-s", type=float, default=120.0)
    parser.add_argument("--rest-s", type=float, default=180.0)
    parser.add_argument("--settle-s", type=float, default=120.0)
    args = parser.parse_args()
    if not 10 <= args.load_s <= 600 or not 30 <= args.rest_s <= 900:
        raise SystemExit("load 10..600 s, rest 30..900 s")
    if not READINESS.exists():
        raise SystemExit("energy_control is not running (no readiness file)")
    context = get_context("spawn")
    run = time.strftime("%Y%m%dT%H%M%S")
    phase({"run": run, "event": "start", "clusters": args.clusters,
           "load_s": args.load_s, "rest_s": args.rest_s})
    reason = "completed"
    if not wait(args.settle_s):
        reason = "service readiness vanished"
    for name in args.clusters if reason == "completed" else ():
        stop = context.Event()
        workers = [context.Process(target=busy, args=(cpu, stop), daemon=True)
                   for cpu in CLUSTERS[name]]
        phase({"run": run, "event": "load", "cluster": name, "cpus": CLUSTERS[name]})
        for worker in workers:
            worker.start()
        ok = wait(args.load_s)
        stop.set()
        for worker in workers:
            worker.join(3)
            if worker.is_alive():
                worker.kill()
        phase({"run": run, "event": "rest", "cluster": name})
        if not ok or not wait(args.rest_s):
            reason = "service readiness vanished"
            break
    phase({"run": run, "event": "end", "reason": reason})
    print(json.dumps({"run": run, "result": reason}), flush=True)
    from energy_control.markers import write_marker
    write_marker("cpu-step", "completed" if reason == "completed" else "stopped", run=run,
                 reason=reason)


if __name__ == "__main__":
    main()
