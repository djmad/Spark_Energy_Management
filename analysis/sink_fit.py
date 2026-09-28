"""Fit the cooler's heat stores with the room air measured (doc/55 §5).

Operator, 27 September 2026: the cooler has a primary copper plate on the die,
joined to the fin block through a narrow cross-section, and the network
card's plate feeds the fin block by a heatpipe; the room air at the intake is
about 21 C. Model (room fixed at ROOM_C):

  plate  Cp dTp/dt = P_in - Gn (Tp - Tf)
  fins   Cf dTf/dt = Gn (Tp - Tf) - Ga(fan) (Tf - T_room)
  Ga(fan) = g0 + g1 * max(0.2, floor / 12)
  P_in   = P_gpu (measured) + P_cpu (power_estimate, calorimetric-v2) + P_bg
  TGPU   = Tp + (r + r1 * a) * P_gpu      (hotspot above the plate)
  a      = (P_gpu - 4.5) / (P_matmul(f) - 4.5), 0..1.2   (GPU activity)

"fins" lumps the fin block with the case air it heats. P_bg is the constant
background heat (board, memory, NIC, idle SoC).

Refit 28 September 2026 over the full power range: training 26-27 September
(GPU 5-53 W: the night's fan-floor runs at floors 2-12 plus the evening's
burn-ins), holdout 28 September (incl. an LLM run at 46 W). The first fit
(night only, 5-26 W) traded background heat against the conductances and ran
10 K warm at 46 W.

Second refit, 28 September 2026 evening: the morning refit had seen high power
only in short burn-ins and ran 4.3 K warm on TGPU for hours of LLM at 2.5 GHz
and 40 W or more. Training now runs to 28 September 14:00 (incl. the day's LLM
at 2.5 GHz), holdout 28 September from 14:00. The hotspot term gained the GPU
activity a (P_matmul: simulation.model.GB10_GPU_MATMUL_W, the burn-in's power at
the clock): dense matrix work sits hotter above the plate per watt than LLM
decode. Result: energy_control/cooler_twin.py (CoolerParams).

    python3 -m analysis.sink_fit [trace-dir] [--three]   # --three: also the 3-store fit
"""
from datetime import datetime
import json
from math import exp, log
from pathlib import Path
import statistics as st
from types import SimpleNamespace

from energy_control.power_estimate import estimate_cpu_power_w
from simulation.model import GB10_GPU_MATMUL_W

import sys

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
DIR = Path(ARGS[0]) if ARGS else Path("/var/lib/spark-energy/traces")
ROOM_C = 21.0
IDLE_GPU_W = 4.5
ORDER = sorted(range(20), key=lambda i: f"policy{i}")
TRAIN = ("2026-09-26T20:00:00+02:00", "2026-09-28T13:59:59+02:00")
HOLDOUT = ("2026-09-28T14:00:00+02:00", "2026-09-28T23:59:00+02:00")
NAMES = ("Cp", "Cf", "Gn", "g0", "g1", "P_bg", "r", "r1")


def activity(gpu_w, mhz):
    """GPU power as a share of the matrix burn-in's power at this clock."""
    if not mhz or mhz <= 0:
        return 0.4
    return min(1.2, max(0.0, (gpu_w - IDLE_GPU_W) / (GB10_GPU_MATMUL_W(mhz) - IDLE_GPU_W)))
NAMES3 = ("Cd", "Cp", "Cf", "Gd", "Gn", "g0", "g1", "P_bg", "r")


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def load(a, b):
    rows = []
    for name in ("20260926.jsonl", "20260927.jsonl", "20260928.jsonl"):
        path = DIR / name
        if not path.exists():
            continue
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r["utc_ns"] / 1e9
            if not ts(a) <= t <= ts(b):
                continue
            tgpu = (r.get("acpi_c") or {}).get("acpi_TGPU")
            if tgpu is None or r.get("gpu_w") is None or r.get("fan_floor") is None:
                continue
            util = r.get("cluster_util_pct")
            mhz = r.get("cpu_mhz") or []
            cpu_w = None
            if isinstance(util, dict) and len(mhz) == 20:
                policies = tuple(SimpleNamespace(index=cpu, measured_mhz=mhz[k]) for k, cpu in enumerate(ORDER))
                cpu_w = estimate_cpu_power_w(util, policies)
            rows.append((t, r["gpu_w"], cpu_w if cpu_w is not None else 4.5, r["fan_floor"], tgpu,
                         r.get("mode"), activity(r["gpu_w"], r.get("gpu_mhz"))))
    rows.sort()
    return rows


