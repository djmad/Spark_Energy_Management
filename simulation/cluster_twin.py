"""Per-cluster CPU ripple twin for the CPU loop against the guard's projection rule.

Pure simulation, no device I/O; illustrative, NOT hardware qualification.
Purpose (doc/48 §0, doc/42 defects 27/28): check offline that a CPU PID can
hold a zone target while the independent guard's immediate-projection abort
(2 s trend >= 90 C and trend + 2 s x rise >= 93 C) stays quiet under P-cluster
sensor ripple, before any live run.

Model (calibrated, 27 September 2026):
- Zone rise above the copper: gain x utilisation^0.8 x (f / f_max)^1.7, with
  first-order lags (P 2 s, E 1.5 s up; 1 s down; live Stage A run). Gains reproduce the
  live 20-worker plateau (P ~2.9 GHz, E ~2.35 GHz: TS1P ~81, TS0P ~77,
  E zones ~62 C at fan 8) and the retired guard's full-load point (vecfp at
  fan 12, P ~3.15 GHz: hottest ~91 C).
- Copper: 27 C + total power x sink resistance (GB10_FIT: 0.8 + 3.3 x fan
  W/K), lag 40 s. TSOC = max(TS0P, TS1P) (measured: equal in 99.4 % under
  CPU load). TSOC is the firmware maximum of all SoC zones: under GPU load it
  equals TGPU, so it is no CPU proxy (doc/42 defect 37).
- Sensors at 4 Hz: AR(1) ripple (sigma 0.45 C, rho 0.5) plus rare one-sample
  steps (legacy traces: residual 0.45 C, p99 step ~2 C per 0.5 s). The guard
  samples independently of the policy (its own sampler child).
"""
from dataclasses import dataclass, field
from math import exp
import random

from energy_control.safety import ABORT_C, PREDICTION_BAND_C, PREDICTION_CONFIRM_S, \
    PREDICTION_IMMEDIATE_C, PREDICTION_S
from simulation.model import Observation, Supervisor, cluster_caps_mhz, cpu_class_caps

CLUSTERS = ("E0", "P0", "E1", "P1")
ZONES = ("TS0E", "TS0P", "TS1E", "TS1P", "TSOC", "TUNC")
FAST_MIN, FAST_MAX, SLOW_MIN, SLOW_MAX = 1378, 3900, 338, 2808


@dataclass(frozen=True)
class TwinParameters:
    gain_c: dict = field(default_factory=lambda: {"TS0P": ("P0", 56.8), "TS1P": ("P1", 63.4),
                                                  "TS0E": ("E0", 27.0), "TS1E": ("E1", 27.0)})
    cluster_full_w: dict = field(default_factory=lambda: {"P0": 12.5, "P1": 12.5,
                                                          "E0": 4.0, "E1": 4.0})
    ambient_c: float = 27.0
    passive_w_k: float = 0.8
    fan_w_k: float = 3.3
    copper_tau_s: float = 40.0
    # 2 s / 1.5 s (was the step-fit 6 s / 3 s, doc/44): the live Stage A run
    # showed P zones rising 2-3 C/s near the top and settling within ~60 s
    # (operator: "not much heat capacity on the cooler").
    tau_up_p_s: float = 2.0
    tau_up_e_s: float = 1.5
    tau_down_s: float = 1.0
    gamma: float = 0.8
    power_exponent: float = 1.7
    ripple_sigma_c: float = 0.45
    ripple_rho: float = 0.5
    spike_rate: float = 0.01
    spike_c: tuple = (1.5, 3.0)


