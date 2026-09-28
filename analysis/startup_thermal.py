"""Offline CPU-assisted model-startup experiment; no actual LLM or hardware.

Illustrative power curves and zero sensor delay. Never use these numbers as
qualification evidence or derive a live startup limit from this script alone.
"""
from dataclasses import replace
import json

from simulation.model import (CoreThermalParameters, CoreThermalPlant,
                              Observation, Supervisor, cpu_class_caps)


def run(*, initial_c=25.0, sink_capacity=150.0, fan_tau=3.0):
    parameters = replace(CoreThermalParameters(), sink_capacity_j_k=sink_capacity,
                         fan_time_constants_s=(fan_tau, fan_tau))
    plant = CoreThermalPlant(parameters, cores_c=(initial_c,) * 20,
                             gpu_c=initial_c, sink_c=initial_c, fans=(0.2, 0.2))
    control = Supervisor()
    dt = 0.25
    peaks = dict(cpu_c=initial_c, gpu_c=initial_c, copper_c=initial_c,
                 gpu_cap_mhz=0., loading_fast_cap_mhz=0.)
    phases = {}
    abort = None
    elapsed = 0.
    for tick in range(960):
        t = tick * dt
        loading = 20 <= t < 140
        inference = 140 <= t < 180
        phase = "loading" if loading else "inference" if inference else "cooldown" if t >= 180 else "idle"
        demand = loading or inference
        command = control.step(Observation(
            max(plant.cores_c), plant.gpu_c, float(demand),
            cpu_demand_active=demand, cpu_work_arrival=tick == 80,
            model_loading=loading, prefill_arrival=tick == 560), dt)
        if command.mode == "FAULT":
            abort = dict(time_s=t, reason=command.reason)
            break
        fast, slow = cpu_class_caps(command.cpu_ratio)
        # Explicit assumptions: one busy fast core at startup; 5 in inference.
        # 0.2 W/core idle; synthetic dynamic 4 W/startup core, 1.8 W/decode core.
        powers = [0.2] * 20
        if loading:
            powers[0] += 4.0 * (fast / 3900) ** 2
        elif inference:
            for core in range(5):
                powers[core] += 1.8 * (fast / 3900) ** 2
        gpu_w = 8 + (62 * (command.gpu_cap_mhz / 1800) ** 2 if demand else 0)
        fan_target = max(0.2, command.fan_state / 12)
        plant.advance(tuple(powers), gpu_w, dt, fan_targets=(fan_target, fan_target))
        elapsed = t + dt
        peaks["cpu_c"] = max(peaks["cpu_c"], max(plant.cores_c))
        peaks["gpu_c"] = max(peaks["gpu_c"], plant.gpu_c)
        peaks["copper_c"] = max(peaks["copper_c"], plant.sink_c)
        peaks["gpu_cap_mhz"] = max(peaks["gpu_cap_mhz"], command.gpu_cap_mhz)
        if loading:
            peaks["loading_fast_cap_mhz"] = max(peaks["loading_fast_cap_mhz"], fast)
        phases[phase] = dict(end_s=elapsed, cpu_c=max(plant.cores_c),
                             gpu_c=plant.gpu_c, copper_c=plant.sink_c,
                             fast_cap_mhz=fast, slow_cap_mhz=slow,
                             gpu_cap_mhz=command.gpu_cap_mhz, fan_response=plant.fans)
        if max(max(plant.cores_c), plant.gpu_c) >= 93:
            abort = dict(time_s=elapsed, reason="synthetic plant reached abort boundary")
            break
    return dict(synthetic=True, hardware_access=False, sensor_delay_s=0,
                initial_c=initial_c, sink_capacity_j_k=sink_capacity,
                fan_time_constant_s=fan_tau, elapsed_s=elapsed,
                peaks=peaks, phase_ends=phases, abort=abort)


if __name__ == "__main__":
    print(json.dumps([run(), run(initial_c=45), run(sink_capacity=300, fan_tau=6)], indent=2))
