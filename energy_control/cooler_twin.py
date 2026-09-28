"""Energy-conserving cooler twin for the dashboards (pure, no device I/O).

Two stores in series (analysis/sink_fit.py, refit 28 September 2026 over the
full power range, 5-53 W GPU, fan floors 2-12; room air measured 21 C):

  plate  Cp dTp/dt = P_in - Gn (Tp - Tf)                  die + contact plate
  fins   Cf dTf/dt = Gn (Tp - Tf) - Ga(fan) (Tf - T_room)  fin block + case air
  Ga(fan) = g0 + g1 * max(0.2, floor / 12)
  P_in   = P_GPU (measured) + P_CPU (calorimetric estimate) + background (fitted)
  TGPU   = Tp + r * P_GPU                                  (observation, for the residual only)

The stores are integrated from the power inputs only. The heat balance is
therefore consistent by construction: input = removal + charge at every step,
and in steady state removal equals input. The model's error shows up as the
residual between predicted and measured TGPU; it never leaks into the balance.
(The earlier view derived the fin temperature from the plate estimate,
T_f = T_p - P_in / Gn, which turned every kelvin of plate-estimate error into
3-5 W of false "charging".)

Known limit: the GPU power reading does not carry all of the load's heat in the
same way for every workload. The memory-heavy matrix burn-in heats the die more
per reported GPU watt than LLM decode, so at the same GPU power the twin runs
about 7 K warm for LLM loads (holdout 28 September 2026).
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class CoolerParams:
    version: str = "fitted 28 Sep 2026 (5-53 W, floors 2-12; holdout 1.95 K)"
    plate_j_k: float = 32.2        # die + contact plate (fast store)
    fins_j_k: float = 430.0        # fin block + case air (main store)
    neck_w_k: float = 3.15         # plate -> fin block
    air_base_w_k: float = 2.46     # fin block -> room, fan-independent
    air_fan_w_k: float = 2.60      # fin block -> room, x max(0.2, floor / 12)
    fan_min_share: float = 0.2
    background_w: float = 16.9     # board, RAM, NIC (heatpipe), idle SoC
    room_c: float = 21.0           # intake air, measured by the operator (no live sensor)
    hotspot_k_w: float = 0.483     # TGPU above the plate per GPU watt

    def air_w_k(self, fan_floor):
        share = max(self.fan_min_share, min(1.0, max(0.0, fan_floor) / 12.0))
        return self.air_base_w_k + self.air_fan_w_k * share


PARAMS = CoolerParams()
MAX_STEP_S = 300.0      # longer gaps: the twin restarts from steady state
SUBSTEP_S = 0.5


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


class CoolerTwin:
    """Integrates the two stores from 1 Hz status samples; ``update`` returns
    the published state (a plain dict), or None when inputs are missing."""

    def __init__(self, params=PARAMS):
        self.p = params
        self.t = None
        self.plate_c = self.fins_c = None
        self.inputs = None

    def steady(self, p_in_w, fan_floor):
        g_air = self.p.air_w_k(fan_floor)
        fins = self.p.room_c + p_in_w / g_air
        return fins + p_in_w / self.p.neck_w_k, fins

    def update(self, t_s, gpu_w, cpu_w, fan_floor, tgpu_c=None):
        if not (_finite(t_s) and _finite(gpu_w) and _finite(fan_floor)):
            return None
        cpu = cpu_w if _finite(cpu_w) and cpu_w >= 0 else 0.0
        p_in = max(0.0, gpu_w) + cpu + self.p.background_w
        restart = self.t is None or not 0 <= t_s - self.t <= MAX_STEP_S
        if restart:
            self.plate_c, self.fins_c = self.steady(p_in, fan_floor)
        else:
            dt, (last_in, last_floor) = t_s - self.t, self.inputs
            steps = max(1, int(dt / SUBSTEP_S + 0.999))
            h = dt / steps
            g_air = self.p.air_w_k(last_floor)
            for _ in range(steps):   # zero-order hold on the previous sample's inputs
                q_neck = self.p.neck_w_k * (self.plate_c - self.fins_c)
                q_air = g_air * (self.fins_c - self.p.room_c)
                self.plate_c += h * (last_in - q_neck) / self.p.plate_j_k
                self.fins_c += h * (q_neck - q_air) / self.p.fins_j_k
        self.t, self.inputs = t_s, (p_in, fan_floor)
        return self.state(gpu_w, cpu, fan_floor, tgpu_c, restart)

    def state(self, gpu_w, cpu_w, fan_floor, tgpu_c, restarted=False):
        p = self.p
        p_in = max(0.0, gpu_w) + cpu_w + p.background_w
        g_air = p.air_w_k(fan_floor)
        neck = p.neck_w_k * (self.plate_c - self.fins_c)
        out = g_air * (self.fins_c - p.room_c)
        steady_plate, steady_fins = self.steady(p_in, fan_floor)
        predicted = self.plate_c + p.hotspot_k_w * max(0.0, gpu_w)
        r = lambda v, d=2: round(v, d)
        return {
            "version": p.version, "restarted": restarted,
            "room_c": p.room_c, "background_w": p.background_w,
            "in_w": r(p_in), "gpu_w": r(gpu_w), "cpu_w": r(cpu_w),
            "plate_c": r(self.plate_c), "fins_c": r(self.fins_c),
            "neck_w": r(neck), "out_w": r(out),
            "plate_charge_w": r(p_in - neck), "fins_charge_w": r(neck - out),
            "charge_w": r(p_in - out),
            "plate_j": r(p.plate_j_k * (self.plate_c - p.room_c), 0),
            "fins_j": r(p.fins_j_k * (self.fins_c - p.room_c), 0),
            "steady_plate_c": r(steady_plate), "steady_fins_c": r(steady_fins),
            "to_steady_j": r(p.plate_j_k * (steady_plate - self.plate_c)
                             + p.fins_j_k * (steady_fins - self.fins_c), 0),
            "g_air_w_k": r(g_air, 3), "neck_w_k": p.neck_w_k,
            "plate_j_k": p.plate_j_k, "fins_j_k": p.fins_j_k,
            "tau_plate_s": r(p.plate_j_k / p.neck_w_k, 1), "tau_fins_s": r(p.fins_j_k / g_air, 0),
            "tgpu_predicted_c": r(predicted),
            "tgpu_measured_c": r(tgpu_c) if _finite(tgpu_c) else None,
            "tgpu_residual_k": r(tgpu_c - predicted) if _finite(tgpu_c) else None,
            "hotspot_k_w": p.hotspot_k_w,
        }


def twin_from_status(twin, payload):
    """Feed one energy_control status payload; None when it lacks the inputs."""
    if not isinstance(payload, dict):
        return None
    gpu = payload.get("gpu") if isinstance(payload.get("gpu"), dict) else {}
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    fan = payload.get("fan") if isinstance(payload.get("fan"), dict) else {}
    zones = payload.get("zones_c") if isinstance(payload.get("zones_c"), dict) else {}
    utc_ns = payload.get("utc_ns")
    if not _finite(utc_ns):
        return None
    return twin.update(utc_ns / 1e9, gpu.get("power_w"), cpu.get("est_power_w"),
                       fan.get("floor"), zones.get("TGPU"))
