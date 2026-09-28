"""Evaluate the GPU matrix-multiplication frequency sweep (doc/53).

Per step (``/var/lib/spark-energy/sweep-<MHz>-*``): measured clock, GPU
power (mean over the last 60 s, maximum), nvidia sensor and ACPI TGPU
(end and maximum), the TGPU rise in the first 20 s of load, the copper
proxy and TFLOPS from the burn-in's own log. Then it fits the dynamic
power, P - idle = k x (f / 1000) ^ a, on the measured clock.

    python3 -m analysis.gpu_sweep
"""
from datetime import datetime
import json
from math import exp, log
from pathlib import Path
import re
import statistics as st

DIR = Path("/var/lib/spark-energy")
TRACE_DIR = DIR / "traces"
IDLE_W = 4.5


def trace_rows(start_s, end_s):
    day = datetime.fromtimestamp(start_s).strftime("%Y%m%d")
    with open(TRACE_DIR / f"{day}.jsonl", encoding="utf-8") as handle:
        return [row for row in map(json.loads, handle)
                if start_s <= row["utc_ns"] / 1e9 <= end_s]


def step_summary(mhz):
    events = [json.loads(line) for line in open(DIR / f"sweep-{mhz}-events.jsonl")]
    start = datetime.strptime(events[0]["utc"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    stop = datetime.strptime(events[-1]["utc"], "%Y-%m-%dT%H:%M:%S%z").timestamp()
    rows = trace_rows(start, stop)
    loaded = [r for r in rows if r["gpu_util_pct"] >= 50]
    if len(loaded) < 10:
        return None
    t0 = loaded[0]["utc_ns"] / 1e9
    last = [r for r in loaded if r["utc_ns"] / 1e9 >= stop - 60]
    first20 = [r for r in loaded if r["utc_ns"] / 1e9 <= t0 + 20]
    tgpu = lambda r: r["acpi_c"].get("acpi_TGPU")
    log_text = (DIR / f"sweep-{mhz}-gpu.log").read_text(errors="replace")
    tflops = [float(x) for x in re.findall(r"\|\s+([\d.]+) TFLOPS", log_text)]
    errors = re.findall(r"Fehler: (\d+)", log_text)
    return {
        "step_mhz": mhz, "reason": events[-1].get("reason"),
        "measured_mhz": round(st.mean(r["gpu_mhz"] for r in last)),
        "power_w_last60": round(st.mean(r["gpu_w"] for r in last), 1),
        "power_w_max": round(max(r["gpu_w"] for r in loaded), 1),
        "nvidia_end_c": last[-1]["gpu_c"], "nvidia_max_c": max(r["gpu_c"] for r in loaded),
        "tgpu_end_c": tgpu(last[-1]), "tgpu_max_c": max(tgpu(r) for r in loaded),
        "tgpu_rise_20s_c": round(tgpu(first20[-1]) - tgpu(first20[0]), 1),
        "tgpu_minus_nvidia_c": round(st.mean(tgpu(r) - r["gpu_c"] for r in last), 1),
        "tunc_end_c": last[-1]["acpi_c"].get("acpi_TUNC"),
        "tflops_mean": round(st.mean(tflops[1:]), 1) if len(tflops) > 1 else None,
        "compute_errors": int(errors[-1]) if errors else None,
    }


def fit(points):
    """Least squares of log(P - idle) on log(f / 1000)."""
    xs = [log(f / 1000.0) for f, _ in points]
    ys = [log(max(p - IDLE_W, 0.1)) for _, p in points]
    mx, my = st.mean(xs), st.mean(ys)
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    return round(exp(my - a * mx), 3), round(a, 3)


def main():
    steps = sorted(int(p.name.split("-")[1]) for p in DIR.glob("sweep-*-events.jsonl"))
    results = [r for r in (step_summary(mhz) for mhz in steps) if r]
    for r in results:
        print(json.dumps(r))
    points = [(r["measured_mhz"], r["power_w_last60"]) for r in results]
    if len(points) >= 2:
        k, a = fit(points)
        print(json.dumps({"fit": "P = 4.5 + k x (f/1000)^a", "k_w": k, "a": a}))


if __name__ == "__main__":
    main()
