"""Calibrate the synthetic RC twin (simulation/model.py) to measured traces.

Output-error fit: measured inputs (GPU power, CPU utilisation x clock, fan
speed) drive the three-node RC network (CPU, GPU, shared copper sink; the
sink temperature is not observed) and the simulated CPU/GPU temperatures are
compared with the measured ones. Nelder-Mead over log-parameters, standard
library only. Fit on training traces, report error on separate holdout
traces, and block-bootstrap parameter intervals. No device I/O; traces are
read from the service/trial trace files (JSONL).
"""
import argparse
import glob
import json
from math import exp, isfinite, log, sqrt
from random import Random

# name, initial value, lower, upper (all positive; ambient handled separately)
PARAMETERS = (
    ("cpu_capacity_j_k", 8.0, 0.5, 200.0),
    ("gpu_capacity_j_k", 20.0, 1.0, 500.0),
    ("sink_capacity_j_k", 150.0, 10.0, 5000.0),
    ("cpu_conductance_w_k", 0.65, 0.02, 20.0),
    ("gpu_conductance_w_k", 2.2, 0.05, 50.0),
    ("passive_conductance_w_k", 0.8, 0.01, 20.0),
    ("fan_conductance_w_k", 2.4, 0.01, 50.0),
    ("cpu_reference_w", 30.0, 2.0, 200.0),
    ("gpu_power_gain", 1.0, 0.2, 10.0),
)
AMBIENT = ("ambient_c", 25.0, 10.0, 40.0)


def load_samples(paths, *, period_s=1.0):
    """Trace rows -> (t, cpu_c, gpu_c, gpu_w, cpu_load, fan) at >= period_s spacing."""
    rows = []
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    rows.sort(key=lambda r: r["mono_ns"])
    fan_max = [1, 1]
    for r in rows:
        rpm = r.get("fan_rpm") or (None, None)
        for i in (0, 1):
            if isinstance(rpm[i], (int, float)):
                fan_max[i] = max(fan_max[i], rpm[i])
    samples, last = [], None
    for r in rows:
        t = r["mono_ns"] / 1e9
        if last is not None and t - last < period_s:
            continue
        rpm = r.get("fan_rpm") or (None, None)
        if (r.get("gpu_w") is None or r.get("cpu_util_pct") is None or None in rpm
                or not r.get("acpi_c")):
            continue
        mhz = r.get("cpu_mhz") or []
        clock = (sum(mhz) / len(mhz) / 3900) if mhz else 0.5
        load = max(0.0, min(1.0, r["cpu_util_pct"] / 100)) * max(0.05, min(1.0, clock)) ** 2
        fan = sum(rpm[i] / fan_max[i] for i in (0, 1)) / 2
        samples.append((t, max(r["acpi_c"].values()), r["gpu_c"], r["gpu_w"], load, fan))
        last = t
    return split_segments(samples, gap_s=5 * period_s)


def split_segments(samples, *, gap_s):
    segments, current = [], []
    for s in samples:
        if current and s[0] - current[-1][0] > gap_s:
            if len(current) >= 20:
                segments.append(current)
            current = []
        current.append(s)
    if len(current) >= 20:
        segments.append(current)
    return segments


def simulate(values, ambient, segment, *, step_s=0.5):
    """Return simulated (cpu_c, gpu_c) at every sample of one segment."""
    (c_cpu, c_gpu, c_sink, g_cpu, g_gpu, g_passive, g_fan, cpu_ref, gpu_gain) = values
    t0, cpu, gpu = segment[0][0], segment[0][1], segment[0][2]
    sink = (cpu + gpu) / 2 - 2.0  # Unobserved; sits slightly below both.
    out = [(cpu, gpu)]
    for (t_a, _, _, gpu_w, load, fan), (t_b, *_rest) in zip(segment, segment[1:]):
        remaining = t_b - t_a
        p_cpu = 2.0 + cpu_ref * load
        p_gpu = gpu_gain * gpu_w
        while remaining > 1e-9:
            h = min(step_s, remaining)
            q_cpu = g_cpu * (cpu - sink)
            q_gpu = g_gpu * (gpu - sink)
            q_out = (g_passive + g_fan * fan) * (sink - ambient)
            cpu += h * (p_cpu - q_cpu) / c_cpu
            gpu += h * (p_gpu - q_gpu) / c_gpu
            sink += h * (q_cpu + q_gpu - q_out) / c_sink
            remaining -= h
        out.append((cpu, gpu))
    return out


def rmse(values, ambient, segments):
    se_cpu = se_gpu = n = 0
    for segment in segments:
        for (cpu, gpu), sample in zip(simulate(values, ambient, segment), segment):
            if not (isfinite(cpu) and isfinite(gpu)):
                return float("inf"), float("inf")
            se_cpu += (cpu - sample[1]) ** 2
            se_gpu += (gpu - sample[2]) ** 2
            n += 1
    return sqrt(se_cpu / n), sqrt(se_gpu / n)


