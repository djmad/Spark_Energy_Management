"""Evaluate the temperature-ceiling block (scripts/temp_raise.sh, doc/55).

Per step (from temp-raise-steps.jsonl), over the last 180 s of each 300 s step,
from the 1 Hz service trace: GPU cap and measured clock, power, the nvidia
sensor and TGPU, the hottest CPU zone, per-cluster measured clocks and caps,
the worst 1 s guard projection, and the vendor's own regulation. The vendor
signals are the vendor watch (a measured clock under a steady cap while
busy) and the GPU clock event reasons.

    python3 -m analysis.temp_raise
"""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import statistics as st

DIR = Path("/var/lib/spark-energy")
ORDER = sorted(range(20), key=lambda i: f"policy{i}")
POS = {cpu: index for index, cpu in enumerate(ORDER)}
CLUSTERS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}
REASONS = {0x4: "sw_power_cap", 0x8: "hw_slowdown", 0x20: "sw_thermal", 0x40: "hw_thermal",
           0x80: "hw_power_brake"}


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def main(label="temp-raise", tail_s=180):
    steps = [json.loads(line) for line in open(DIR / f"{label}-steps.jsonl")]
    marks = [s for s in steps if s.get("event") == "step"]
    end = next((ts(s["utc"]) for s in steps if s.get("event") == "block_end"), None)
    day = datetime.fromtimestamp(ts(marks[0]["utc"])).strftime("%Y%m%d")
    rows = [json.loads(line) for line in open(DIR / "traces" / f"{day}.jsonl")]
    for index, mark in enumerate(marks):
        start = ts(mark["utc"])
        stop = ts(marks[index + 1]["utc"]) if index + 1 < len(marks) else (end or start + 300)
        window = [r for r in rows if max(start, stop - tail_s) <= r["utc_ns"] / 1e9 <= stop]
        if len(window) < 5:
            continue
        acpi = lambda r, zone: r["acpi_c"].get(f"acpi_{zone}")
        cpu_zones = ("TS0P", "TS1P", "TS0E", "TS1E", "TSOC", "TUNC")
        projections = [(r.get("control") or {}).get("cpu_projection_max_1s_c") for r in window]
        projections = [p for p in projections if p is not None]
        reasons = Counter()
        for r in window:
            mask = r.get("gpu_event_reasons") or 0
            for bit, name in REASONS.items():
                if mask & bit:
                    reasons[name] += 1
        vendor = Counter(name for r in window for name in (r.get("vendor_throttle") or []))
        measured = {name: round(st.mean(st.mean(r["cpu_mhz"][POS[c]] for c in cpus)
                                        for r in window)) for name, cpus in CLUSTERS.items()}
        caps = {name: round(st.mean((r.get("cpu_cluster_caps_mhz") or [0] * 4)[i]
                                    for r in window)) for i, name in enumerate(CLUSTERS)}
        print(json.dumps({
            "ceiling_c": mark.get("ceiling_c"), "trend_margin_c": mark.get("trend_margin_c"),
            "samples": len(window),
            "gpu_cap_mhz": round(st.mean(r["gpu_cap_mhz"] for r in window)),
            "gpu_measured_mhz": round(st.mean(r["gpu_mhz"] for r in window)),
            "gpu_w_mean": round(st.mean(r["gpu_w"] for r in window), 1),
            "gpu_w_max": round(max(r["gpu_w"] for r in window), 1),
            "nvidia_c_max": max(r["gpu_c"] for r in window),
            "tgpu_mean_c": round(st.mean(acpi(r, "TGPU") for r in window), 1),
            "tgpu_max_c": max(acpi(r, "TGPU") for r in window),
            "cpu_hottest_mean_c": round(st.mean(max(acpi(r, z) for z in cpu_zones) for r in window), 1),
            "cpu_hottest_max_c": max(max(acpi(r, z) for z in cpu_zones) for r in window),
            "projection_max_c": round(max(projections), 1) if projections else None,
            "cpu_measured_mhz": measured, "cpu_caps_mhz": caps,
            "gpu_vendor_reasons_s": dict(reasons), "vendor_watch_s": dict(vendor),
            "modes": dict(Counter(r["mode"] for r in window)),
        }))


if __name__ == "__main__":
    main()
