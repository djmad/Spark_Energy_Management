"""Bounded aggregate-only review of historical CPU PID NDJSON traces.

No workload, GPU context, device write or file output. This deliberately does
not infer CPU watts, ambient temperature, causal PID gains or hardware safety.
"""

import argparse
import json
from math import isfinite
import os
from pathlib import Path
import stat
from statistics import median


MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 256 * 1024
MAX_SAMPLES = 5000
_PHASES = ("baseline", "load", "cooldown")


def _number(value, low, high):
    return type(value) in (int, float) and isfinite(value) and low <= value <= high


def summarize(path: Path) -> dict:
    """Return aggregate evidence only; never return arbitrary source fields."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
            raise ValueError("trace must be a bounded regular file")
        phases = {name: [] for name in _PHASES}
        previous_ms = None
        sample_count = 0
        bytes_read = 0
        guard_temperature_differences = []
        guard_status_repeats = 0
        previous_guard_at = None
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            for line in stream:
                bytes_read += len(line)
                if bytes_read > MAX_FILE_BYTES or len(line) > MAX_LINE_BYTES:
                    raise ValueError("trace inspection budget exceeded")
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("event") != "sample":
                    continue
                sample_count += 1
                if sample_count > MAX_SAMPLES:
                    raise ValueError("too many trace samples")
                phase = row.get("phase")
                ms = row.get("ms")
                cpu = row.get("hottest_c")
                gpu = row.get("gpu")
                if (phase not in phases or not _number(ms, 0, 100_000_000)
                        or not _number(cpu, -10, 150) or not isinstance(gpu, dict)
                        or (previous_ms is not None and ms <= previous_ms)):
                    raise ValueError("invalid sample sequence")
                previous_ms = ms
                gpu_c = gpu.get("temp_c")
                gpu_w = gpu.get("power_w")
                util = gpu.get("util_pct")
                guard = row.get("guard")
                ratio = guard.get("cap_ratio") if isinstance(guard, dict) else None
                if (not _number(gpu_c, -10, 150) or not _number(gpu_w, 0, 500)
                        or not _number(util, 0, 100)
                        or (ratio is None and phase != "cooldown")
                        or (ratio is not None and not _number(ratio, 0, 1))):
                    raise ValueError("invalid GPU or guard sample")
                if isinstance(guard, dict):
                    guard_hottest = guard.get("hottest")
                    guard_c = (guard_hottest.get("c")
                               if isinstance(guard_hottest, dict) else None)
                    guard_at = guard.get("at")
                    if guard_c is not None:
                        if not _number(guard_c, -10, 150):
                            raise ValueError("invalid guard temperature")
                        guard_temperature_differences.append(abs(cpu - guard_c))
                    if guard_at is not None:
                        if not isinstance(guard_at, str) or len(guard_at) > 40:
                            raise ValueError("invalid guard timestamp")
                        if guard_at == previous_guard_at:
                            guard_status_repeats += 1
                        previous_guard_at = guard_at
                phases[phase].append((ms / 1000, cpu, gpu_c, gpu_w, util, ratio))
        if any(not phases[name] for name in _PHASES):
            raise ValueError("baseline, load and cooldown phases required")
        if not (phases["baseline"][-1][0] < phases["load"][0][0]
                < phases["load"][-1][0] < phases["cooldown"][0][0]):
            raise ValueError("phases are not ordered")
        all_samples = [sample for name in _PHASES for sample in phases[name]]
        intervals = [right[0] - left[0] for left, right in zip(all_samples, all_samples[1:])]
        load = phases["load"]
        cooldown = phases["cooldown"]
        post_peak = max(cooldown, key=lambda row: row[1])
        start_s = load[0][0]
        first_reduction = next((row[0] - start_s for row in load if row[5] < 0.999), None)
        threshold_delays = {
            str(level): next((round(row[0] - start_s, 3)
                              for row in load if row[1] >= level), None)
            for level in (88, 90, 92, 93)
        }
        return {
            "sample_count": sample_count,
            "phase_samples": {name: len(phases[name]) for name in _PHASES},
            "cooldown_missing_guard_samples": sum(row[5] is None for row in cooldown),
            "guard_temperature_pairs": len(guard_temperature_differences),
            "guard_vs_outer_temperature_max_abs_c": (
                round(max(guard_temperature_differences), 2)
                if guard_temperature_differences else None),
            "guard_vs_outer_temperature_median_abs_c": (
                round(median(guard_temperature_differences), 2)
                if guard_temperature_differences else None),
            "repeated_guard_status_samples": guard_status_repeats,
            "median_sample_interval_s": round(median(intervals), 3),
            "observed_load_span_s": round(load[-1][0] - load[0][0], 3),
            "baseline_cpu_median_c": round(median(row[1] for row in phases["baseline"]), 2),
            "load_cpu_start_c": load[0][1],
            "load_cpu_peak_c": max(row[1] for row in load),
            "load_first_threshold_s": threshold_delays,
            "first_cpu_cap_reduction_s": (round(first_reduction, 3)
                                          if first_reduction is not None else None),
            "load_gpu_peak_c": max(row[2] for row in load),
            "load_gpu_power_median_w": round(median(row[3] for row in load), 2),
            "load_gpu_util_median_pct": round(median(row[4] for row in load), 2),
            "cooldown_cpu_peak_c": post_peak[1],
            "cooldown_peak_delay_from_first_sample_s": round(
                post_peak[0] - cooldown[0][0], 3),
            "hardware_qualified": False,
        }
    finally:
        if fd >= 0:
            os.close(fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate-only historical CPU trace review")
    parser.add_argument("trace", type=Path)
    args = parser.parse_args(argv)
    try:
        result = summarize(args.trace)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"invalid trace: {type(exc).__name__}")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
