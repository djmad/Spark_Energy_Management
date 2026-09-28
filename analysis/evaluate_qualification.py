"""Evaluate SQ/TH qualification runs against the predeclared criteria (doc/46).

Reads the service's 1 Hz trace for a UTC window (the run's load period) and
the service journal for aborts. No device I/O. The 2 s trend of the hottest
ACPI zone is recomputed from the raw 1 Hz samples (least squares over the
last 3 samples) as a conservative stand-in for the policy's trend.
"""
import argparse
import glob
import json
import subprocess
from collections import Counter

TRACES = "/var/lib/spark-energy/traces/*.jsonl"


def rows_between(start_utc_s, end_utc_s, traces=TRACES):
    rows = []
    for path in sorted(glob.glob(traces)):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if start_utc_s <= r["utc_ns"] / 1e9 <= end_utc_s:
                rows.append(r)
    return sorted(rows, key=lambda r: r["utc_ns"])


def trend(values):
    n = len(values)
    if n < 3:
        return values[-1]
    xs = range(n)
    mx, my = (n - 1) / 2, sum(values) / n
    b = sum((x - mx) * (y - my) for x, y in zip(xs, values)) / sum((x - mx) ** 2 for x in xs)
    return my + b * (n - 1 - mx)


def journal_aborts(start_utc_s, end_utc_s):
    try:
        out = subprocess.run(["journalctl", "-u", "energy_control", "--no-pager", "-o", "cat",
                              f"--since=@{int(start_utc_s)}", f"--until=@{int(end_utc_s) + 5}"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return [line for line in out.splitlines() if "abort" in line.lower()]


def evaluate(rows, *, gpu_target, cpu_target, preferred_fan, steady_after_s=300, kind="SQ",
             aborts=None):
    if not rows:
        raise ValueError("no trace rows in window")
    t0 = rows[0]["utc_ns"] / 1e9
    steady = [r for r in rows if r["utc_ns"] / 1e9 - t0 >= steady_after_s]
    hottest = [max(r["acpi_c"].values()) for r in rows]
    trends = [trend(hottest[max(0, i - 2):i + 1]) for i in range(len(rows))]
    steady_trends = trends[len(rows) - len(steady):]
    gpu = [r["gpu_c"] for r in steady]
    gen = [r["vllm_gen_tokens"] for r in rows if r.get("vllm_gen_tokens") is not None]
    span_s = (rows[-1]["utc_ns"] - rows[0]["utc_ns"]) / 1e9
    report = {
        "kind": kind, "samples": len(rows), "steady_samples": len(steady),
        "duration_s": round(span_s),
        "aborts_in_journal": aborts,
        "gpu_max_c": max(gpu), "gpu_mean_c": round(sum(gpu) / len(gpu), 1),
        "acpi_raw_max_c": max(hottest), "acpi_trend_max_c": round(max(steady_trends), 1),
        "acpi_trend_mean_c": round(sum(steady_trends) / len(steady_trends), 1),
        "modes": dict(Counter(r["mode"] for r in steady)),
        "gpu_cap_at_max_share": round(sum(1 for r in steady if r["gpu_cap_mhz"] >= 1800)
                                      / len(steady), 3),
        "fan_at_or_below_preferred_share": round(
            sum(1 for r in steady if (r["fan_floor"] or 0) <= preferred_fan) / len(steady), 3),
        "aggregate_gen_tok_s": round((gen[-1] - gen[0]) / span_s, 1) if len(gen) > 1 else None,
        "zone_means_c": {z.replace("acpi_", ""): round(sum(r["acpi_c"][z] for r in steady)
                                                        / len(steady), 1)
                         for z in steady[0]["acpi_c"]},
    }
    checks = {"no_abort": aborts is not None and not aborts}
    if kind == "SQ":
        checks["gpu_le_target_plus_2"] = report["gpu_max_c"] <= gpu_target + 2
        checks["acpi_trend_le_target_plus_2"] = report["acpi_trend_max_c"] <= cpu_target + 2
        checks["gpu_at_max_ge_80pct"] = report["gpu_cap_at_max_share"] >= 0.8
        checks["fan_le_preferred_ge_80pct"] = report["fan_at_or_below_preferred_share"] >= 0.8
    else:
        within = lambda xs, t, band: sum(1 for x in xs if x <= t + band) / len(xs)
        checks["gpu_within_3_ge_90pct"] = within(gpu, gpu_target, 3) >= 0.9
        checks["gpu_never_above_6"] = max(gpu) <= gpu_target + 6
        checks["cpu_trend_within_3_ge_90pct"] = within(steady_trends, cpu_target, 3) >= 0.9
        checks["cpu_trend_never_above_6"] = max(steady_trends) <= cpu_target + 6
        caps = [r["gpu_cap_mhz"] for r in steady]
        report["gpu_cap_range_mhz"] = (min(caps), max(caps))
    report["checks"] = checks
    report["pass"] = all(checks.values())
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate SQ/TH runs (doc/46)")
    parser.add_argument("--start", type=float, required=True, help="load start, UTC seconds")
    parser.add_argument("--end", type=float, required=True, help="load end, UTC seconds")
    parser.add_argument("--kind", choices=("SQ", "TH"), default="SQ")
    parser.add_argument("--gpu-target", type=float, default=75.0)
    parser.add_argument("--cpu-target", type=float, default=90.0)
    parser.add_argument("--preferred-fan", type=int, default=6)
    args = parser.parse_args(argv)
    rows = rows_between(args.start, args.end)
    print(json.dumps(evaluate(rows, gpu_target=args.gpu_target, cpu_target=args.cpu_target,
                              preferred_fan=args.preferred_fan, kind=args.kind,
                              aborts=journal_aborts(args.start, args.end)), indent=2))


if __name__ == "__main__":
    main()