class ClusterZonePlant:
    def __init__(self, p=None, *, gpu_w=12.0, fan_state=12, seed=1):
        self.p = p or TwinParameters()
        self.gpu_w, self.fan_state = gpu_w, fan_state
        self.x = {c: 0.0 for c in CLUSTERS}          # lagged heating share per cluster
        self.copper_c = self.p.ambient_c + 10.0
        self.rng = random.Random(seed)

    def _sink_k_w(self):
        return 1.0 / (self.p.passive_w_k + self.p.fan_w_k * max(0.2, self.fan_state / 12))

    def advance(self, cluster_mhz, utilisation, dt):
        """``cluster_mhz``: dict cluster -> cap MHz (E0, P0, E1, P1)."""
        p = self.p
        total_w = self.gpu_w
        for c in CLUSTERS:
            f, f_max = cluster_mhz[c], (FAST_MAX if c.startswith("P") else SLOW_MAX)
            share = max(0.0, min(1.0, utilisation.get(c, 0.0))) ** p.gamma \
                * (f / f_max) ** p.power_exponent
            tau = (p.tau_up_p_s if c.startswith("P") else p.tau_up_e_s) if share > self.x[c] \
                else p.tau_down_s
            self.x[c] += (1.0 - exp(-dt / tau)) * (share - self.x[c])
            total_w += p.cluster_full_w[c] * self.x[c]
        target = p.ambient_c + total_w * self._sink_k_w()
        self.copper_c += (1.0 - exp(-dt / p.copper_tau_s)) * (target - self.copper_c)

    def zones(self):
        t = {zone: self.copper_c + gain * self.x[cluster]
             for zone, (cluster, gain) in self.p.gain_c.items()}
        t["TSOC"] = max(t["TS0P"], t["TS1P"])
        t["TUNC"] = self.copper_c + 0.5 * (t["TS0P"] - self.copper_c)
        return t


class RippleSensor:
    """Independent 4 Hz sampler: AR(1) ripple plus rare one-sample steps."""

    def __init__(self, p, seed):
        self.p, self.rng = p, random.Random(seed)
        self.noise = {z: 0.0 for z in ZONES}

    def sample(self, true_c):
        p, out = self.p, {}
        innovation = p.ripple_sigma_c * (1 - p.ripple_rho ** 2) ** 0.5
        for zone, value in true_c.items():
            self.noise[zone] = p.ripple_rho * self.noise[zone] + self.rng.gauss(0.0, innovation)
            spike = self.rng.uniform(*p.spike_c) if self.rng.random() < p.spike_rate else 0.0
            out[zone] = value + self.noise[zone] + spike
        out["TSOC"] = max(out["TS0P"], out["TS1P"])  # firmware max (no GPU in this twin)
        return out


class TrendWindow:
    """Least-squares trend and slope over the last 2 s (temperature_slope.py)."""

    def __init__(self, window_s=2.0):
        self.window_s, self.history = window_s, []

    def add(self, t, readings):
        self.history = [(ht, r) for ht, r in self.history if t - ht <= self.window_s]
        self.history.append((t, readings))
        if len(self.history) < 2:
            return None
        times = [ht for ht, _ in self.history]
        mean_t = sum(times) / len(times)
        spread = sum((x - mean_t) ** 2 for x in times)
        result = {}
        for zone in readings:
            values = [r[zone] for _, r in self.history]
            mean_v = sum(values) / len(values)
            slope = sum((x - mean_t) * (v - mean_v) for x, v in zip(times, values)) / spread
            result[zone] = (mean_v + slope * (times[-1] - mean_t), max(0.0, slope))
        return result


class GuardProjection:
    """The guard's CPU-zone abort rules (safety.py): raw, immediate, confirmed."""

    def __init__(self):
        self.since, self.events = {}, []

    def check(self, t, raw, trends):
        for zone, (trend, rise) in trends.items():
            if raw[zone] >= ABORT_C:
                self.events.append((t, zone, "raw", raw[zone], trend, rise))
                continue
            projected = (trend >= ABORT_C - PREDICTION_BAND_C
                         and trend + PREDICTION_S * rise >= ABORT_C)
            if not projected:
                self.since.pop(zone, None)
                continue
            start = self.since.setdefault(zone, t)
            if trend >= ABORT_C - PREDICTION_IMMEDIATE_C:
                self.events.append((t, zone, "immediate", raw[zone], trend, rise))
            elif t - start >= PREDICTION_CONFIRM_S:
                self.events.append((t, zone, "confirmed", raw[zone], trend, rise))


