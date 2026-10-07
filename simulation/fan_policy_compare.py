"""Fan policy comparison on the new cooler (doc/58 fit) — synthetic, not hardware evidence.

The Supervisor (clock loops and fan policy) runs closed-loop against the
two-store cooler of energy_control/cooler_twin.py (calorimetry 1 October 2026,
new heatsink and pads). Workloads:

  replay      measured GPU and CPU power at 5 s from Spark_Telemetry
              (--replay FILE: {"energy.gpu.power_w": [[ms, W], ...],
              "energy.cpu.est_power_w": ..., "energy.gpu.measured_mhz": ...});
              the power and clocks are the measured ones (the policy's GPU cap
              does not feed back into the replayed power)
  llm-bursts  synthetic LLM bursts (fan_twin.scenario)
  burnin      GPU matrix burn-in 10 min
  worst       GPU burn-in plus all CPU cores 10 min

Candidates: today's production fan (predictive, old cooler constants, floor 6)
against the twin fan (fan_policy "twin") at floors 2 and 4. Metrics per
candidate: fan level mean and share at 12, level changes per hour, TGPU mean /
p95 / max, minutes above 75 C, the GPU cap's mean under load and the share of
time the clock loops derated.

    python3 -m simulation.fan_policy_compare --replay telemetry.json
"""
from dataclasses import replace
from math import exp, sin
import argparse
import json
import random

from energy_control.cooler_twin import PARAMS as COOLER
from simulation.fan_twin import F_MAX, F_MIN, ZONE_GAIN, cpu_power, scenario
from simulation.model import GB10_GPU_MATMUL_W, Observation, Settings, Supervisor


def load_replay(path):
    data = json.load(open(path))

    def series(name):
        return {int(t): v for t, v in data.get(name, []) if v is not None}
    gpu, cpu, mhz = series("energy.gpu.power_w"), series("energy.cpu.est_power_w"), series("energy.gpu.measured_mhz")
    times = sorted(gpu)
    t0 = times[0]
    return [((t - t0) / 1000.0, gpu[t], cpu.get(t, 4.0), mhz.get(t, 2400.0)) for t in times]


