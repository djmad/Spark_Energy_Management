"""Calorimetric CPU power from the calibration run (doc/55 §6).

GB10 has no CPU power sensor. The GPU (power measured by nvidia-smi) is the
reference heater: from GPU-only data each sensor gets a linear response
model, a sum of first-order modes driven by the measured GPU power,

    T(t) = c + sum_m a_m * y_m(t),   y_m' = (P(t) - y_m) / tau_m,

with tau_m fixed (3 ... 700 s) and a_m, c fitted by least squares. The
cooler is shared: CPU heat enters the same die plate, fin block and case air.
For a CPU block k with unknown constant extra power x_k, the sensor then
reads the GPU-driven model plus x_k times the model's response to a unit
step during block k. The x_k follow by linear least squares over the CPU
blocks and their cool-downs.

Self-check: the 1900 MHz GPU block is treated as unknown (its GPU power is
replaced by the idle level) and must be recovered by the same method.

Die sensors (TGPU, TUNC, CPU zones) see the CPU heat closer than the GPU
heat and overestimate it. Sensors downstream of the shared plate (the plate
estimate, case air) are the calorimeters; the spread across them is the
uncertainty.

    python3 -m analysis.calorimetry
"""
from datetime import datetime
import json
from math import exp
from pathlib import Path
import statistics as st

DIR = Path("/var/lib/spark-energy")
TAUS = (3.0, 15.0, 60.0, 200.0, 700.0)
HISTORY_FROM = "2026-09-27T20:10:00+02:00"     # CPU idle from here (temp-raise ended 20:08:50)
TRAIN = ("2026-09-27T21:00:00+02:00", "2026-09-27T21:53:58+02:00")   # thermal-step-1500 (+ idle)
GPU_CHECK = ("2026-09-27T21:53:58+02:00", "2026-09-27T22:02:06+02:00")  # gpu-1900 block
CPU_FIT = ("2026-09-27T21:53:58+02:00", "2026-09-27T23:05:00+02:00")
ROOM_C = 21.0   # operator, 27 September 2026, ~22:45: intake air


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def sensors(r):
    z = r["acpi_c"]
    board = r.get("board_c") or {}
    return {
        "TGPU": z.get("acpi_TGPU"), "TUNC": z.get("acpi_TUNC"),
        "cpu_mean": st.mean(z[k] for k in ("acpi_TS0P", "acpi_TS1P", "acpi_TS0E", "acpi_TS1E")),
        "plate(nv-P/1.9)": r["gpu_c"] - r["gpu_w"] / 1.9,
        "nvme": r.get("nvme_c", board.get("nvme")), "wifi": r.get("wifi_c", board.get("wifi")),
    }


def filters(rows, power):
    """Mode states y_m per row for the power series (exact first-order steps)."""
    ys, state, prev = [], [power[0]] * len(TAUS), rows[0]["t"]
    for r, p in zip(rows, power):
        dt = min(max(r["t"] - prev, 0.0), 10.0)
        prev = r["t"]
        state = [y + (1 - exp(-dt / tau)) * (p - y) for y, tau in zip(state, TAUS)]
        ys.append(state)
    return ys


def lstsq(X, y):
    n = len(X[0])
    A = [[sum(x[i] * x[j] for x in X) + (1e-9 if i == j else 0) for j in range(n)] for i in range(n)]
    b = [sum(x[i] * v for x, v in zip(X, y)) for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(A[k][i]))
        A[i], A[p], b[i], b[p] = A[p], A[i], b[p], b[i]
        for k in range(n):
            if k != i:
                f = A[k][i] / A[i][i]
                A[k] = [a - f * c for a, c in zip(A[k], A[i])]
                b[k] -= f * b[i]
    return [b[i] / A[i][i] for i in range(n)]


def blocks():
    found, open_ = [], {}
    for line in open(DIR / "calibration-events.jsonl"):
        e = json.loads(line)
        if e.get("event") != "block" or e["utc"] < "2026-09-27T21:50":
            continue
        if e["phase"] == "start":
            open_[e["label"]] = (e["kind"], ts(e["utc"]))
        else:
            kind, start = open_.pop(e["label"])
            found.append({"label": e["label"], "kind": kind, "start": start, "end": ts(e["utc"])})
    return found


