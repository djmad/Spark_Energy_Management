"""Evaluate one GPU ladder step (doc/52) from the 1 Hz service trace.

A step run is LLM load from ``start`` with CPU workers added at ``cpu_at_s``.
Windows: LLM only (60 s after start to the CPU step) and combined (60 s after
the CPU step to its end). Reports throughput, clocks (cap, measured, share of
time at the step maximum, deficit while busy at it), GPU power and
temperature, CPU clusters, hottest ACPI zone, projection and vendor flags.

    python3 -m analysis.gpu_ladder --start 2026-09-27T17:25:24+02:00 --step 2100 \
        --cpu-label ladder2100-20w
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics as st

TRACE_DIR = Path("/var/lib/spark-energy/traces")
CPU_RUNS = Path("/var/lib/spark-energy/cpu-load-runs.jsonl")
# The trace lists cpufreq policies in lexicographic order (policy0, policy1,
# policy10, ...); map CPU numbers to trace positions.
ORDER = sorted(range(20), key=lambda i: f"policy{i}")
POS = {cpu: index for index, cpu in enumerate(ORDER)}
CLUSTERS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}


def rows_between(start_s, end_s, trace_dir=TRACE_DIR):
    day = datetime.fromtimestamp(start_s).strftime("%Y%m%d")
    rows = []
    with open(Path(trace_dir) / f"{day}.jsonl", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if start_s <= row["utc_ns"] / 1e9 <= end_s:
                    rows.append(row)
    return rows


def summarise(rows, step_mhz):
    if len(rows) < 2:
        return None
    gen = [r.get("vllm_gen_tokens") for r in rows if r.get("vllm_gen_tokens") is not None]
    span_s = (rows[-1]["utc_ns"] - rows[0]["utc_ns"]) / 1e9
    at_max = [r for r in rows if r["gpu_cap_mhz"] >= step_mhz]
    busy_at_max = [r for r in at_max if (r.get("gpu_util_pct") or 0) >= 80]
    control = [r.get("control") or {} for r in rows]
    projections = [c["cpu_projection_max_1s_c"] for c in control
                   if c.get("cpu_projection_max_1s_c") is not None]
    hottest = [max(r["acpi_c"].values()) for r in rows]
    return {
        "samples": len(rows),
        "tokens_per_s": round((gen[-1] - gen[0]) / span_s, 1) if len(gen) > 1 else None,
        "gpu_cap_mean_mhz": round(st.mean(r["gpu_cap_mhz"] for r in rows)),
        "gpu_measured_mean_mhz": round(st.mean(r["gpu_mhz"] for r in rows)),
        "share_at_step_max": round(len(at_max) / len(rows), 3),
        "busy_at_max_deficit_mean_mhz": (round(st.mean(r["gpu_cap_mhz"] - r["gpu_mhz"]
                                                       for r in busy_at_max), 1)
                                         if busy_at_max else None),
        "gpu_w_mean": round(st.mean(r["gpu_w"] for r in rows), 1),
        "gpu_w_max": round(max(r["gpu_w"] for r in rows), 1),
        "gpu_c_mean": round(st.mean(r["gpu_c"] for r in rows), 1),
        "gpu_c_max": max(r["gpu_c"] for r in rows),
        "cpu_measured_mhz": {name: round(st.mean(st.mean(r["cpu_mhz"][POS[c]] for c in cpus)
                                                 for r in rows))
                             for name, cpus in CLUSTERS.items()},
        "hottest_mean_c": round(st.mean(hottest), 1),
        "hottest_max_c": max(hottest),
        "projection_max_c": round(max(projections), 1) if projections else None,
        "vendor_rows": sum(1 for r in rows if r.get("vendor_throttle")),
        "modes": {mode: sum(1 for r in rows if r["mode"] == mode)
                  for mode in sorted({r["mode"] for r in rows})},
    }


def cpu_result(label, path=CPU_RUNS):
    result = None
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                record = json.loads(line)
                if record.get("label") == label and record.get("event") == "stop":
                    result = record
    except FileNotFoundError:
        return None
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", required=True, help="load start, ISO 8601 with offset")
    parser.add_argument("--step", type=int, required=True, help="GPU maximum under test, MHz")
    parser.add_argument("--cpu-at-s", type=int, default=180)
    parser.add_argument("--cpu-seconds", type=int, default=300)
    parser.add_argument("--cpu-label")
    args = parser.parse_args(argv)
    start = datetime.fromisoformat(args.start).timestamp()
    step_at = start + args.cpu_at_s
    report = {
        "step_mhz": args.step,
        "llm_only": summarise(rows_between(start + 60, step_at), args.step),
        "combined": summarise(rows_between(step_at + 60, step_at + args.cpu_seconds), args.step),
        "whole_run": summarise(rows_between(start, step_at + args.cpu_seconds), args.step),
    }
    if args.cpu_label:
        record = cpu_result(args.cpu_label)
        report["cpu_bogo_ops_per_s"] = (record or {}).get("bogo_ops_per_s_real")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