def simulate(p, rows):
    Cp, Cf, Gn, g0, g1, P_bg, r, r1 = p
    t0, pg, pc, fan, tg, _, a = rows[0]
    ga = g0 + g1 * max(0.2, fan / 12)
    p_in = pg + pc + P_bg
    Tf = ROOM_C + p_in / ga
    Tp = Tf + p_in / Gn
    shift = tg - (Tp + (r + r1 * a) * pg)   # start from the measured state, keep the split
    Tp += shift; Tf += shift
    err, prev = [], t0
    for t, pg, pc, fan, tg, mode, a in rows:
        dt = t - prev
        prev = t
        if dt > 30:                     # gap (restart): keep the state, no integration
            dt = 0.0
        ga = g0 + g1 * max(0.2, fan / 12)
        p_in = pg + pc + P_bg
        n = max(1, int(dt / 0.5))
        for _ in range(n):
            h = dt / n
            qn = Gn * (Tp - Tf)
            Tp += h * (p_in - qn) / Cp
            Tf += h * (qn - ga * (Tf - ROOM_C)) / Cf
        err.append(Tp + (r + r1 * a) * pg - tg)
    return err


def simulate3(p, rows):
    """die (Cd) -> plate (Cp) -> neck Gn -> fins/case (Cf) -> room via Ga(fan)."""
    Cd, Cp, Cf, Gd, Gn, g0, g1, P_bg, r = p
    t0, pg, pc, fan, tg, _, _ = rows[0]
    ga = g0 + g1 * max(0.2, fan / 12)
    p_in = pg + pc + P_bg
    Tf = ROOM_C + p_in / ga
    Tp = Tf + p_in / Gn
    Td = Tp + p_in / Gd
    shift = tg - (Td + r * pg)
    Td += shift; Tp += shift; Tf += shift
    err, prev = [], t0
    for t, pg, pc, fan, tg, mode, _ in rows:
        dt = t - prev
        prev = t
        if dt > 30:
            dt = 0.0
        ga = g0 + g1 * max(0.2, fan / 12)
        p_in = pg + pc + P_bg
        n = max(1, int(dt / 0.25))
        for _ in range(n):
            h = dt / n
            qd, qn = Gd * (Td - Tp), Gn * (Tp - Tf)
            Td += h * (p_in - qd) / Cd
            Tp += h * (qd - qn) / Cp
            Tf += h * (qn - ga * (Tf - ROOM_C)) / Cf
        err.append(Td + r * pg - tg)
    return err


def unpack3(x):
    return [exp(v) for v in x[:8]] + [x[8]]


def cost3(x, rows):
    p = unpack3(x)
    if not 0 <= p[8] <= 1.5 or not p[0] < p[1] < p[2]:
        return 1e9
    e = simulate3(p, rows)
    return (sum(v * v for v in e) / len(e)) ** 0.5


def unpack(x):
    return [exp(v) for v in x[:6]] + list(x[6:8])


def cost(x, rows):
    p = unpack(x)
    if not (0 <= p[6] <= 1.5 and -1.5 <= p[7] <= 1.5):
        return 1e9
    e = simulate(p, rows)
    return (sum(v * v for v in e) / len(e)) ** 0.5


