"""Per-cluster CPU twin fit from `scripts/cpu_step.py` runs (no device I/O).

GB10 has four CPU clusters (E0 0-4, P0 5-9, E1 10-14, P1 15-19) that heat
the seven ACPI zones differently (doc/22). Model per zone z:

    T_z(t) = B_z + sum_c K[z][c] * x_c(t),    dx_c/dt = (u_c - x_c) / tau

u_c is the cluster load in [0, 1] (the phase log's pinned-load windows).
Heating and cooling differ (live: 63 % rise in ~5-6 s, fall in ~1.5-2 s,
firmware clock clamping shapes the rise), so tau is tau_up (P and E
separately) while x_c rises and one shared tau_down while it falls. Lags
come from a grid search, K and B from linear least squares on the training
run(s). A holdout run keeps K and the lags and takes B_z from the median of
its last 30 s before the first load, so the holdout error is a prediction
error. Inputs: the service's 1 Hz
trace (`/var/lib/spark-energy/traces`) and the phase log.
"""
import argparse
import glob
import json
from math import exp, sqrt

from analysis.llm_throughput import _solve

TRACES = "/var/lib/spark-energy/traces/*.jsonl"
PHASES = "/var/lib/spark-energy/cpu-step-runs.jsonl"
CLUSTERS = ("E0", "P0", "E1", "P1")
ZONES = ("TS0E", "TS0P", "TS1E", "TS1P", "TSOC", "TUNC", "TGPU")
TAU_UP_GRID = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0)
TAU_DOWN_GRID = (0.5, 1.0, 1.5, 2.0, 3.0)
GAMMA_GRID = (0.4, 0.6, 0.8, 1.0)


def _measured_load(row):
    """Per-cluster utilisation from the trace (includes background load)."""
    util = row.get("cluster_util_pct")
    if not util or any(util.get(c) is None for c in CLUSTERS):
        return None
    return {c: max(0.0, min(1.0, util[c] / 100.0)) for c in CLUSTERS}


def load_window(start_utc_ns, end_utc_ns, *, traces=TRACES):
    """Ordinary service-trace window with measured utilisation (natural load)."""
    samples = []
    for path in sorted(glob.glob(traces)):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if not start_utc_ns <= r["utc_ns"] <= end_utc_ns:
                continue
            load = _measured_load(r)
            if load is None or not all(f"acpi_{z}" in r["acpi_c"] for z in ZONES):
                continue
            samples.append((r["mono_ns"] / 1e9, load, {z: r["acpi_c"][f"acpi_{z}"] for z in ZONES}))
    samples.sort(key=lambda s: s[0])
    if len(samples) < 60:
        raise ValueError("window has fewer than 60 traced samples with cluster utilisation")
    return samples, None