def main():
    day = datetime.now().strftime("%Y%m%d")
    rows = [json.loads(l) for l in open(DIR / "traces" / f"{day}.jsonl")]
    for r in rows:
        r["t"] = r["utc_ns"] / 1e9
    rows = [r for r in rows if r["t"] >= ts(HISTORY_FROM)]
    cal = blocks()
    cpu_blocks = [b for b in cal if b["kind"] == "cpu"]
    idle_w = st.median(r["gpu_w"] for r in rows if r["gpu_w"] < 10)
    p_gpu = [r["gpu_w"] for r in rows]
    # self-check: GPU block power hidden (replaced by idle)
    g0, g1 = map(ts, GPU_CHECK)
    p_hidden = [idle_w if g0 <= r["t"] <= g1 else p for r, p in zip(rows, p_gpu)]
    unit = {b["label"]: filters(rows, [1.0 if b["start"] <= r["t"] <= b["end"] else 0.0 for r in rows])
            for b in cpu_blocks}
    unit_gpu = filters(rows, [1.0 if g0 <= r["t"] <= g1 else 0.0 for r in rows])
    ys_true, ys_hidden = filters(rows, p_gpu), filters(rows, p_hidden)
    t0, t1 = map(ts, TRAIN)
    c0, c1 = map(ts, CPU_FIT)
    gpu_block_w = st.mean(r["gpu_w"] for r in rows if g0 + 30 <= r["t"] <= g1) - idle_w
    report = {"idle_gpu_w": round(idle_w, 2), "gpu_1900_above_idle_w": round(gpu_block_w, 1),
              "room_c": ROOM_C, "sensors": {}}
    for name in ("TGPU", "TUNC", "cpu_mean", "plate(nv-P/1.9)", "nvme", "wifi"):
        idx = [i for i, r in enumerate(rows) if t0 <= r["t"] <= t1 and sensors(r)[name] is not None]
        if len(idx) < 300:
            continue
        X = [ys_true[i] + [1.0] for i in idx]
        y = [sensors(rows[i])[name] for i in idx]
        coef = lstsq(X, y)
        a, c = coef[:-1], coef[-1]
        rms = (sum((sum(ai * xi for ai, xi in zip(a, x[:-1])) + c - v) ** 2 for x, v in zip(X, y)) / len(y)) ** 0.5
        gain = sum(a)                                      # K per W at steady state
        # self-check on the hidden GPU block (fit window: the block and its gap)
        chk = [i for i, r in enumerate(rows) if g0 <= r["t"] <= g1 + 240 and sensors(r)[name] is not None]
        resid = [sensors(rows[i])[name] - c - sum(ai * yi for ai, yi in zip(a, ys_hidden[i])) for i in chk]
        basis = [sum(ai * yi for ai, yi in zip(a, unit_gpu[i])) for i in chk]
        x_gpu = sum(u * v for u, v in zip(basis, resid)) / max(1e-9, sum(u * u for u in basis))
        # CPU blocks: joint least squares for all blocks
        fit = [i for i, r in enumerate(rows) if c0 <= r["t"] <= c1 and sensors(r)[name] is not None]
        labels = [b["label"] for b in cpu_blocks]
        Xc = [[sum(ai * yi for ai, yi in zip(a, unit[l][i])) for l in labels] for i in fit]
        yc = [sensors(rows[i])[name] - c - sum(ai * yi for ai, yi in zip(a, ys_true[i])) for i in fit]
        xs = lstsq(Xc, yc)
        res = (sum((sum(u * v for u, v in zip(x, xs)) - v2) ** 2 for x, v2 in zip(Xc, yc)) / len(yc)) ** 0.5
        # Downstream estimate: behind the plate's neck GPU and CPU heat mix in the
        # fin block, so the slow modes (tau >= 200 s) respond alike to both.
        # Fit x_k on the cool-down tails only (90-240 s after each block), where
        # the fast, position-dependent modes have decayed.
        slow = [m for m, tau in enumerate(TAUS) if tau >= 200]
        def tail_fit(blocks_, hidden=False):
            idx_ = [i for i, r in enumerate(rows) for b in blocks_
                    if b["end"] + 90 <= r["t"] <= b["end"] + 235 and sensors(r)[name] is not None]
            ys_base = ys_hidden if hidden else ys_true
            X_ = [[sum(a[m] * (unit_gpu if hidden else unit[b["label"]])[i][m] for m in slow)
                   for b in blocks_] for i in idx_]
            y_ = [sensors(rows[i])[name] - c - sum(ai * yi for ai, yi in zip(a, ys_base[i])) for i in idx_]
            return lstsq(X_, y_)
        tail_cpu = tail_fit(cpu_blocks)
        tail_gpu = tail_fit([{"label": "gpu", "start": g0, "end": g1}], hidden=True)[0]
        report["sensors"][name] = {
            "slow_gain_k_per_w": round(sum(a[m] for m in slow), 3),
            "tail_gpu_selfcheck_w": round(tail_gpu, 1),
            "tail_cpu_w_above_idle": {l: round(v, 1) for l, v in zip(labels, tail_cpu)},
            "gain_k_per_w": round(gain, 3), "offset_c": round(c, 1),
            "background_w_at_room": round((c - ROOM_C) / gain, 1) if gain > 0 else None,
            "train_rms_k": round(rms, 2), "gpu_selfcheck_w": round(x_gpu, 1),
            "cpu_w_above_idle": {l: round(v, 1) for l, v in zip(labels, xs)}, "cpu_fit_rms_k": round(res, 2)}
    # the model's own estimate over the same blocks, for comparison
    est = {}
    for b in cpu_blocks:
        vals = [r.get("cpu_est_w") for r in rows if b["start"] + 60 <= r["t"] <= b["end"] and r.get("cpu_est_w")]
        idle = [r.get("cpu_est_w") for r in rows if g0 - 170 <= r["t"] <= g0 - 5 and r.get("cpu_est_w")]
        mhz = {}
        for name, cpus in {"E": [0, 1, 2, 3, 4, 10, 11, 12, 13, 14], "P": [5, 6, 7, 8, 9, 15, 16, 17, 18, 19]}.items():
            window = [r for r in rows if b["start"] + 60 <= r["t"] <= b["end"]]
            order = sorted(range(20), key=lambda i: f"policy{i}")
            pos = {cpu: k for k, cpu in enumerate(order)}
            mhz[name] = round(st.mean(st.mean(r["cpu_mhz"][pos[c]] for c in cpus) for r in window))
        est[b["label"]] = {"model_w": round(st.mean(vals), 1) if vals else None,
                           "model_idle_w": round(st.mean(idle), 1) if idle else None, "mhz": mhz}
    report["twin_v1_estimate"] = est
    report["model_fit"] = model_fit(report)
    print(json.dumps(report, indent=1))