def nelder_mead(f, x0, step, iters=1200):
    n = len(x0)
    pts = [x0] + [[x0[j] + (step[j] if j == i else 0) for j in range(n)] for i in range(n)]
    vals = [f(q) for q in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts, vals = [pts[i] for i in order], [vals[i] for i in order]
        c = [sum(q[j] for q in pts[:-1]) / n for j in range(n)]
        xr = [c[j] + (c[j] - pts[-1][j]) for j in range(n)]; fr = f(xr)
        if fr < vals[0]:
            xe = [c[j] + 2 * (c[j] - pts[-1][j]) for j in range(n)]; fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = [c[j] + 0.5 * (pts[-1][j] - c[j]) for j in range(n)]; fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                pts = [pts[0]] + [[pts[0][j] + 0.5 * (q[j] - pts[0][j]) for j in range(n)] for q in pts[1:]]
                vals = [vals[0]] + [f(q) for q in pts[1:]]
    i = min(range(n + 1), key=lambda k: vals[k])
    return pts[i], vals[i]


def thin(rows, every=2):
    return rows[::every]


def main():
    train, hold = load(*TRAIN), load(*HOLDOUT)
    print(json.dumps({"train_rows": len(train), "holdout_rows": len(hold),
                      "train_fans": sorted({r[3] for r in train})}))
    tr = thin(train, 3)
    best = None
    for cf in (400.0, 1500.0):
        x0 = [log(160.0), log(cf), log(6.0), log(1.5), log(3.0), log(15.0), 0.3, 0.1]
        x, v = nelder_mead(lambda x: cost(x, tr), x0, [0.4] * 6 + [0.2, 0.1], iters=2000)
        if best is None or v < best[1]:
            best = (x, v)
    p = unpack(best[0])
    hold_e = simulate(p, hold)
    fit = dict(zip(NAMES, (round(v, 3) for v in p)))
    ga12, ga2 = p[3] + p[4], p[3] + p[4] * 0.2
    out = {"fit": fit, "train_rms_k": round(best[1], 2),
           "holdout_rms_k": round((sum(e * e for e in hold_e) / len(hold_e)) ** 0.5, 2),
           "holdout_bias_k": round(st.mean(hold_e), 2),
           "plate_copper_g": round(p[0] / 0.385), "fins_alu_g": round(p[1] / 0.897),
           "g_air_fan12": round(ga12, 2), "g_air_fan2": round(ga2, 2),
           "tau_plate_s": round(p[0] / p[2], 1),
           "tau_fins_fan12_s": round(p[1] / ga12), "tau_fins_fan2_s": round(p[1] / ga2),
           "plate_to_room_k_per_w_fan12": round(1 / p[2] + 1 / ga12, 3),
           "plate_to_room_k_per_w_fan2": round(1 / p[2] + 1 / ga2, 3)}
    print(json.dumps(out, indent=1))
    if "--three" not in sys.argv:
        return
    best3 = None
    for cf in (600.0, 2000.0):
        x0 = [log(20.0), log(150.0), log(cf), log(4.0), log(4.0), log(2.0), log(3.0), log(12.0), 0.3]
        x, v = nelder_mead(lambda x: cost3(x, tr), x0, [0.4] * 8 + [0.2], iters=2000)
        if best3 is None or v < best3[1]:
            best3 = (x, v)
    p3 = unpack3(best3[0])
    e3 = simulate3(p3, hold)
    Cd, Cp, Cf, Gd, Gn, g0, g1, P_bg, r = p3
    ga12, ga2 = g0 + g1, g0 + 0.2 * g1
    print(json.dumps({"three_store": dict(zip(NAMES3, (round(v, 3) for v in p3))),
                      "train_rms_k": round(best3[1], 2),
                      "holdout_rms_k": round((sum(e * e for e in e3) / len(e3)) ** 0.5, 2),
                      "holdout_bias_k": round(st.mean(e3), 2),
                      "plate_copper_g": round(Cp / 0.385), "fins_alu_g": round(Cf / 0.897),
                      "tau_die_s": round(Cd / Gd, 1), "tau_plate_s": round(Cp / (Gd + Gn), 1),
                      "tau_fins_fan12_s": round(Cf / ga12), "tau_fins_fan2_s": round(Cf / ga2),
                      "g_air_fan12": round(ga12, 2), "g_air_fan2": round(ga2, 2)}, indent=1))


if __name__ == "__main__":
    main()
