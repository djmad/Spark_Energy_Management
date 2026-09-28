"""Synthetic CPU-policy comparison; no devices, workloads, or evidence files.

All arms use the same fixed GPU cap and fan floor to isolate CPU policy.
This is not a simulation of the installed whole stack or a fitted Lenovo plant.
"""

import json

from simulation.legacy_cpu import LegacyCpuPid
from simulation.model import Command, Observation, Plant, Supervisor


def compare(cpu_cores: int) -> dict:
    if type(cpu_cores) is not int or not 0 <= cpu_cores <= 20:
        raise ValueError("CPU cores must be an integer from 0 to 20")
    results = {}
    for name in ("legacy_cpu", "shadow_cpu", "announced_cpu_entry"):
        plant = Plant(cpu_c=25, gpu_c=25, sink_c=25)
        legacy, shadow = LegacyCpuPid(), Supervisor()
        peak_cpu, peak_gpu, peak_slope = 25.0, 25.0, 0.0
        first_cap = entry_temperature = None
        aborted = False
        for tick in range(240):
            time_s = tick * 0.5
            loaded = tick >= 120  # 60 s idle followed by 60 s combined load.
            if plant.cpu_c >= 93 or plant.gpu_c >= 93:
                aborted = True
                break
            if name == "legacy_cpu":
                ratio = legacy.step(plant.cpu_c, time_s * 1000).cap_ratio
            else:
                candidate = shadow.step(Observation(
                    plant.cpu_c, plant.gpu_c, float(loaded),
                    prefill_arrival=tick == 120,
                    cpu_demand_active=(loaded and cpu_cores > 0)
                        if name == "announced_cpu_entry" else None,
                    cpu_work_arrival=name == "announced_cpu_entry" and tick == 120 and cpu_cores > 0), 0.5)
                if candidate.mode == "FAULT":
                    aborted = True
                    break
                ratio = candidate.cpu_ratio
            if tick == 120:
                first_cap, entry_temperature = ratio, plant.cpu_c
            previous = plant.cpu_c
            plant.advance(Command(1200, ratio, 12, "COMPARISON", "fixed GPU/fan"),
                          float(loaded), cpu_cores / 20 if loaded else 0, 0.5)
            peak_cpu = max(peak_cpu, plant.cpu_c)
            peak_gpu = max(peak_gpu, plant.gpu_c)
            if loaded:
                peak_slope = max(peak_slope, (plant.cpu_c - previous) / 0.5)
            if plant.cpu_c >= 93 or plant.gpu_c >= 93:
                aborted = True
                break
        results[name] = dict(first_load_cpu_cap_ratio=first_cap,
                             entry_cpu_c=entry_temperature, peak_cpu_c=peak_cpu,
                             peak_gpu_c=peak_gpu, peak_cpu_rise_c_s=peak_slope,
                             aborted=aborted)
    return dict(synthetic=True, hardware_access=False, cpu_cores=cpu_cores,
                idle_s=60, load_s=60, gpu_cap_mhz=1200, fan_floor_state=12,
                sensor_lag_s=0, results=results)


if __name__ == "__main__":
    print(json.dumps([compare(cores) for cores in (0, 5, 20)], indent=2))