def simulate(settings, name, replay=None, *, dt=0.5, seed=3):
    rng = random.Random(seed)
    sup = Supervisor(settings)
    p = COOLER
    replay = replay if name == "replay" else None
    if name == "replay":
        segs, end = None, replay[-1][0]
    else:
        segs = scenario(name)
        end = segs[-1][0]
    p_start = (replay[0][1] + replay[0][2]) if replay else 10.0
    Tf = p.room_c + (p_start + p.background_w) / p.air_w_k(12)
    Tp = Tf + (p_start + p.background_w) / p.neck_w_k
    fan_eff, zones = 12.0, {k: Tp for k in ZONE_GAIN}
    t, rows, idx, prev_intensity = 0.0, [], 0, 0.0
    tg_hist = []
    while t < end:
        if replay:
            while idx + 1 < len(replay) and replay[idx + 1][0] <= t:
                idx += 1
            _, gpu_meas, cpu_meas, mhz = replay[idx]
            p_gpu = gpu_meas   # measured at the production clocks; the replay keeps them
            p_cpu, util = cpu_meas, (0.1, 0.3, 0.1, 0.3)
            intensity = 0.45 if gpu_meas > 12 else 0.0
            clock = min(sup.cap, mhz)
        else:
            seg = next(s for s in segs if t < s[0])
            intensity, util = seg[1], seg[2]
            wobble = 12.0 * sin(t * 1.7) * (1.0 if intensity >= 0.9 else 0.3) + rng.gauss(0, 1.5)
            p_gpu = 5.4 + max(0.0, (GB10_GPU_MATMUL_W(sup.cap) - 4.5) * intensity + wobble * intensity)
            ratios = tuple(c.cap for c in sup.clusters)
            p_cpu = cpu_power(util, ratios)
            clock = sup.cap
        prefill = intensity >= 0.4 and prev_intensity < 0.4
        prev_intensity = intensity
        fan_eff += (sup.fan_state - fan_eff) * (1 - exp(-dt / 3.0))
        p_in = p_gpu + p_cpu + p.background_w
        g_air = p.air_w_k(fan_eff)
        q_neck = p.neck_w_k * (Tp - Tf)
        Tp += dt * (p_in - q_neck) / p.plate_j_k
        Tf += dt * (q_neck - g_air * (Tf - p.room_c)) / p.fins_j_k
        tgpu = Tp + p.hotspot_w_k(p_gpu, clock) * p_gpu + rng.gauss(0, 0.3)
        nvidia = tgpu - 5.0
        ratios = tuple(c.cap for c in sup.clusters)
        for i, k in enumerate(ZONE_GAIN):
            kind = k[0]
            f = F_MIN[kind] + ratios[i] * (F_MAX[kind] - F_MIN[kind])
            target = Tp + ZONE_GAIN[k] * util[i] ** 0.8 * (f / F_MAX[kind]) ** 1.7 - 6.0
            zones[k] += (target - zones[k]) * (1 - exp(-dt / 2.0))
        tg_hist.append(tgpu)
        rise = (tg_hist[-1] - tg_hist[-5]) / 2.0 if len(tg_hist) > 4 else 0.0
        zone_obs = tuple((zones[k], zones[k]) for k in ZONE_GAIN)
        o = Observation(max(zones.values()), nvidia, min(1.0, 0.05 + 0.95 * max(intensity, 0.9 if replay and p_gpu > 12 else 0)),
                        prefill_arrival=prefill, gpu_w=p_gpu, cpu_w=p_cpu,
                        cpu_util=sum(util) / 4, cluster_util=util, cpu_zones=zone_obs,
                        gpu_zone=(tgpu, tgpu + 2 * max(0.0, rise)))
        cmd = sup.step(o, dt)
        rows.append((t, tgpu, sup.fan_state, cmd.gpu_cap_mhz, cmd.mode, p_gpu > 12, max(zones.values())))
        t += dt
    return rows


def metrics(rows, dt=0.5):
    hours = len(rows) * dt / 3600
    tg = sorted(r[1] for r in rows)
    load = [r for r in rows if r[5]]
    changes = sum(1 for a, b in zip(rows, rows[1:]) if a[2] != b[2])
    return {
        "fan_mean": round(sum(r[2] for r in rows) / len(rows), 1),
        "fan_at_12_share": round(sum(r[2] == 12 for r in rows) / len(rows), 3),
        "fan_changes_per_h": round(changes / max(hours, 1e-9), 1),
        "tgpu_mean_c": round(sum(tg) / len(tg), 1),
        "tgpu_p95_c": round(tg[int(0.95 * (len(tg) - 1))], 1),
        "tgpu_max_c": round(tg[-1], 1),
        "min_above_75c": round(sum(r[1] > 75 for r in rows) * dt / 60, 1),
        "cpu_zone_max_c": round(max(r[6] for r in rows), 1),
        "gpu_cap_mean_load": round(sum(r[3] for r in load) / max(1, len(load))),
        "derated_share": round(sum(r[4] == "DERATED" for r in load) / max(1, len(load)), 3),
    }


def candidates():
    base = Settings(maximum_mhz=2500, baseline_mhz=1700, gpu_target_c=78.0, cpu_target_c=92.0)
    return {
        "today: predictive, old cooler, floor 6": replace(base, fan_policy="predictive", fan_min_state=6),
        "twin 70 C, floor 2": replace(base, fan_policy="twin", fan_min_state=2),
        "twin 70 C, floor 4": replace(base, fan_policy="twin", fan_min_state=4),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--replay", help="Spark_Telemetry series JSON (5 s)")
    args = parser.parse_args()
    replay = load_replay(args.replay) if args.replay else None
    out = {}
    for scen in (["replay"] if replay else []) + ["llm-bursts", "burnin", "worst"]:
        out[scen] = {label: metrics(simulate(s, scen, replay)) for label, s in candidates().items()}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
