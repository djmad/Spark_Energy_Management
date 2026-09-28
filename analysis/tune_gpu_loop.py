"""Closed-loop GPU regulation tuning on the calibrated twin (no device I/O).

TH run 12 (27 September 2026) held the GPU at its 50 C test target but
dithered the ceiling by ~125 MHz (worst 650 MHz within 30 s). This harness
closes the loop between ``GB10_FIT`` and the live policy settings with the
live sensing path: an integer GPU sensor, a 2 s least-squares trend, constant
LLM load (96 % utilisation, 12 active jobs) and a new prefill every 90 s.
The GPU target sits 2 C below the plant's uncapped steady temperature so the
loop must regulate. Scores use the TH criterion-3 metric.
"""
import argparse
import json

from energy_control.broker import Config
from energy_control.policy import ShadowPolicy
from simulation.model import GB10_FIT, Observation, Plant, Supervisor


def _trend(history):
    n = len(history)
    if n < 3:
        return history[-1][1]
    mt = sum(x for x, _ in history) / n
    mv = sum(y for _, y in history) / n
    b = sum((x - mt) * (y - mv) for x, y in history) / sum((x - mt) ** 2 for x, _ in history)
    return mv + b * (history[-1][0] - mt)


def uncapped_steady_gpu_c(seconds=900.0, dt=0.25):
    plant, command = Plant(GB10_FIT), None
    controller = Supervisor(ShadowPolicy._settings(Config(gpu_max_mhz=1800, gpu_entry_mhz=1800)),
                            twin=GB10_FIT)
    controller.cap = 1800
    for _ in range(int(seconds / dt)):
        command = controller.step(Observation(55.0, plant.gpu_c, 0.96, active_jobs=12,
                                              cpu_demand_active=True, cpu_util=0.3), dt)
        plant.advance(command, 0.96, 0.3, dt)
    return plant.gpu_c


def run(config, *, seconds=1200.0, dt=0.25, prefill_every_s=90.0, steady_after_s=300.0):
    plant = Plant(GB10_FIT)
    controller = Supervisor(ShadowPolicy._settings(config), twin=GB10_FIT)
    controller.cap = config.gpu_entry_mhz
    history, samples = [], []
    for i in range(int(seconds / dt)):
        t = i * dt
        history = [p for p in history + [(t, float(round(plant.gpu_c)))] if t - p[0] <= 2.0]
        arrival = i > 0 and (t % prefill_every_s) < dt
        command = controller.step(Observation(55.0, _trend(history), 0.96, active_jobs=12,
                                              prefill_arrival=arrival,
                                              cpu_demand_active=True, cpu_util=0.3), dt)
        plant.advance(command, 0.96, 0.3, dt)
        if t >= steady_after_s:
            samples.append((t, command.gpu_cap_mhz, plant.gpu_c))
    caps = [c for _, c, _ in samples]
    worst = windows = 0
    for index, (t, _, _) in enumerate(samples):
        window = [c for tt, c, _ in samples[index:] if tt - t <= 30.0]
        spread = max(window) - min(window)
        worst = max(worst, spread)
        windows += spread > 100
    temps = [g for _, _, g in samples]
    return {"worst_pp30_mhz": round(worst), "windows_over_100": windows, "windows": len(samples),
            "cap_mean_mhz": round(sum(caps) / len(caps)), "cap_min_mhz": round(min(caps)),
            "gpu_mean_c": round(sum(temps) / len(temps), 2), "gpu_max_c": round(max(temps), 2)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kp", type=float, nargs="+", default=[0.06, 0.04, 0.03, 0.02])
    parser.add_argument("--kd", type=float, nargs="+", default=[0.08, 0.04, 0.0])
    parser.add_argument("--ki", type=float, nargs="+", default=[0.006])
    args = parser.parse_args(argv)
    steady = uncapped_steady_gpu_c()
    target = round(steady - 2.0, 1)
    results = []
    for kp in args.kp:
        for kd in args.kd:
            for ki in args.ki:
                config = Config(gpu_max_mhz=1800, gpu_entry_mhz=1700, gpu_target_c=target,
                                gpu_kp=kp, gpu_kd=kd, gpu_ki=ki)
                results.append({"kp": kp, "kd": kd, "ki": ki, **run(config)})
    print(json.dumps({"uncapped_steady_gpu_c": round(steady, 2), "target_c": target,
                      "results": results}, indent=1))


if __name__ == "__main__":
    main()
