"""Closed-loop fan twin: the Supervisor (clock loops and fan policy) against the
fitted cooler (analysis/sink_fit.py, 27 September 2026). Synthetic; for
choosing fan parameters, not hardware evidence.

Plant:
  plate  Cp dTp/dt = P - Gn (Tp - Tf)                      (die + contact plate)
  fins   Cf dTf/dt = Gn (Tp - Tf) - Ga(fan) (Tf - room)     (fin block + case)
  Ga(fan) = g0 + g1 * max(0.2, fan / 12); fan response lag ~3 s
  TGPU = Tp + 0.52 K/W x P_gpu; nvidia = Tp + P_gpu / 1.9
  CPU zones = Tp + cluster twin rise (gain x util^0.8 x (f/f_max)^1.7), lag 2 s
GPU power: sweep fit x workload intensity (matrix 1.0, LLM decode ~0.45),
with the burn-in's own +-12 W wobble. CPU power: calorimetric-v2.

Stress metrics (thermal cycling): the plate's swing and its 60 s-smoothed
rate; a range-pair damage proxy (sum of squared ranges between turning
points of the 30 s-smoothed plate) and TGPU's fast swing for reference.

    python3 -m simulation.fan_twin
"""
from dataclasses import replace
from math import exp, sin
import json
import random

from energy_control.power_estimate import MODEL as CPU_MODEL
from simulation.model import GB10_GPU_MATMUL_W, Observation, Settings, Supervisor

CP, CF, GN, G0, G1, ROOM, BG, R_TGPU = 28.3, 271.7, 3.45, 1.92, 1.50, 21.0, 12.3, 0.52
ZONE_GAIN = {"E0": 27.0, "P0": 56.8, "E1": 27.0, "P1": 63.4}
F_MAX = {"E": 2808.0, "P": 3900.0}
F_MIN = {"E": 338.0, "P": 1378.0}


def cpu_power(util, ratios):
    total = CPU_MODEL.base_w
    for i, name in enumerate(("E0", "P0", "E1", "P1")):
        idle, full, fmax = CPU_MODEL.clusters[name]
        kind = name[0]
        f = F_MIN[kind] + ratios[i] * (F_MAX[kind] - F_MIN[kind])
        total += idle + (full - idle) * util[i] * (f / fmax) ** CPU_MODEL.exponent
    return total


def scenario(name):
    """(t_end_s, gpu_intensity, cpu_util per cluster) segments."""
    idle = (0.0, (0.02, 0.05, 0.02, 0.05))
    if name == "burnin":
        return [(600, *idle), (1200, 1.0, (0.02, 0.05, 0.02, 0.05)), (2400, *idle)]
    if name == "llm-bursts":
        segs, t = [(300, *idle)], 300
        for _ in range(5):
            segs += [(t + 180, 0.45, (0.1, 0.3, 0.1, 0.3)), (t + 300, *idle)]
            t += 300
        return segs + [(t + 600, *idle)]
    if name == "worst":
        return [(300, *idle), (900, 1.0, (1.0, 1.0, 1.0, 1.0)), (1800, *idle)]
    raise ValueError(name)


