"""GPU zone twin: GPU power up to ~70 W, the ACPI GPU zone (TGPU) and the
guard's projection rule on it.

Pure simulation, no device I/O; illustrative, NOT hardware qualification.
Purpose (doc/42 defect 34, doc/53): the matrix burn-in on 27 September 2026
drew 66 W at 2200 MHz. TGPU rose 3-4 C/s and the guard aborted on its
projection while the nvidia sensor was still at 68 C. The twin extends the
fitted GPU model (doc/44, fitted up to ~17 W) to that range, so the GPU zone
loop can be checked offline before a live repeat.

Model (heat flows from the die hotspot to the copper):
- GPU power: idle + dynamic(f) x load intensity. The matrix burn-in's curve
  is fitted to the frequency sweep (doc/53). LLM decode uses a fraction of it.
- Hotspot node (TGPU): small capacity, 1 / hot_k_w K/W to the GPU node. In
  steady state TGPU = nvidia + offset + 0.21 C/W x P (all of 27 September's
  trace rows).
- GPU node (nvidia sensor, integer): 11 J/K, 1.9 W/K to the copper (GB10_FIT).
- Copper: 165 J/K, 0.8 + 3.3 x fan W/K to 27 C ambient, plus the CPU's heat.
- Sensors at 4 Hz: TGPU with AR(1) ripple. The guard samples independently
  (2 s least-squares trend, 2 s x rise, immediate at trend >= 90 C,
  otherwise confirmed after 1 s).
The real Supervisor drives the GPU cap; the CPU side is a fixed heat input.
"""
import argparse
from dataclasses import dataclass, replace
import json
import random

from energy_control.broker import Config
from energy_control.policy import ShadowPolicy
from energy_control.safety import ABORT_C, PREDICTION_BAND_C, PREDICTION_S
from simulation.cluster_twin import GuardProjection, TrendWindow
from simulation.model import Observation, Supervisor


@dataclass(frozen=True)
class GpuTwinParameters:
    ambient_c: float = 27.0
    passive_w_k: float = 0.8
    fan_w_k: float = 3.3          # fan state 12
    sink_capacity_j_k: float = 165.0
    gpu_capacity_j_k: float = 11.0
    gpu_w_k: float = 1.9          # GPU node (nvidia sensor) to copper
    hot_capacity_j_k: float = 5.0
    # 0.29 C/W under the matrix burn-in (sweep: TGPU - nvidia 8.5 C at 30.5 W,
    # 11.8 C at 40.3 W); LLM decode spreads its heat more (0.21 C/W).
    hot_w_k: float = 3.45
    hot_offset_c: float = 0.0
    idle_w: float = 4.5
    # Matrix burn-in dynamic power: k x (f / 1000 MHz) ^ exponent (sweep fit,
    # 1458-1680 MHz measured; predicts ~71 W at 2200 MHz, live 66 W).
    matmul_k_w: float = 11.1
    matmul_exponent: float = 2.27
    other_w: float = 8.0          # board, memory, NIC heat into the copper
    ripple_sigma_c: float = 0.3
    ripple_rho: float = 0.5


class GpuZonePlant:
    def __init__(self, p=None, *, cpu_w=0.0, intensity=1.0, wobble=0.0, bursts=0.0, seed=1):
        self.p = p or GpuTwinParameters()
        self.cpu_w, self.intensity = cpu_w, intensity
        # The burn-in's own power swings at a fixed clock (live 27 September:
        # 46-60 W over ~10 s at 2200 MHz, single bursts to 68 W): a sine of
        # relative amplitude ``wobble`` plus random bursts of +``bursts``.
        self.wobble, self.bursts = wobble, bursts
        self._rng, self._t, self._burst_left = random.Random(seed + 7), 0.0, 0.0
        p = self.p
        # Start in steady state at idle.
        idle = p.idle_w
        sink_k = p.passive_w_k + p.fan_w_k
        self.sink = p.ambient_c + (idle + cpu_w + p.other_w) / sink_k
        self.gpu = self.sink + idle / p.gpu_w_k
        self.hot = self.gpu + idle / p.hot_w_k
        self.power_w = idle

    def gpu_power(self, mhz, busy, dt=0.0):
        p = self.p
        dynamic = p.matmul_k_w * (mhz / 1000.0) ** p.matmul_exponent
        factor = 1.0
        if busy and (self.wobble or self.bursts):
            from math import pi, sin
            self._t += dt
            factor += self.wobble * sin(2 * pi * self._t / 10.0)
            if self._burst_left > 0:
                self._burst_left -= dt
                factor += self.bursts
            elif dt and self._rng.random() < dt / 12.0:   # about one burst per 12 s
                self._burst_left = 1.5
        return p.idle_w + (dynamic * self.intensity * factor if busy else 0.0)

    def advance(self, mhz, busy, dt):
        p = self.p
        self.power_w = self.gpu_power(mhz, busy, dt)
        q_hot = (self.hot - self.gpu) * p.hot_w_k
        q_gpu = (self.gpu - self.sink) * p.gpu_w_k
        q_out = (self.sink - p.ambient_c) * (p.passive_w_k + p.fan_w_k)
        self.hot += (self.power_w - q_hot) / p.hot_capacity_j_k * dt
        self.gpu += (q_hot - q_gpu) / p.gpu_capacity_j_k * dt
        self.sink += (q_gpu + self.cpu_w + p.other_w - q_out) / p.sink_capacity_j_k * dt

    @property
    def tgpu(self):
        return self.hot + self.p.hot_offset_c