def policy_projection(trends):
    """energy_control.policy.ShadowPolicy._cpu_projection on the twin's zones:
    (worst projection, that zone's trend)."""
    return max((trend + PREDICTION_S * rise if trend >= ABORT_C - PREDICTION_BAND_C else trend,
                trend) for trend, rise in trends.values())


def run(settings, *, seconds=1200.0, dt=0.25, utilisation=None, gpu_w=12.0,
        load_start_s=10.0, seed=1, recovery_ratio_s=0.03, feed_projection=True,
        twin=None, cluster_control=True, disturbance=None):
    """Closed loop: Supervisor + the policy's CPU output shaping (slew, int MHz),
    per cluster when ``cluster_control`` (policy._cluster_zones/_cluster_outputs)."""
    twin = twin or TwinParameters()
    utilisation = utilisation or {c: 1.0 for c in CLUSTERS}
    plant = ClusterZonePlant(twin, gpu_w=gpu_w, fan_state=12, seed=seed)
    policy_sensor, guard_sensor = RippleSensor(twin, seed + 101), RippleSensor(twin, seed + 202)
    policy_trend, guard_trend = TrendWindow(), TrendWindow()
    guard = GuardProjection()
    control = Supervisor(settings)
    caps = {"E0": float(SLOW_MAX), "P0": float(FAST_MAX), "E1": float(SLOW_MAX), "P1": float(FAST_MAX)}
    spans = {c: ((FAST_MAX - FAST_MIN) if c.startswith("P") else (SLOW_MAX - SLOW_MIN) / 0.75)
             for c in CLUSTERS}
    rows, previous_demand = [], False
    for k in range(int(seconds / dt)):
        t = k * dt
        loaded = t >= load_start_s
        util = utilisation if loaded else {c: 0.03 for c in CLUSTERS}
        if disturbance is not None:  # (time_s, {cluster: util}, gpu_w) steps
            for at_s, util_step, gpu_step in disturbance:
                if t >= at_s:
                    util = {**util, **util_step}
                    plant.gpu_w = gpu_step if gpu_step is not None else plant.gpu_w
        true_c = plant.zones()
        guard_raw = guard_sensor.sample(true_c)
        guard_trends = guard_trend.add(t, guard_raw)
        if guard_trends is not None:
            guard.check(t, guard_raw, guard_trends)
        sensed = policy_sensor.sample(true_c)
        trends = policy_trend.add(t, sensed)
        if trends is None:
            plant.advance({c: int(v) for c, v in caps.items()}, util, dt)
            continue
        demand = loaded
        zones = None
        if cluster_control:
            zones = tuple((trends[z][0], trends[z][0] + PREDICTION_S * trends[z][1]
                           if trends[z][0] >= ABORT_C - PREDICTION_BAND_C else trends[z][0])
                          for z in ("TS0E", "TS0P", "TS1E", "TS1P"))
        observation = Observation(
            cpu_c=max(trend for trend, _ in trends.values()),
            gpu_c=plant.copper_c + gpu_w / 1.9, gpu_util=0.9 if gpu_w > 20 else 0.02,
            active_jobs=4 if gpu_w > 20 else 0, cpu_demand_active=demand,
            cpu_work_arrival=demand and not previous_demand,
            cpu_util=sum(util.values()) / 4, cpu_zones=zones,
            cluster_util=tuple(util[c] for c in CLUSTERS) if cluster_control else None,
            **(dict(zip(("cpu_projected_c", "cpu_projected_basis_c"), policy_projection(trends)))
               if feed_projection else {}))
        previous_demand = demand
        command = control.step(observation, dt, track_cpu=False)
        if command.mode == "FAULT":
            rows.append({"t": t, "fault": command.reason})
            break
        if command.cluster_ratios is not None:
            wanted = dict(zip(CLUSTERS, cluster_caps_mhz(command.cluster_ratios)))
        else:
            fast_w, slow_w = cpu_class_caps(command.cpu_ratio)
            wanted = {"E0": slow_w, "P0": fast_w, "E1": slow_w, "P1": fast_w}
        for c in CLUSTERS:
            caps[c] = min(wanted[c], caps[c] + spans[c] * recovery_ratio_s * dt)
        out = {c: int(v) for c, v in caps.items()}
        fast_out, slow_out = max(out["P0"], out["P1"]), max(out["E0"], out["E1"])
        applied = min(command.cpu_ratio, (fast_out - FAST_MIN) / (FAST_MAX - FAST_MIN),
                      1.0 if slow_out == SLOW_MAX else
                      0.75 * (slow_out - SLOW_MIN) / (SLOW_MAX - SLOW_MIN))
        control.cpu.track(max(0.0, min(1.0, applied + control.cpu_track_offset)), dt)
        plant.fan_state = command.fan_state
        plant.advance(out, util, dt)
        rows.append({"t": t, "hottest_c": max(true_c.values()), "cpu_c": observation.cpu_c,
                     "projected_c": observation.cpu_projected_c,
                     "setpoint_c": control.cpu_setpoint, "fast_mhz": fast_out,
                     "slow_mhz": slow_out, "clusters_mhz": out, "fan": command.fan_state,
                     "zones_c": {z: true_c[z] for z in ("TS0P", "TS1P")},
                     "throughput": sum(util[c] * out[c] / (FAST_MAX if c.startswith("P") else SLOW_MAX)
                                       for c in CLUSTERS) / 4})
    return rows, guard.events