def simulate(settings, name, *, dt=0.25, seed=3):
    rng = random.Random(seed)
    sup = Supervisor(settings)
    segs = scenario(name)
    Tf = ROOM + (BG + 10) / (G0 + G1)
    Tp = Tf + (BG + 10) / GN
    fan_eff, zones = 12.0, {k: Tp for k in ZONE_GAIN}
    t, rows, prefill_sent = 0.0, [], False
    prev_intensity = 0.0
    tg_hist = []
    while t < segs[-1][0]:
        seg = next(s for s in segs if t < s[0])
        intensity, util = seg[1], seg[2]
        prefill = intensity >= 0.4 and prev_intensity < 0.4
        prev_intensity = intensity
        cap = sup.cap
        wobble = 12.0 * sin(t * 1.7) * (1.0 if intensity >= 0.9 else 0.3) + rng.gauss(0, 1.5)
        p_gpu = 5.4 + max(0.0, (GB10_GPU_MATMUL_W(cap) - 4.5) * intensity + wobble * intensity)
        ratios = tuple(c.cap for c in sup.clusters)
        p_cpu = cpu_power(util, ratios)
        p = p_gpu + p_cpu + BG
        fan_eff += (sup.fan_state - fan_eff) * (1 - exp(-dt / 3.0))
        ga = G0 + G1 * max(0.2, fan_eff / 12)
        qn = GN * (Tp - Tf)
        Tp += dt * (p - qn) / CP
        Tf += dt * (qn - ga * (Tf - ROOM)) / CF
        tgpu = Tp + R_TGPU * p_gpu
        nvidia = Tp + p_gpu / 1.9 - 8.0
        for i, k in enumerate(ZONE_GAIN):
            kind = k[0]
            f = F_MIN[kind] + ratios[i] * (F_MAX[kind] - F_MIN[kind])
            target = Tp + ZONE_GAIN[k] * util[i] ** 0.8 * (f / F_MAX[kind]) ** 1.7 - 6.0
            zones[k] += (target - zones[k]) * (1 - exp(-dt / 2.0))
        tg_hist.append(tgpu)
        rise = (tg_hist[-1] - tg_hist[-9]) / 2.0 if len(tg_hist) > 8 else 0.0
        zone_obs = tuple((zones[k], zones[k] + 2 * max(0.0, 0.0)) for k in ZONE_GAIN)
        o = Observation(max(zones.values()), nvidia, min(1.0, 0.05 + 0.95 * intensity),
                        prefill_arrival=prefill, gpu_w=p_gpu, cpu_w=p_cpu,
                        cpu_util=sum(util) / 4, cluster_util=util, cpu_zones=zone_obs,
                        gpu_zone=(tgpu, tgpu + 2 * max(0.0, rise)))
        cmd = sup.step(o, dt)
        rows.append((t, Tp, Tf, tgpu, cmd.gpu_cap_mhz, cmd.mode, sup.fan_state, intensity))
        t += dt
    return rows


def smooth(xs, n):
    out, acc = [], 0.0
    for i, x in enumerate(xs):
        acc += x
        if i >= n:
            acc -= xs[i - n]
        out.append(acc / min(i + 1, n))
    return out


def metrics(rows, dt=0.25):
    plate = [r[1] for r in rows]
    tgpu = [r[3] for r in rows]
    load = [r for r in rows if r[7] >= 0.4]
    s30 = smooth(plate, int(30 / dt))[::int(10 / dt)]          # 10 s points of the 30 s mean
    turning = [s30[0]] + [b for a, b, c in zip(s30, s30[1:], s30[2:]) if (b - a) * (c - b) < 0] + [s30[-1]]
    ranges = [abs(b - a) for a, b in zip(turning, turning[1:])]
    s60 = smooth(plate, int(60 / dt))
    step = int(60 / dt)
    rate = max(abs(s60[i] - s60[i - step]) for i in range(step, len(s60)))   # K per min
    first = []
    starts = [i for i in range(1, len(rows)) if rows[i][7] >= 0.4 and rows[i - 1][7] < 0.4]
    for i in starts:
        first += [r[4] for r in rows[i:i + int(60 / dt)]]
    return {"plate_min_max_c": [round(min(plate), 1), round(max(plate), 1)],
            "plate_rate_max_k_per_min": round(rate, 2),
            "cycle_damage_proxy": round(sum(r * r for r in ranges), 1),
            "tgpu_max_c": round(max(tgpu), 1),
            "gpu_cap_mean_load": round(sum(r[4] for r in load) / max(1, len(load))),
            "gpu_cap_mean_first_60s": round(sum(first) / max(1, len(first))),
            "derated_share_load": round(sum(r[5] == "DERATED" for r in load) / max(1, len(load)), 3),
            "fan_mean_idle": round(sum(r[6] for r in rows if r[7] < 0.4) / max(1, sum(r[7] < 0.4 for r in rows)), 1),
            "fan_mean_load": round(sum(r[6] for r in load) / max(1, len(load)), 1)}


def main():
    base = Settings(maximum_mhz=2200, baseline_mhz=1700, fan_min_state=2)
    candidates = {"load (today)": replace(base, fan_policy="load", fan_idle_delay_s=300.0)}
    for target in (50.0, 55.0, 60.0):
        for dwell in (15.0, 30.0):
            candidates[f"predictive plate {target:.0f} C, down {dwell:.0f} s"] = replace(
                base, fan_policy="predictive", fan_plate_target_c=target, fan_down_dwell_s=dwell)
    out = {}
    for scen in ("burnin", "llm-bursts", "worst"):
        out[scen] = {label: metrics(simulate(s, scen)) for label, s in candidates.items()}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