def _decode(x):
    values = []
    for (name, _, low, high), v in zip(PARAMETERS, x[:-1]):
        values.append(min(high, max(low, exp(v))))
    ambient = min(AMBIENT[3], max(AMBIENT[2], x[-1]))
    return values, ambient


def _cost(x, segments):
    values, ambient = _decode(x)
    e_cpu, e_gpu = rmse(values, ambient, segments)
    return e_cpu + e_gpu


def nelder_mead(f, x0, *, step=0.3, iterations=1500, tolerance=1e-6):
    n = len(x0)
    simplex = [list(x0)] + [[x0[j] + (step if i == j else 0) for j in range(n)] for i in range(n)]
    scores = [f(x) for x in simplex]
    for _ in range(iterations):
        order = sorted(range(n + 1), key=lambda i: scores[i])
        simplex, scores = [simplex[i] for i in order], [scores[i] for i in order]
        if abs(scores[-1] - scores[0]) < tolerance:
            break
        centroid = [sum(p[j] for p in simplex[:-1]) / n for j in range(n)]
        worst = simplex[-1]
        reflected = [centroid[j] + (centroid[j] - worst[j]) for j in range(n)]
        fr = f(reflected)
        if fr < scores[0]:
            expanded = [centroid[j] + 2 * (centroid[j] - worst[j]) for j in range(n)]
            fe = f(expanded)
            simplex[-1], scores[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < scores[-2]:
            simplex[-1], scores[-1] = reflected, fr
        else:
            contracted = [centroid[j] + 0.5 * (worst[j] - centroid[j]) for j in range(n)]
            fc = f(contracted)
            if fc < scores[-1]:
                simplex[-1], scores[-1] = contracted, fc
            else:
                best = simplex[0]
                simplex = [best] + [[best[j] + 0.5 * (p[j] - best[j]) for j in range(n)]
                                    for p in simplex[1:]]
                scores = [scores[0]] + [f(p) for p in simplex[1:]]
    best = min(range(n + 1), key=lambda i: scores[i])
    return simplex[best], scores[best]


def fit(train, *, x0=None, iterations=1500, restarts=5):
    """Nelder-Mead with restarts from the best point (it stalls in flat valleys)."""
    x = x0 or [log(v) for _, v, _, _ in PARAMETERS] + [AMBIENT[1]]
    score = float("inf")
    for _ in range(restarts):
        candidate, candidate_score = nelder_mead(lambda z: _cost(z, train), x,
                                                 iterations=iterations)
        improved = candidate_score < score - 1e-4
        x, score = (candidate, candidate_score) if candidate_score < score else (x, score)
        if not improved:
            break
    values, ambient = _decode(x)
    return x, values, ambient, score


def bootstrap(train, x_best, *, repeats=12, seed=7, iterations=400):
    """Resample whole segments (block bootstrap) and refit from the best point."""
    rng, draws = Random(seed), []
    for _ in range(repeats):
        resample = [train[rng.randrange(len(train))] for _ in train]
        _, values, ambient, _ = fit(resample, x0=x_best, iterations=iterations)
        draws.append(values + [ambient])
    names = [p[0] for p in PARAMETERS] + [AMBIENT[0]]
    intervals = {}
    for i, name in enumerate(names):
        column = sorted(d[i] for d in draws)
        intervals[name] = (column[int(0.1 * (len(column) - 1))], column[int(0.9 * (len(column) - 1))])
    return intervals


def report(train, holdout, *, repeats=12):
    x, values, ambient, _ = fit(train)
    names = [p[0] for p in PARAMETERS]
    params = dict(zip(names, values), ambient_c=ambient)
    result = {"parameters": params,
              "train_rmse_c": dict(zip(("cpu", "gpu"), rmse(values, ambient, train))),
              "holdout_rmse_c": dict(zip(("cpu", "gpu"), rmse(values, ambient, holdout))),
              "train_samples": sum(map(len, train)), "holdout_samples": sum(map(len, holdout))}
    if repeats:
        result["interval_80pct"] = bootstrap(train, x, repeats=repeats)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fit the RC twin to measured traces")
    parser.add_argument("--train", nargs="+", required=True, help="training trace globs")
    parser.add_argument("--holdout", nargs="+", required=True, help="holdout trace globs")
    parser.add_argument("--period", type=float, default=1.0)
    parser.add_argument("--bootstrap", type=int, default=12)
    args = parser.parse_args(argv)
    expand = lambda patterns: sorted(p for pattern in patterns for p in glob.glob(pattern))
    train = [s for path in expand(args.train) for s in load_samples([path], period_s=args.period)]
    holdout = [s for path in expand(args.holdout) for s in load_samples([path], period_s=args.period)]
    if not train or not holdout:
        raise SystemExit("training and holdout traces are both required")
    print(json.dumps(report(train, holdout, repeats=args.bootstrap), indent=2))


if __name__ == "__main__":
    main()