def summarise(rows, events, *, steady_from_s=300.0):
    steady = [r for r in rows if "fault" not in r and r["t"] >= steady_from_s]
    if not steady:
        return {"fault": next((r["fault"] for r in rows if "fault" in r), None)}
    n = len(steady)
    return {"hottest_mean_c": sum(r["hottest_c"] for r in steady) / n,
            "hottest_max_c": max(r["hottest_c"] for r in rows if "fault" not in r),
            "setpoint_mean_c": sum(r["setpoint_c"] for r in steady) / n,
            "fast_mean_mhz": sum(r["fast_mhz"] for r in steady) / n,
            "slow_mean_mhz": sum(r["slow_mhz"] for r in steady) / n,
            "throughput_mean": sum(r["throughput"] for r in steady) / n,
            "fan_last": steady[-1]["fan"],
            "guard_events": len(events),
            "guard_first": events[0][:3] if events else None,
            "fault": next((r["fault"] for r in rows if "fault" in r), None)}


def main(argv=None):
    import argparse
    from energy_control.broker import Config
    from energy_control.policy import ShadowPolicy
    parser = argparse.ArgumentParser(description="CPU loop vs guard projection, ripple twin")
    parser.add_argument("--target", type=float, default=90.0)
    parser.add_argument("--minutes", type=float, default=20.0)
    parser.add_argument("--integrator", choices=("conditional", "tracking"), default="conditional")
    parser.add_argument("--fan-policy", choices=("load", "staging"), default="load")
    parser.add_argument("--gpu-w", type=float, default=12.0)
    parser.add_argument("--no-projection", action="store_true")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args(argv)
    settings = ShadowPolicy._settings(Config(
        gpu_max_mhz=2000, gpu_entry_mhz=1700, cpu_target_c=args.target, fan_min_state=2,
        pid_integrator=args.integrator, fan_policy=args.fan_policy))
    rows, events = run(settings, seconds=args.minutes * 60, gpu_w=args.gpu_w, seed=args.seed,
                       feed_projection=not args.no_projection)
    for key, value in summarise(rows, events).items():
        print(f"{key}: {value:.2f}" if isinstance(value, float) else f"{key}: {value}")


if __name__ == "__main__":
    main()