CONSENSUS = ("TGPU", "wifi", "nvme", "plate(nv-P/1.9)")   # downstream of the CPU zones
F_MAX = {"P": 3900.0, "E": 2808.0}


def model_fit(report):
    """power_estimate.py parameters from the calibration (operator, 27 September
    2026: "for now we use the measured values from our calibration cycle").

    The sensors agree on the ratios but not the absolute scale (factor ~3), so
    each block uses the geometric mean over the downstream sensors. Two P
    clusters at full load: dP = 2 x (full - idle) x (f / f_max) ^ exponent; the
    exponent comes from the P blocks at ~3480 and 2600 MHz; the E cores' clock
    stays near f_max, so their block gives the E full-load watts directly."""
    from math import exp as e_, log
    geo = {}
    for label in ("p-cores", "e-cores", "all-cores", "p-cores-2600"):
        values = [report["sensors"][s]["cpu_w_above_idle"][label] for s in CONSENSUS
                  if s in report["sensors"]]
        values = [v for v in values if v > 0]
        geo[label] = e_(sum(log(v) for v in values) / len(values)) if values else None
    mhz = {label: report["twin_v1_estimate"][label]["mhz"] for label in geo}
    p_hi, p_lo = mhz["p-cores"]["P"], mhz["p-cores-2600"]["P"]
    exponent = log(geo["p-cores"] / geo["p-cores-2600"]) / log(p_hi / p_lo)
    p_dyn = geo["p-cores"] / 2 / (p_hi / F_MAX["P"]) ** exponent       # per P cluster at f_max
    e_dyn = geo["e-cores"] / 2 / (mhz["e-cores"]["E"] / F_MAX["E"]) ** exponent
    predicted_all = (2 * p_dyn * (mhz["all-cores"]["P"] / F_MAX["P"]) ** exponent
                     + 2 * e_dyn * (mhz["all-cores"]["E"] / F_MAX["E"]) ** exponent)
    return {"geomean_w_above_idle": {k: round(v, 1) for k, v in geo.items()},
            "exponent": round(exponent, 2),
            "p_cluster_dynamic_w_at_fmax": round(p_dyn, 2), "e_cluster_dynamic_w_at_fmax": round(e_dyn, 2),
            "all_cores_predicted_w": round(predicted_all, 1), "all_cores_measured_w": round(geo["all-cores"], 1),
            "scale_spread": round(max(report["sensors"][s]["cpu_w_above_idle"]["p-cores"] for s in CONSENSUS)
                                  / min(report["sensors"][s]["cpu_w_above_idle"]["p-cores"] for s in CONSENSUS), 1)}


if __name__ == "__main__":
    main()
