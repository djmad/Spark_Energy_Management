"""Energy-conserving cooler twin for the dashboards (pure, no device I/O).

Two stores in series (analysis/sink_fit.py, refit 28 September 2026 evening
with the day's long LLM runs at 2.5 GHz: GPU 5-53 W, fan floors 2-12, room air
measured 21 C):

  plate  Cp dTp/dt = P_in - Gn (Tp - Tf)                  die + contact plate
  fins   Cf dTf/dt = Gn (Tp - Tf) - Ga(fan) (Tf - T_room)  fin block + case air
  Ga(fan) = g0 + g1 * max(0.2, floor / 12)
  P_in   = P_GPU (measured) + P_CPU (calorimetric estimate) + background (fitted)
  TGPU   = Tp + (r0 + r1 * a) * P_GPU                      (observation, for the residual only)
  a      = (P_GPU - 4.5 W) / (P_matmul(f) - 4.5 W)            GPU activity, 0..1.2

The stores are integrated from the power inputs only. The heat balance is
therefore consistent by construction: input = removal + charge at every step,
and in steady state removal equals input. The model's error shows up as the
residual between predicted and measured TGPU; it never leaks into the balance.
(The earlier view derived the fin temperature from the plate estimate,
T_f = T_p - P_in / Gn, which turned every kelvin of plate-estimate error into
3-5 W of false "charging".)

GPU activity: P_matmul(f) is the measured matrix burn-in power at the GPU
clock f (simulation.model.GB10_GPU_MATMUL_W), so a is about 1 for the burn-in
and 0.2-0.5 for LLM decode. Dense matrix work concentrates the heat in the
compute units, so TGPU sits higher above the plate per GPU watt. LLM decode
spreads the same power over memory and fabric. The term changes only the TGPU
observation, not the heat balance. The first refit (morning of 28 September,
common r = 0.483) ran 4.3 K warm for LLM at 2.5 GHz and 40 W or more.
"""
from dataclasses import dataclass
from math import isfinite

from simulation.model import GB10_GPU_MATMUL_W


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


@dataclass(frozen=True)
class CoolerParams:
    version: str = "fitted 28 Sep 2026 evening (5-53 W, floors 2-12, LLM at 2.5 GHz; holdout 2.04 K)"
    plate_j_k: float = 23.1        # die + contact plate (fast store)
    fins_j_k: float = 287.0        # fin block + case air (main store)
    neck_w_k: float = 2.96         # plate -> fin block
    air_base_w_k: float = 2.56     # fin block -> room, fan-independent
    air_fan_w_k: float = 2.44      # fin block -> room, x max(0.2, floor / 12)
    fan_min_share: float = 0.2
    background_w: float = 17.7     # board, RAM, NIC (heatpipe), idle SoC
    room_c: float = 21.0           # intake air, measured by the operator (no live sensor)
    hotspot_k_w: float = 0.320     # TGPU above the plate per GPU watt at activity 0
    hotspot_act_k_w: float = 0.090  # added per GPU watt at activity 1 (matrix burn-in)
    idle_gpu_w: float = 4.5        # GPU power at idle (the matrix fit's offset)
    default_activity: float = 0.4  # without a clock reading (typical LLM decode)

    def activity(self, gpu_w, gpu_mhz):
        """GPU power as a share of the matrix burn-in's power at this clock."""
        if not (_finite(gpu_w) and _finite(gpu_mhz) and gpu_mhz > 0):
            return self.default_activity
        span = GB10_GPU_MATMUL_W(gpu_mhz) - self.idle_gpu_w
        return min(1.2, max(0.0, (gpu_w - self.idle_gpu_w) / span)) if span > 1.0 else self.default_activity

    def hotspot_w_k(self, gpu_w, gpu_mhz):
        return self.hotspot_k_w + self.hotspot_act_k_w * self.activity(gpu_w, gpu_mhz)

    def air_w_k(self, fan_floor):
        share = max(self.fan_min_share, min(1.0, max(0.0, fan_floor) / 12.0))
        return self.air_base_w_k + self.air_fan_w_k * share


PARAMS = CoolerParams()
MAX_STEP_S = 300.0      # longer gaps: the twin restarts from steady state
SUBSTEP_S = 0.5


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

    def update(self, t_s, gpu_w, cpu_w, fan_floor, tgpu_c=None, gpu_mhz=None):
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
        return self.state(gpu_w, cpu, fan_floor, tgpu_c, restart, gpu_mhz)

    def state(self, gpu_w, cpu_w, fan_floor, tgpu_c, restarted=False, gpu_mhz=None):
        p = self.p
        p_in = max(0.0, gpu_w) + cpu_w + p.background_w
        g_air = p.air_w_k(fan_floor)
        neck = p.neck_w_k * (self.plate_c - self.fins_c)
        out = g_air * (self.fins_c - p.room_c)
        steady_plate, steady_fins = self.steady(p_in, fan_floor)
        activity = p.activity(gpu_w, gpu_mhz)
        hotspot = p.hotspot_k_w + p.hotspot_act_k_w * activity
        predicted = self.plate_c + hotspot * max(0.0, gpu_w)
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
            "hotspot_k_w": r(hotspot, 3), "gpu_activity": r(activity, 2),
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
                       fan.get("floor"), zones.get("TGPU"), gpu.get("measured_mhz"))