def load_run(run, *, traces=TRACES, phases=PHASES, pre_s=60.0):
    """Trace rows of one step run with the cluster load schedule."""
    events = [json.loads(line) for line in open(phases, encoding="utf-8")]
    events = [e for e in events if e.get("run") == run]
    if not events:
        raise ValueError(f"no phase log for run {run}")
    start = min(e["mono_ns"] for e in events) - pre_s * 1e9
    end = max(e["mono_ns"] for e in events)
    windows = []
    for e in events:
        if e["event"] == "load":
            stop = next(x["mono_ns"] for x in events
                        if x["event"] == "rest" and x.get("cluster") == e["cluster"]
                        and x["mono_ns"] > e["mono_ns"])
            windows.append((e["cluster"], e["mono_ns"], stop))
    rows = []
    for path in sorted(glob.glob(traces)):
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            if start <= r["mono_ns"] <= end and all(f"acpi_{z}" in r["acpi_c"] for z in ZONES):
                rows.append(r)
    rows.sort(key=lambda r: r["mono_ns"])
    samples = []
    for r in rows:
        # Measured utilisation when traced (background load on P0 heated TS0P
        # to 50-65 C during "rest", live 27 September); else the step schedule.
        load = _measured_load(r)
        if load is None:
            load = {c: 0.0 for c in CLUSTERS}
            for cluster, a, b in windows:
                if a <= r["mono_ns"] < b:
                    load[cluster] = 1.0
        samples.append((r["mono_ns"] / 1e9, load, {z: r["acpi_c"][f"acpi_{z}"] for z in ZONES}))
    first_load = min(a for _, a, _ in windows) / 1e9
    quiet = [s for s in samples if first_load - 30 <= s[0] < first_load]
    baseline = {z: sorted(s[2][z] for s in quiet)[len(quiet) // 2] for z in ZONES}
    return samples, baseline


def filtered(samples, tau_p, tau_e, tau_down=None, gamma=1.0):
    """Lagged cluster loads x_c of u**gamma: tau_p/tau_e rising, tau_down falling.

    gamma < 1 is concave: ~30 % utilisation already gave +20 C of the +37 C a
    full five-core load gives (firmware clamps clocks under full load).
    """
    x = {c: 0.0 for c in CLUSTERS}
    out, last = [], None
    for t, load, _ in samples:
        dt = 1.0 if last is None else max(0.0, t - last)
        last = t
        for c in CLUSTERS:
            tau = tau_p if c.startswith("P") else tau_e
            u = load[c] ** gamma
            if tau_down is not None and u < x[c]:
                tau = tau_down
            a = 1.0 - exp(-dt / tau)
            x[c] += a * (u - x[c])
        out.append(dict(x))
    return out


def fit(train, tau_p, tau_e, tau_down=None, gamma=1.0):
    """Least squares K[z][c] and per-run B_z for fixed lags; returns (K, rmse)."""
    k = {}
    se = n = 0
    for z in ZONES:
        # Unknowns: 4 gains + one baseline per training run.
        rows = []
        for index, (samples, _) in enumerate(train):
            for x, (_, _, temps) in zip(filtered(samples, tau_p, tau_e, tau_down, gamma), samples):
                basis = [x[c] for c in CLUSTERS] + [1.0 if j == index else 0.0
                                                    for j in range(len(train))]
                rows.append((basis, temps[z]))
        m = len(rows[0][0])
        normal = [[sum(b[i] * b[j] for b, _ in rows) for j in range(m)] for i in range(m)]
        target = [sum(b[i] * y for b, y in rows) for i in range(m)]
        coef = _solve(normal, target)
        k[z] = dict(zip(CLUSTERS, coef[:4]))
        k[z]["_baseline"] = sum(coef[4:]) / len(coef[4:])
        for b, y in rows:
            se += (sum(c * v for c, v in zip(coef, b)) - y) ** 2
            n += 1
    return k, sqrt(se / n)


def predict_rmse(k, run, tau_p, tau_e, tau_down=None, gamma=1.0):
    samples, baseline = run
    if baseline is None:  # Natural-load window: no quiet pre-period.
        baseline = {z: k[z]["_baseline"] for z in ZONES}
    se = n = 0
    per_zone = {}
    for z in ZONES:
        zse = 0.0
        for x, (_, _, temps) in zip(filtered(samples, tau_p, tau_e, tau_down, gamma), samples):
            predicted = baseline[z] + sum(k[z][c] * x[c] for c in CLUSTERS)
            zse += (predicted - temps[z]) ** 2
        per_zone[z] = sqrt(zse / len(samples))
        se += zse
        n += len(samples)
    return sqrt(se / n), per_zone


def main(argv=None):
    parser = argparse.ArgumentParser(description="Per-cluster CPU twin fit")
    parser.add_argument("--train", nargs="+", required=True,
                        help="cpu_step run ids or START_NS:END_NS utc windows")
    parser.add_argument("--holdout", nargs="+", required=True,
                        help="cpu_step run ids or START_NS:END_NS utc windows")
    args = parser.parse_args(argv)

    def load(spec):
        if ":" in spec:
            a, b = spec.split(":")
            return load_window(int(a), int(b))
        return load_run(spec)
    train = [load(r) for r in args.train]
    holdout = [load(r) for r in args.holdout]
    best = min(((fit(train, tp, te, td, g), tp, te, td, g)
                for tp in TAU_UP_GRID for te in TAU_UP_GRID for td in TAU_DOWN_GRID
                for g in GAMMA_GRID),
               key=lambda item: item[0][1])
    (k, train_rmse), tau_p, tau_e, tau_down, gamma = best
    report = {"tau_up_p_s": tau_p, "tau_up_e_s": tau_e, "tau_down_s": tau_down,
              "gamma": gamma, "train_rmse_c": train_rmse,
              "gain_c_per_full_cluster_load": {z: {c: round(k[z][c], 2) for c in CLUSTERS}
                                               for z in ZONES},
              "baseline_c": {z: round(k[z]["_baseline"], 1) for z in ZONES},
              "holdout": {}}
    for run_id, run in zip(args.holdout, holdout):
        rmse, per_zone = predict_rmse(k, run, tau_p, tau_e, tau_down, gamma)
        report["holdout"][run_id] = {"rmse_c": rmse,
                                     "per_zone_rmse_c": {z: round(v, 2) for z, v in per_zone.items()}}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