class Ripple:
    def __init__(self, p, seed):
        self.p, self.rng, self.noise = p, random.Random(seed), 0.0

    def sample(self, value):
        p = self.p
        innovation = p.ripple_sigma_c * (1 - p.ripple_rho ** 2) ** 0.5
        self.noise = p.ripple_rho * self.noise + self.rng.gauss(0.0, innovation)
        return value + self.noise


def zone_projection(trend, rise):
    projection = trend + PREDICTION_S * rise if trend >= ABORT_C - PREDICTION_BAND_C else trend
    return (trend, max(trend, projection))


def run(settings, *, seconds=600.0, dt=0.25, cpu_w=0.0, intensity=1.0, fixed_mhz=None,
        p=None, seed=1, zone_loop=True, wobble=0.0, bursts=0.0):
    """Burn-in style load: busy from t = 10 s. ``fixed_mhz`` replays a sweep
    step (cap held, no controller); otherwise the Supervisor drives the cap."""
    p = p or GpuTwinParameters()
    plant = GpuZonePlant(p, cpu_w=cpu_w, intensity=intensity, wobble=wobble, bursts=bursts,
                         seed=seed)
    policy_sensor, guard_sensor = Ripple(p, seed), Ripple(p, seed + 1000)
    policy_window, guard_window = TrendWindow(), TrendWindow()
    guard = GuardProjection()
    controller = Supervisor(settings)
    cap = settings.baseline_mhz if fixed_mhz is None else fixed_mhz
    rows, t = [], 0.0
    while t < seconds:
        busy = t >= 10.0
        plant.advance(cap, busy, dt)
        t += dt
        trends = policy_window.add(t, {"TGPU": policy_sensor.sample(plant.tgpu)})
        guard_raw = {"TGPU": guard_sensor.sample(plant.tgpu)}
        guard_trends = guard_window.add(t, guard_raw)
        if guard_trends:
            guard.check(t, guard_raw, guard_trends)
        if fixed_mhz is None and trends:
            zone = zone_projection(*trends["TGPU"]) if zone_loop else None
            command = controller.step(Observation(
                cpu_c=60.0, gpu_c=float(round(plant.gpu)), gpu_util=1.0 if busy else 0.0,
                active_jobs=None, gpu_zone=zone), dt)
            if command.mode == "FAULT":
                rows.append((t, cap, plant.power_w, plant.gpu, plant.tgpu, plant.sink, "FAULT"))
                break
            # The GPU runs at the cap, a little lower at the lock (live: 1449 at 1500).
            cap = command.gpu_cap_mhz
        rows.append((t, cap, plant.power_w, plant.gpu, plant.tgpu, plant.sink, ""))
    return rows, guard.events


def summarise(rows, events, *, steady_from_s=120.0):
    steady = [r for r in rows if r[0] >= steady_from_s] or rows
    caps = [r[1] for r in steady]
    # Oscillation: peak-to-peak per 30 s window (mean and worst), and hard cuts
    # (cap down by more than 200 MHz within 2 s). Rows are 0.25 s apart.
    step = 120
    windows = [caps[i:i + step] for i in range(0, max(1, len(caps) - step + 1), step)]
    p2p = [max(w) - min(w) for w in windows if w]
    cuts = sum(1 for a, b in zip(caps, caps[8:]) if a - b > 200)
    return {"guard_events": len(events), "first_event": events[0][:2] if events else None,
            "cap_p2p_30s_mean": round(sum(p2p) / len(p2p)) if p2p else None,
            "cap_p2p_30s_max": round(max(p2p)) if p2p else None, "hard_cuts": cuts,
            "fault": any(r[6] == "FAULT" for r in rows),
            "cap_mean_mhz": round(sum(r[1] for r in steady) / len(steady)),
            "power_mean_w": round(sum(r[2] for r in steady) / len(steady), 1),
            "nvidia_max_c": round(max(r[3] for r in rows), 1),
            "tgpu_max_c": round(max(r[4] for r in rows), 1),
            "tgpu_steady_mean_c": round(sum(r[4] for r in steady) / len(steady), 1),
            "copper_end_c": round(rows[-1][5], 1)}


def production_settings(**config):
    return ShadowPolicy._settings(Config(**{"gpu_max_mhz": 2200, "gpu_entry_mhz": 1700,
                                            **config}))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=float, default=600.0)
    parser.add_argument("--cpu-w", type=float, default=0.0, help="CPU heat into the copper")
    parser.add_argument("--intensity", type=float, default=1.0, help="1 = matrix burn-in")
    parser.add_argument("--fixed-mhz", type=float)
    parser.add_argument("--no-zone-loop", action="store_true")
    parser.add_argument("--seeds", type=int, default=3)
    args = parser.parse_args(argv)
    for seed in range(1, args.seeds + 1):
        rows, events = run(production_settings(), seconds=args.seconds, cpu_w=args.cpu_w,
                           intensity=args.intensity, fixed_mhz=args.fixed_mhz, seed=seed,
                           zone_loop=not args.no_zone_loop)
        print(json.dumps({"seed": seed, **summarise(rows, events)}))


if __name__ == "__main__":
    main()
