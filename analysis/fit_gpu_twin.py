"""Two-node GPU/copper twin fit from fan-floor identification runs.

GB10 packs CPU and GPU into one package, so the hottest ACPI zone is not a
clean CPU node (the three-node fit was not credible). This fit uses the GPU
sensor and a slower copper-sink node:

    C_g dT_g/dt = k*P_gpu + P_0 - G_gs (T_g - T_s)
    C_s dT_s/dt = G_gs (T_g - T_s) - (G_p + G_f * fan) (T_s - T_amb)

``fan`` is the measured mean fan speed as a fraction of the maximum; ``k``
scales the reported GPU power and ``P_0`` absorbs CPU/idle heat. Fitted on
training runs and scored on a separate holdout run. No device I/O.
"""
import argparse
import glob
import json
from math import exp, log, sqrt

from analysis.fit_twin import nelder_mead

PARAMETERS = (  # name, initial, low, high
    ("gpu_capacity_j_k", 60.0, 5.0, 2000.0),
    ("sink_capacity_j_k", 300.0, 20.0, 20000.0),
    ("gpu_sink_conductance_w_k", 3.0, 0.1, 100.0),
    ("passive_conductance_w_k", 0.5, 0.01, 20.0),
    ("fan_conductance_w_k", 1.5, 0.01, 50.0),
    ("gpu_heat_gain", 1.0, 0.1, 10.0),
    ("baseline_heat_w", 5.0, 0.0, 100.0),
)
FAN_MAX_RPM = 11250.0


def load_run(path, *, period_s=1.0):
    rows = [json.loads(line) for line in open(path, encoding="utf-8")]
    samples, last = [], None
    for r in rows:
        t = r["mono_ns"] / 1e9
        if last is not None and t - last < period_s:
            continue
        rpm = r.get("fan_rpm") or (None, None)
        if r.get("gpu_w") is None or None in rpm:
            continue
        fan = min(1.0, (rpm[0] + rpm[1]) / 2 / FAN_MAX_RPM)
        samples.append((t, r["gpu_c"], r["gpu_w"], fan))
        last = t
    return samples


def _unpack(x):
    values = [min(high, max(low, exp(v) if name != "baseline_heat_w" else v))
              for (name, _, low, high), v in zip(PARAMETERS, x[:-1])]
    return values, min(35.0, max(15.0, x[-1]))


def simulate(values, ambient, run, *, step_s=0.5):
    c_g, c_s, g_gs, g_p, g_f, k, p0 = values
    t0, tg, w0, fan0 = run[0]
    # Start from the idle steady state consistent with the first sample.
    ts = ambient + (k * w0 + p0) / (g_p + g_f * fan0)
    ts = min(ts, tg)
    out = [tg]
    for (t_a, _, w, fan), (t_b, *_rest) in zip(run, run[1:]):
        remaining = t_b - t_a
        heat = k * w + p0
        while remaining > 1e-9:
            h = min(step_s, remaining)
            q = g_gs * (tg - ts)
            tg += h * (heat - q) / c_g
            ts += h * (q - (g_p + g_f * fan) * (ts - ambient)) / c_s
            remaining -= h
        out.append(tg)
    return out


def rmse(values, ambient, runs):
    se = n = 0
    for run in runs:
        for predicted, sample in zip(simulate(values, ambient, run), run):
            se += (predicted - sample[1]) ** 2
            n += 1
    return sqrt(se / n)


def fit(runs, *, restarts=5):
    x = [log(v) if name != "baseline_heat_w" else v for name, v, _, _ in PARAMETERS] + [25.0]
    score = float("inf")
    cost = lambda z: rmse(*_unpack(z), runs)
    for _ in range(restarts):
        candidate, candidate_score = nelder_mead(cost, x, iterations=2000)
        improved = candidate_score < score - 1e-4
        if candidate_score < score:
            x, score = candidate, candidate_score
        if not improved:
            break
    values, ambient = _unpack(x)
    return dict(zip([p[0] for p in PARAMETERS], values), ambient_c=ambient), values, ambient


def main(argv=None):
    parser = argparse.ArgumentParser(description="Two-node GPU/copper twin fit")
    parser.add_argument("--train", nargs="+", required=True)
    parser.add_argument("--holdout", nargs="+", required=True)
    args = parser.parse_args(argv)
    expand = lambda patterns: sorted(p for q in patterns for p in glob.glob(q))
    train = [load_run(p) for p in expand(args.train)]
    holdout = [load_run(p) for p in expand(args.holdout)]
    params, values, ambient = fit(train)
    fast_tau = params["gpu_capacity_j_k"] / params["gpu_sink_conductance_w_k"]
    print(json.dumps({"parameters": params,
                      "train_rmse_c": rmse(values, ambient, train),
                      "holdout_rmse_c": rmse(values, ambient, holdout),
                      "per_holdout_run_rmse_c": [rmse(values, ambient, [r]) for r in holdout],
                      "derived": {"gpu_node_tau_s": fast_tau,
                                  "sink_tau_full_fan_s": params["sink_capacity_j_k"]
                                  / (params["passive_conductance_w_k"] + params["fan_conductance_w_k"]),
                                  "sink_tau_fan_0_44_s": params["sink_capacity_j_k"]
                                  / (params["passive_conductance_w_k"]
                                     + 0.44 * params["fan_conductance_w_k"])}}, indent=2))


if __name__ == "__main__":
    main()
