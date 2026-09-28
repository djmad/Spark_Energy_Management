"""Fit the calorimetric CPU power model (scripts/calibrate_power.sh, doc/55).

The GPU is the reference heater: its power is measured (nvidia-smi W). With
the CPU idle, each GPU block's steady copper temperature against the total
heat gives the copper-to-ambient line. There is no copper sensor; the copper
is estimated as

    copper ~= nvidia sensor - R_GPU x P_gpu        (R_GPU = 1 / 1.9 C/W, doc/44)

and cross-checked with TUNC. In the CPU blocks the extra copper rise beyond
the (idle) GPU heat is the CPU's heat. From P cores alone, E cores alone, all
cores, and P cores at lowered maxima, it derives the full-load watts per
cluster class at the measured clocks and the clock exponent for
energy_control/power_estimate.py.

    python3 -m analysis.calibrate_power
"""
from datetime import datetime
import json
from math import log
from pathlib import Path
import statistics as st

DIR = Path("/var/lib/spark-energy")
R_GPU = 1 / 1.9
ORDER = sorted(range(20), key=lambda i: f"policy{i}")
POS = {cpu: index for index, cpu in enumerate(ORDER)}
CLUSTERS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}


def blocks(path=DIR / "calibration-events.jsonl"):
    events = [json.loads(line) for line in open(path)]
    found, open_blocks = [], {}
    for event in events:
        if event.get("event") != "block":
            continue
        key = event["label"]
        t = datetime.fromisoformat(event["utc"]).timestamp()
        if event["phase"] == "start":
            open_blocks[key] = (event["kind"], t)
        elif key in open_blocks:
            kind, start = open_blocks.pop(key)
            found.append({"label": key, "kind": kind, "start": start, "end": t,
                          "ok": event.get("reason", "completed") == "completed"})
    return found


def steady(rows, block, tail_s=120):
    window = [r for r in rows if block["end"] - tail_s <= r["utc_ns"] / 1e9 <= block["end"]]
    if len(window) < 30:
        return None
    p_gpu = st.mean(r["gpu_w"] for r in window)
    nv = st.mean(r["gpu_c"] for r in window)
    measured = {name: st.mean(st.mean(r["cpu_mhz"][POS[c]] for c in cpus) for r in window)
                for name, cpus in CLUSTERS.items()}
    util = {name: st.mean((r.get("cluster_util_pct") or {}).get(name) or 0 for r in window)
            for name in CLUSTERS}
    est = [r.get("cpu_est_w") for r in window if r.get("cpu_est_w") is not None]
    return {"p_gpu": p_gpu, "copper": nv - R_GPU * p_gpu,
            "tunc": st.mean(r["acpi_c"].get("acpi_TUNC") for r in window),
            "clusters_mhz": measured, "util": util, "model_est_w": st.mean(est) if est else None}


def main():
    day = datetime.now().strftime("%Y%m%d")
    rows = [json.loads(line) for line in open(DIR / "traces" / f"{day}.jsonl")]
    found = [b for b in blocks() if b["ok"]]
    points = {b["label"]: (b, steady(rows, b)) for b in found}
    cal = [(s["p_gpu"], s["copper"]) for b, s in points.values()
           if s and b["kind"] in ("gpu",) or (s and b["label"] == "baseline")]
    if len(cal) < 2:
        print("not enough calibration blocks")
        return
    xs, ys = zip(*cal)
    mx, my = st.mean(xs), st.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in cal) / sum((x - mx) ** 2 for x in xs)
    offset = my - slope * mx
    print(json.dumps({"copper_fit": {"offset_c": round(offset, 2), "c_per_w": round(slope, 4),
                                     "conductance_w_per_k": round(1 / slope, 2),
                                     "points": [(round(x, 1), round(y, 2)) for x, y in cal]}}))
    baseline = points.get("baseline", (None, None))[1]
    results = {}
    for label, (block, s) in points.items():
        if not s or block["kind"] != "cpu":
            continue
        total = (s["copper"] - offset) / slope            # heat above the fit's zero
        cpu_w = total - s["p_gpu"]
        idle_cpu = 0.0
        if baseline:
            idle_cpu = (baseline["copper"] - offset) / slope - baseline["p_gpu"]
        results[label] = dict(cpu_w_above_idle=round(cpu_w - idle_cpu, 1),
                              clusters_mhz={k: round(v) for k, v in s["clusters_mhz"].items()},
                              util={k: round(v) for k, v in s["util"].items()},
                              model_est_w=s["model_est_w"])
        print(json.dumps({label: results[label]}))
    p, e = results.get("p-cores"), results.get("e-cores")
    low = results.get("p-cores-2600")
    if p and e:
        p_mhz = st.mean([p["clusters_mhz"]["P0"], p["clusters_mhz"]["P1"]])
        e_mhz = st.mean([e["clusters_mhz"]["E0"], e["clusters_mhz"]["E1"]])
        fit = {"p_cluster_full_w_at_measured": round(p["cpu_w_above_idle"] / 2, 2), "p_mhz": round(p_mhz),
               "e_cluster_full_w_at_measured": round(e["cpu_w_above_idle"] / 2, 2), "e_mhz": round(e_mhz)}
        if low and low["cpu_w_above_idle"] > 0 and p["cpu_w_above_idle"] > 0:
            low_mhz = st.mean([low["clusters_mhz"]["P0"], low["clusters_mhz"]["P1"]])
            if abs(low_mhz - p_mhz) > 100:
                fit["exponent"] = round(log(p["cpu_w_above_idle"] / low["cpu_w_above_idle"])
                                        / log(p_mhz / low_mhz), 2)
        print(json.dumps({"model_fit": fit}))


if __name__ == "__main__":
    main()
