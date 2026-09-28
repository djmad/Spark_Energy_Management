"""Synthetic coupled thermal plant and constrained PID controllers.

All thermal constants and GPU gains are illustrative, NOT hardware calibration.
Only the Python standard library is used. This module performs no file/device I/O.
"""
from dataclasses import dataclass, field
from math import exp, isfinite
import random
from energy_control.limits import ACPI_ABORT_C, GPU_HARD_MAX_MHZ  # shared envelope (no I/O)


def clamp(value, low, high):
    return max(low, min(high, value))


PID_INTEGRATORS = ("conditional", "tracking")
FAN_POLICIES = ("load", "staging", "predictive")


@dataclass(frozen=True)
class Gains:
    kp: float = 0.075      # normalized output / deg C
    ki: float = 0.012      # normalized output / (deg C s)
    kd: float = 0.060      # normalized output s / deg C
    derivative_tau: float = 1.0  # seconds
    tracking_tau: float = 2.0    # seconds (tracking integrator only)
    # "conditional": the retired guard's integrator (doc/48 §0, D2): integrate
    # only while the PID's own provisional output is inside 0..1 or the error
    # drives it back out of saturation; downstream limits are never tracked.
    # "tracking": the defect-21 back-calculation, kept selectable for A/B. It
    # pinned the CPU integral near 0 under sensor ripple (defect 27).
    integrator: str = "conditional"
    # Conditional integrator only: the integral winds down this many times
    # faster while above target. The CPU clusters (fast P zones, ~2 s lag)
    # otherwise sat 2-3 C above their setpoint for tens of seconds after a
    # disturbance (live combined load, 27 September 15:43, abort).
    wind_down_factor: float = 1.0

    def __post_init__(self):
        if not isfinite(self.wind_down_factor) or not 1.0 <= self.wind_down_factor <= 10.0:
            raise ValueError("PID wind-down factor must be 1..10")
        if not all(isfinite(v) and v >= 0 for v in (self.kp, self.ki, self.kd)):
            raise ValueError("PID gains must be finite and nonnegative")
        if not all(isfinite(v) and v > 0 for v in (self.derivative_tau, self.tracking_tau)):
            raise ValueError("PID time constants must be finite and positive")
        if self.integrator not in PID_INTEGRATORS:
            raise ValueError("PID integrator must be 'conditional' or 'tracking'")


class PID:
    """Headroom PID: positive error permits performance; derivative on measurement.

    With the conditional integrator (default) propose() integrates on the PID's
    own provisional output and track() only validates. With the tracking
    integrator, track() uses the final constrained actuator output for
    conditional integration and external-reset anti-windup.
    """
    def __init__(self, gains):
        self.gains = gains
        self.integral = 1.0
        self.previous = None
        self.slope = 0.0
        self.error = self.raw = 0.0
        self.p = self.d = 0.0

    def propose(self, temperature, target, dt):
        if not all(isfinite(v) for v in (temperature, target, dt)) or dt <= 0:
            raise ValueError("finite temperatures and positive dt required")
        raw_slope = 0.0 if self.previous is None else (temperature - self.previous) / dt
        alpha = 1.0 - exp(-dt / self.gains.derivative_tau)
        self.slope += alpha * (raw_slope - self.slope)
        self.previous = temperature
        self.error = target - temperature
        self.p = self.gains.kp * self.error
        self.d = -self.gains.kd * self.slope
        if self.gains.integrator == "conditional":
            # Same arithmetic as spark-cpu-thermal-guard.js (Archive): the
            # integral starts at 1 and stays there while the output saturates
            # below target, so the loop relieves only near its own target.
            provisional = self.integral + self.p + self.d
            if (0.0 < provisional < 1.0 or (provisional >= 1.0 and self.error < 0)
                    or (provisional <= 0.0 and self.error > 0)):
                rate = self.gains.ki * (self.gains.wind_down_factor if self.error < 0 else 1.0)
                self.integral = clamp(self.integral + rate * self.error * dt, 0.0, 1.0)
        self.raw = self.p + self.integral + self.d
        return clamp(self.raw, 0.0, 1.0)

    def track(self, applied, dt):
        if not isfinite(applied) or not 0 <= applied <= 1 or not isfinite(dt) or dt <= 0:
            raise ValueError("normalized applied output and positive dt required")
        if self.gains.integrator == "conditional":
            return  # Integration happens in propose(); downstream limits are not tracked.
        # Freeze integration when it would push further into any downstream limit.
        blocked = ((self.raw > applied and self.error > 0)
                   or (self.raw < applied and self.error < 0))
        increment = 0.0 if blocked else self.gains.ki * self.error * dt
        # Back-calculation only toward the PID's own saturation (raw > 1) or
        # up to an actuator held above the request (floors). A downstream
        # performance limit below the request (entry ceiling, ramp) must not
        # drain the headroom integral: live 27 September 2026 (TH run 10) a
        # load-start derivative kick plus the entry/ramp limits drained it from
        # 1 to ~0 in 20 s, and the P term alone then cycled the GPU cap
        # between ~800 and 1800 MHz ~6 C below its target.
        if self.raw > 1.0:
            reference = 1.0
        elif applied > self.raw:
            reference = applied
        else:
            reference = self.raw
        tracking = (1.0 - exp(-dt / self.gains.tracking_tau)) * (reference - self.raw)
        self.integral = clamp(self.integral + increment + tracking, 0.0, 1.0)


@dataclass(frozen=True)
class RandomLoad:
    gpu_min_pct: int = 0
    gpu_max_pct: int = 100
    cpu_min_pct: int = 0
    cpu_max_pct: int = 100
    hold_s: float = 5.0
    seed: int = 42

    def __post_init__(self):
        for low, high in ((self.gpu_min_pct, self.gpu_max_pct),
                          (self.cpu_min_pct, self.cpu_max_pct)):
            if not all(type(v) is int for v in (low, high)) or not 0 <= low <= high <= 100:
                raise ValueError("load ranges require integer 0 <= min <= max <= 100 percent")
        if not isfinite(self.hold_s) or not 0.25 <= self.hold_s <= 60:
            raise ValueError("random load hold must be 0.25..60 simulated seconds")
        if type(self.seed) is not int or not 0 <= self.seed <= 1_000_000:
            raise ValueError("seed must be an integer in 0..1000000")

    def sample(self, time_s):
        # A slot-local generator makes demand independent of render/control cadence.
        slot = int(time_s / self.hold_s)
        rng = random.Random(f"{self.seed}:{slot}")
        return (rng.randint(self.gpu_min_pct, self.gpu_max_pct) / 100,
                rng.randint(self.cpu_min_pct, self.cpu_max_pct) / 100, slot)


@dataclass(frozen=True)
class QueueLoad:
    """Waiting and active requests are independent demand inputs."""
    cpu_cores: int = 5
    queued_jobs: int = 12
    active_jobs: int = 4

    def __post_init__(self):
        for value in (self.cpu_cores, self.queued_jobs, self.active_jobs):
            if type(value) is not int or not 0 <= value <= 20:
                raise ValueError("CPU cores and LLM job counts must be integers in 0..20")


@dataclass(frozen=True)
class Settings:
    baseline_mhz: float = 1200.0  # synthetic cold-entry cap, not hardware-qualified
    maximum_mhz: float = 1800.0  # absolute user-specified ceiling
    minimum_mhz: float = 500.0
    ramp_mhz_s: float = 100.0
    normal_down_mhz_s: float = 150.0
    busy_threshold: float = 0.95
    busy_dwell_s: float = 1.0
    low_load_timeout_s: float = 1.0
    # GPU utilisation below this fraction counts as idle. GB10 reads 8-9 % with
    # nothing running (display, driver housekeeping), so the old 5 % never
    # released a productive cap without an LLM prefill (doc/42 defect 36;
    # operator, 27 September 2026: "Leerlaufschwelle 20 % ist gut").
    idle_util_threshold: float = 0.20
    cpu_target_c: float = 90.0  # goal v2 operator targets
    gpu_target_c: float = 75.0
    cpu_emergency_c: float = ACPI_ABORT_C  # fixed abort limits: ACPI (limits.py), GPU 85 C
    gpu_emergency_c: float = 85.0
    # Last-resort derate band below each abort limit. 1.0 C (was 2.5): at the
    # new operating point (~87 C) the noisy slope projection entered a 2.5 C
    # band on ~1 % of ticks, and each entry chopped the CPU cap by ~1 GHz
    # (live sawtooth, 27 September 2026; fast twin: cap s.d. 223 -> 58 MHz).
    guard_band_c: float = 1.0
    cpu_recovery_s: float = 0.03
    cpu_entry_ratio: float = 0.5  # synthetic normalized fast-class cap
    cpu_normal_down_s: float = 0.1
    prediction_s: float = 2.0
    prediction_band_c: float = 10.0  # projection only within this band below each abort
    # A PID may request relief only within this band below its target. Far
    # below target the integral sits unwound at full headroom and P-cluster
    # spikes (several C/s) let the derivative alone cut the GPU to its spill
    # bound at ACPI ~72 C (live, 27 September 2026, fan floor 2).
    pid_band_c: float = 12.0
    # A projected breach must persist this long (P-cluster spikes, doc/42).
    prediction_confirm_s: float = 1.0
    prediction_immediate_c: float = 3.0  # within this of a limit: no confirmation wait
    # Fast-core reservation while LLM work runs (normalized fast-class cap).
    # Measured 27 September 2026 (doc/45): fast caps at minimum cost ~3 % decode
    # (threads move to the E-cores), the whole CPU at 1.4 GHz ~7.5 %; a small
    # reservation keeps the balance free to relieve the CPU with CPU caps.
    cpu_reservation_ratio: float = 0.10
    reservation_yield_c: float = 1.5  # reservation yields this far above CPU target
    # Power balance: throughput cost per watt removed (twin-based allocation).
    balance: bool = True
    # Measured LLM throughput per watt removed (doc/45): GPU ~1.6 %/W at
    # 1800 MHz, CPU ~0.5 %/W (CPU power estimated). A lower CPU cost is kept
    # back until the CPU twin node is fitted: with the synthetic CPU node it
    # over-relieved the CPU (hot-ambient scenario 23 C under target).
    gpu_cost_per_w: float = 1.0
    cpu_cost_per_w: float = 0.6
    # Workload priorities (doc/48 §0.1, D5; operator: settable from the
    # dashboard). The balance weighs a cut by who pays for it: the LLM pays
    # for GPU cuts (gpu_cost_per_w) and a little for CPU cuts (cpu_cost_per_w,
    # its engine thread), CPU jobs pay cpu_job_cost_per_w for CPU cuts. Each
    # active workload's weight is its priority divided by its current relative
    # speed (proportional fairness: the more a load is already slowed, the
    # dearer it gets). Inactive workloads cost nothing.
    priority_gpu: float = 1.0  # GPU (LLM) : CPU = 1 : 1 (operator, 27 September 2026)
    priority_cpu: float = 1.0
    # Same units as gpu_cost_per_w (1.0 ~ 1.6 % LLM throughput per W): CPU-job
    # throughput ~ f, power ~ f^1.7, so ~1.7 %/W of CPU power at ~35 W.
    cpu_job_cost_per_w: float = 1.06
    cpu_job_util: float = 0.15       # aggregate utilisation above which CPU jobs count as active
    cpu_spill_fraction: float = 0.25  # max share of GPU power cut on behalf of the CPU
    # Additive fan floor staging (states 0..12).
    fan_min_state: int = 0
    fan_preferred_state: int = 6
    fan_derate_dwell_s: float = 5.0
    fan_step_s: float = 3.0
    fan_down_dwell_s: float = 15.0
    # Raise above the preferred level only when a loop's balanced headroom
    # falls below boost (or a sensor stays above target); release at or above
    # release. The gap is hysteresis against fan/clock limit cycles.
    fan_boost_headroom: float = 0.6
    fan_target_band_c: float = 1.0  # "above target" for fan staging, past PID overshoot
    fan_release_headroom: float = 0.75
    # False only once idle -> 100 % at the maximum is qualified with margin.
    entry_fallback: bool = True
    # REARM to the entry ceiling on each new prefill. Off (operator, 28 September
    # 2026: "we detect load only on GPU utilisation, prefill we don't need to
    # look at any more; example is our burn-in test"). Every prompt that joined a
    # running decode dropped the cap to the entry ceiling for ~8 s, and the
    # prefill burst itself already ran at full clock (vLLM counters are read at
    # 1 Hz). Cold starts keep the entry ceiling: the idle cooldown returns the cap
    # to it, and the ramp starts only at busy utilisation.
    prefill_rearm: bool = False
    emergency_hysteresis_c: float = 5.0
    emergency_recovery_s: float = 10.0
    # Fan policy (doc/48 §0, D1). "load": the floor sits at fan_load_state
    # whenever the machine carries load (LLM work, busy GPU or CPU demand) and
    # drops to fan_min_state only after fan_idle_delay_s without load; the
    # copper (tau 50-90 s) must already be cold when load arrives. "staging":
    # the goal-v2 preferred-level staging (_stage_fan), kept for A/B.
    fan_policy: str = "load"
    fan_load_state: int = 12
    fan_idle_delay_s: float = 300.0
    # Predictive fan (fan_policy "predictive"; operator, 27 September 2026:
    # "control it based on our temperature + predictions with our current load
    # profile, so it spins up early"; the temperature swings "produce thermal
    # stress"). The fan acts on the slow part of the cooler (fin block and case
    # air, tau 80-120 s); the die's fast swings follow the load's own power.
    #   feed-forward: the lowest level whose steady plate estimate for the
    #     expected power stays at or below fan_plate_target_c;
    #   feedback: every loop's guard projection within fan_fb_band_c of its
    #     setpoint adds fan, 12 at fan_fb_full_c below it (fan before clocks);
    #   up at once, down one level per fan_down_dwell_s (gentle cool-down).
    # Cooler constants: analysis/sink_fit.py (two stores, room 21 C measured,
    # fan floors 2-12; holdout 2.4 K). fan_load_state caps the level outside
    # the near-abort case; the operator's fan_min_state is the floor.
    fan_plate_target_c: float = 55.0
    fan_fb_band_c: float = 12.0
    fan_fb_full_c: float = 3.0
    fan_neck_w_k: float = 3.45        # plate -> fin block
    fan_air_g0_w_k: float = 1.92      # fin block -> room, fan-independent
    fan_air_g1_w_k: float = 1.50      # fin block -> room per fan share (floor / 12, min 0.2)
    fan_room_c: float = 21.0          # intake air (operator reading, 27 Sep 2026)
    fan_background_w: float = 12.3    # board, memory, NIC, idle SoC (fitted)
    # Load-start anticipation: while the GPU turns active, its expected power is
    # the matrix-load power at the current cap for fan_anticipate_s, then the
    # measured power (peak-held, decaying with fan_power_decay_s).
    fan_anticipate_s: float = 30.0
    fan_power_decay_s: float = 300.0  # peak-hold of the expected power: bursts every 1-5 min keep the fan up
    # One level down per fan_release_step_s (operator, 28 September 2026: the
    # release to lower speeds was too aggressive: 12 -> 6 within 90 s between
    # prefill bursts, a sawtooth). 60 s: 12 -> 6 takes 6 min.
    fan_release_step_s: float = 60.0
    # Operator per-cluster maxima (E0, P0, E1, P1) as ratios of each class's
    # hardware span; each cluster loop treats its value as a bound, so a raise
    # ramps in the loop's controlled way and a reduction applies at once.
    cluster_max_ratio: tuple = (1.0, 1.0, 1.0, 1.0)
    # GPU cap ramp slows with the TGPU zone's projection headroom below its
    # setpoint: full rate beyond this band, recovery_taper_min at the setpoint.
    gpu_zone_taper_band_c: float = 10.0
    # The TGPU zone loop's own gains (was the CPU clusters' gains: live GPU-only
    # burn-in at 2200 MHz, 27 September 2026, jumped 2150 -> 1725 MHz every
    # 10-15 s). TGPU is spiky: the burn-in's own power swings 46-68 W at a fixed
    # clock move it up to 7 C/s. Gentler P, no derivative kick (twin-tuned).
    # Twin grid (burn-in wobble +-13 % / 10 s and +30 % bursts, 0 and 35 W CPU):
    # kp 0.02, ki 0.004, kd 0 with margin 3 C gives 0 guard events, ~110 MHz
    # peak-to-peak per 30 s (old: 280-340 MHz, 67-100 hard cuts), TGPU <= 91 C.
    gpu_zone_gains: Gains = field(default_factory=lambda: Gains(0.02, 0.004, 0.0, 3.0))
    # A TGPU projection spike (trend still below the band) cuts the GPU cap by
    # at most this fraction of its span per control tick instead of dropping to
    # the entry ceiling at once; a real runaway (trend in the band) cuts fully.
    gpu_zone_spike_step: float = 0.02
    # TGPU regulates this far below the shared zone ceiling: the burn-in's own
    # power swings move TGPU by 5-7 C within 1-2 s, and that must fit below the
    # guard's immediate zone instead of being regulated away (twin, doc/55).
    gpu_zone_margin_c: float = 3.0
    # Guard-margin adaptive CPU setpoint (doc/48 §0, D3; doc/42 defect 28).
    # The guard aborts at once when a zone's 2 s trend is >= 90 C and its
    # 2 s projection reaches 93 C. While the projection comes within
    # guard_margin_c of the abort the effective setpoint backs off at
    # setpoint_backoff_c_s; otherwise it recovers toward cpu_target_c at
    # setpoint_recovery_c_s. The ratio of the two rates is the tolerated
    # near-miss fraction (0.5 %); the back-off is bounded below the target.
    # Ripple twin (simulation/cluster_twin.py, 3 x 1 h at target 90): margin
    # 1.5 C / recovery 0.02 C/s tripped the guard 64 times, 2.5 C / 0.005 C/s
    # never, settling at ~87.3 C with +14 % CPU throughput over defect 27.
    # 2.0 C / 0.02 C/s (was 2.5 / 0.005): with the 87 C ceiling doing the main
    # safety work the back-off is secondary; live (Stage B, 27 September) the
    # slow recovery let a few early near-misses hold a cluster ~3 C low for
    # minutes (-5 % throughput); the cluster twin shows equal trip counts for
    # 2.5/0.005 ... 1.0/0.2.
    guard_margin_c: float = 2.0
    # The effective setpoint never enters the guard's no-confirmation zone
    # (trend >= abort - prediction_immediate_c = 90 C): one +3 C ripple step
    # lifts the 2 s trend by ~1.25 C, and the guard samples independently of
    # the policy, so it stays trend_margin_c below it (87 C today) whatever the
    # target. Fast twin, 2 s P-zone lag, 6 x 30 min per load: margin 1.5 C
    # tripped at onset, 2.5 C once in 3 h, 3.0 C never, at unchanged
    # throughput (0.934 heavy / 0.980 light vs 0.811 / 0.895 before).
    # 4.0 C (86 C ceiling) after the live combined-load abort (27 September
    # 15:43): LLM + 10 workers pushed TS0P 2.3 C over its cluster setpoint
    # before the integral wound down, and a jump then held the projection
    # over 93 C for 1 s. Costs ~1 % CPU clock.
    trend_margin_c: float = 4.0
    setpoint_backoff_c_s: float = 1.0
    setpoint_recovery_c_s: float = 0.02
    setpoint_backoff_max_c: float = 6.0
    # Recovery taper (live Stage A run, 27 September 2026): ramping the CPU cap
    # at the full recovery rate into the limit heated the P zones by 2-3 C/s
    # (fast GB10 thermals), the 2 s projection ran 4-6 C ahead of the trend and
    # the last-resort derate band chopped the caps by ~1 GHz every 15-30 s
    # (sawtooth). The upward rate now scales with the guard-style projection's
    # headroom below the effective setpoint: full rate beyond taper_band_c,
    # recovery_taper_min of it at the setpoint. Reductions stay immediate.
    # Band 15 C / minimum 5 % (was 8 C / 10 %): live the P zones rise ~34 C per
    # GHz near the top, so the full rate (~72 MHz/s) heats them ~2.4 C/s and
    # the 2 s projection runs ~5 C ahead; the approach must slow from ~72 C.
    recovery_taper_band_c: float = 15.0
    recovery_taper_min: float = 0.05
    # Cluster loops relieve only when their zone's guard projection comes within
    # this of their setpoint. Live 27 September: a single-thread burst lifted
    # TS1P 53 -> 73 C in 1 s (projection 70 C, 11 C under the setpoint) and the
    # derivative alone cut P1 to 1860 MHz and E1 to 968 MHz for ~30 s.
    relief_projection_margin_c: float = 2.0
    # ... but only for a lightly loaded cluster: under sustained load the same
    # rule removed the derivative braking of the approach, the caps ramped at
    # full rate into the limit and the derate band chopped them to minimum
    # (live Stage B, 27 September 15:17). Busy clusters keep pid_band_c.
    cluster_busy_util: float = 0.5
    # Learned full-load cap (mixed-load aborts, 27 September 15:43 and 15:59):
    # a partly loaded P cluster at full clock that suddenly takes full load
    # (thread migration, a job starting next to the LLM) jumps +25-28 C within
    # ~1 s, faster than any 4 Hz loop; the guard's projection then trips. Each
    # cluster learns the cap it holds at its setpoint under full load
    # (utilisation >= cluster_full_util, EMA learn_tau_s); while a P cluster is
    # only partly loaded its cap stays at learned - partial_cap_margin, so a
    # step lands at a clock the loop can hold. E zones stay far from their
    # limits (gains ~15-27 C), so E clusters start unconstrained (1.0).
    cluster_full_util: float = 0.9
    partial_cap_margin: float = 0.04       # ratio, ~100 MHz on a P cluster
    learn_tau_s: float = 30.0
    learned_cap_initial_p: float = 0.80    # ~3.4 GHz until the first full-load run
    cpu_gains: Gains = field(default_factory=Gains)
    gpu_gains: Gains = field(default_factory=lambda: Gains(0.06, 0.006, 0.08))
    random_load: RandomLoad = field(default_factory=RandomLoad)
    queue_load: QueueLoad = field(default_factory=QueueLoad)

    def __post_init__(self):
        zero_ok = {"cpu_reservation_ratio", "fan_min_state", "fan_preferred_state",
                   "fan_load_state"}
        numbers = [v for k, v in vars(self).items()
                   if type(v) in (int, float) and k not in zero_ok]
        if not all(isfinite(v) and v > 0 for v in numbers):
            raise ValueError("settings must be finite and positive")
        if type(self.balance) is not bool or type(self.entry_fallback) is not bool:
            raise ValueError("explicit boolean balance / entry-fallback flags required")
        if self.fan_policy not in FAN_POLICIES:
            raise ValueError("fan policy must be 'load' or 'staging'")
        if not 1.0 <= self.guard_margin_c <= 3.0:
            raise ValueError("guard margin must be 1..3 C below the abort")
        if (type(self.cluster_max_ratio) is not tuple or len(self.cluster_max_ratio) != 4
                or any(type(v) not in (int, float) or not isfinite(v) or not 0 <= v <= 1
                       for v in self.cluster_max_ratio)):
            raise ValueError("cluster maxima must be four ratios in 0..1")
        if not self.setpoint_recovery_c_s < self.setpoint_backoff_c_s:
            raise ValueError("setpoint recovery must be slower than its back-off")
        if not 0 < self.recovery_taper_min <= 1:
            raise ValueError("recovery taper minimum must be within 0..1")
        if not self.minimum_mhz < self.baseline_mhz <= self.maximum_mhz <= GPU_HARD_MAX_MHZ:
            raise ValueError(f"require minimum < baseline <= maximum <= {GPU_HARD_MAX_MHZ} MHz")
        if not 0 < self.busy_threshold <= 1:
            raise ValueError("invalid busy threshold")
        if not 0 < self.idle_util_threshold < self.busy_threshold:
            raise ValueError("idle utilisation threshold must lie below the busy threshold")
        if not all(isfinite(v) and v > 0 for v in (
                self.fan_neck_w_k, self.fan_air_g0_w_k, self.fan_air_g1_w_k, self.fan_power_decay_s,
                self.fan_release_step_s)) \
                or not 0 <= self.fan_fb_full_c < self.fan_fb_band_c or self.fan_anticipate_s < 0 \
                or self.fan_background_w < 0 or not self.fan_room_c < self.fan_plate_target_c:
            raise ValueError("invalid predictive fan settings")
        if self.cpu_entry_ratio > 1:
            raise ValueError("CPU entry ratio must be <= 1")
        if (type(self.cpu_reservation_ratio) not in (int, float)
                or not 0 <= self.cpu_reservation_ratio <= self.cpu_entry_ratio):
            raise ValueError("fast-core reservation must be within 0..CPU entry ratio")
        if not 0 < self.fan_boost_headroom < self.fan_release_headroom <= 1:
            raise ValueError("require 0 < fan boost headroom < release headroom <= 1")
        for value in (self.fan_min_state, self.fan_preferred_state, self.fan_load_state):
            if type(value) is not int or not 0 <= value <= 12:
                raise ValueError("fan states must be integers in 0..12")
        if (self.cpu_emergency_c > ACPI_ABORT_C or self.gpu_emergency_c > 85
                or not self.cpu_target_c < self.cpu_emergency_c
                or not self.gpu_target_c < self.gpu_emergency_c):
            raise ValueError(f"targets must be below emergency thresholds "
                             f"(ACPI {ACPI_ABORT_C:g} C, GPU 85 C)")


@dataclass(frozen=True)
class Observation:
    cpu_c: float
    gpu_c: float
    gpu_util: float
    valid: bool = True
    fan_ok: bool = True
    memory_ok: bool = True
    # Emitted BEFORE simulated work begins, including a prompt during decode.
    prefill_arrival: bool = False
    workload_done: bool = False
    active_jobs: int | None = None  # independently owned request count, if known
    # Admission information, not utilization inferred after work has started.
    cpu_demand_active: bool | None = None
    cpu_work_arrival: bool = False
    model_loading: bool = False  # trusted lifecycle signal, not GPU utilization
    cpu_util: float | None = None  # aggregate 0..1, for twin power estimates only
    # Guard-style projection: max over CPU zones of 2 s trend + 2 s x rise rate
    # (the guard's own formula), and the 2 s trend of that zone. They drive the
    # adaptive setpoint and the policy's own predicted-breach check, which then
    # mirrors the guard instead of a differently filtered slope. None: unknown.
    cpu_projected_c: float | None = None
    cpu_projected_basis_c: float | None = None
    # Per-cluster zones (E0, P0, E1, P1) as (2 s trend, guard projection) of the
    # cluster's own ACPI zone (TS0E, TS0P, TS1E, TS1P). When given, each cluster
    # gets its own loop (doc/48 §0, D7); None keeps the uniform class caps.
    cpu_zones: tuple | None = None
    # Per-cluster utilisation 0..1 (E0, P0, E1, P1), or None when unknown.
    cluster_util: tuple | None = None
    # The ACPI GPU zone (TGPU) as (2 s trend, guard projection). It falls
    # under the 93 C ACPI abort and runs ~0.21 C/W above the nvidia sensor,
    # leading it in fast power steps (19 C at 66 W, matrix burn-in, 27
    # September 2026; doc/42 defect 34). When given, the GPU cap gets its own
    # guard-aware zone loop.
    gpu_zone: tuple | None = None
    # Measured GPU power (nvidia-smi) and the CPU power estimate (calorimetric
    # power_estimate), W; the predictive fan's feed-forward. None: unknown.
    gpu_w: float | None = None
    cpu_w: float | None = None


@dataclass(frozen=True)
class Command:
    gpu_cap_mhz: float
    cpu_ratio: float
    fan_state: int
    mode: str
    reason: str
    # Per-cluster cap ratios (E0, P0, E1, P1), each within its own class range;
    # None: uniform class caps from cpu_ratio (fast-first mapping).
    cluster_ratios: tuple | None = None


CLUSTER_NAMES = ("E0", "P0", "E1", "P1")


def cluster_caps_mhz(ratios):
    """Per-cluster MHz from per-cluster ratios (E: 338-2808, P: 1378-3900)."""
    return tuple((1378 + (3900 - 1378) * clamp(r, 0.0, 1.0)) if name.startswith("P")
                 else (338 + (2808 - 338) * clamp(r, 0.0, 1.0))
                 for name, r in zip(CLUSTER_NAMES, ratios))


def cpu_setpoint_ceiling(s):
    """Highest effective CPU setpoint: the target, but below the guard's
    no-confirmation zone by trend_margin_c (Settings, doc/48 §0.2)."""
    return min(s.cpu_target_c, s.cpu_emergency_c - s.prediction_immediate_c - s.trend_margin_c)


def cpu_class_caps(ratio, emergency=False):
    fast_ratio = clamp(ratio, 0.0, 1.0)
    slow_ratio = fast_ratio if emergency else min(1.0, fast_ratio / 0.75)
    return (1378 + (3900 - 1378) * fast_ratio,
            338 + (2808 - 338) * slow_ratio)


def legacy_cpu_ceiling(c):
    for threshold, cap in ((97, 0), (95, 0.25), (93, 0.50), (92, 0.65), (90, 0.80), (88, 0.95)):
        if c >= threshold:
            return cap
    return 1.0


def fan_curve(c):
    return next((state for threshold, state in ((70, 12), (65, 10), (60, 8), (55, 5), (50, 3))
                 if c >= threshold), 0)


def GB10_GPU_MATMUL_W(mhz):
    """Measured GB10 GPU power under the matrix burn-in at a clock (doc/53
    sweep fit: 4.5 + 11.1 x (f / GHz) ^ 2.27 W); the load-start expectation."""
    return 4.5 + 11.1 * (max(0.0, mhz) / 1000.0) ** 2.27


def gpu_power_w(p, mhz, util):
    """Synthetic twin GPU power; the exponent and reference are unfitted."""
    return p.gpu_idle_w + (p.gpu_reference_w - p.gpu_idle_w) * util * (mhz / 1800) ** p.gpu_power_exponent


def cpu_power_w(p, ratio, load, emergency=False):
    """Synthetic twin CPU power from utilisation x class frequency (no sensor exists)."""
    fast, slow = cpu_class_caps(ratio, emergency)
    return 4.0 + (p.cpu_reference_w - 4.0) * load * (0.5 * (fast / 3900) ** 2 + 0.5 * (slow / 2808) ** 2)


def steady_resistance(p, fan):
    """Steady-state K/W from (CPU, GPU) power to (CPU, GPU) temperature.

    The shared copper sink couples both: the off-diagonal terms are the sink
    resistance, which depends on the fan response ``fan`` (0..1).
    """
    sink = 1.0 / (p.passive_conductance_w_k + p.fan_conductance_w_k * fan)
    return ((1.0 / p.cpu_conductance_w_k + sink, sink),
            (sink, 1.0 / p.gpu_conductance_w_k + sink))


def balance_reductions(need_c, need_g, resistance, cost_c, cost_g, max_c, max_g):
    """Cheapest CPU/GPU watt reductions that deliver both requested reliefs.

    Minimise ``cost_c*x_c + cost_g*x_g`` subject to
    ``R_cc*x_c + R_cg*x_g >= need_c``, ``R_gc*x_c + R_gg*x_g >= need_g`` and
    ``0 <= x <= max``. Two variables: the optimum is a vertex of the feasible
    polygon, so every pairwise line intersection is checked. When even both
    maxima cannot deliver the reliefs, both maxima are returned.
    """
    (r_cc, r_cg), (r_gc, r_gg) = resistance
    need_c, need_g = max(0.0, need_c), max(0.0, need_g)
    max_c, max_g = max(0.0, max_c), max(0.0, max_g)
    lines = ((r_cc, r_cg, need_c), (r_gc, r_gg, need_g),
             (1.0, 0.0, 0.0), (1.0, 0.0, max_c), (0.0, 1.0, 0.0), (0.0, 1.0, max_g))
    eps = 1e-9

    def feasible(x_c, x_g):
        return (-eps <= x_c <= max_c + eps and -eps <= x_g <= max_g + eps
                and r_cc * x_c + r_cg * x_g >= need_c - 1e-7
                and r_gc * x_c + r_gg * x_g >= need_g - 1e-7)

    best = None
    for i, (a1, b1, c1) in enumerate(lines):
        for a2, b2, c2 in lines[i + 1:]:
            det = a1 * b2 - a2 * b1
            if abs(det) < 1e-12:
                continue
            x_c = (c1 * b2 - c2 * b1) / det
            x_g = (a1 * c2 - a2 * c1) / det
            if feasible(x_c, x_g):
                x_c, x_g = clamp(x_c, 0.0, max_c), clamp(x_g, 0.0, max_g)
                # Tiny total-reduction tie-break keeps equal-cost choices minimal.
                score = cost_c * x_c + cost_g * x_g + 1e-9 * (x_c + x_g)
                if best is None or score < best[0]:
                    best = (score, x_c, x_g)
    return (max_c, max_g) if best is None else (best[1], best[2])


class Supervisor:
    def __init__(self, settings=None, twin=None):
        self.s = settings or Settings()
        # Digital twin used for power balance; synthetic until fitted.
        self.twin = twin or PlantParameters()
        self.cpu = PID(self.s.cpu_gains)
        self.gpu = PID(self.s.gpu_gains)
        self.cap = self.s.baseline_mhz
        self.cpu_cap = 1.0
        self.cpu_signal_seen = False
        self.previous_cpu_demand = False
        self.fan_state = 12
        self.busy_s = self.low_s = 0.0
        self.fan_pressure_s = self.fan_calm_s = 0.0
        self.emergency_latched = False
        self.cool_s = 0.0
        self.projected_s = 0.0
        # Served-equivalent minus balanced actuator output, for external CPU
        # tracking (ShadowPolicy tracks after its own class quantization).
        self.cpu_track_offset = 0.0
        self.balance_info = {}
        # Per-cluster loops (E0, P0, E1, P1) and the non-thermal CPU limit
        # (entry clamp, model loading, idle-down) they share.
        self.clusters = [ClusterLoop(self.s, name) for name in CLUSTER_NAMES]
        # The GPU cap's own loop on the ACPI GPU zone (defect 34).
        self.gpu_zone_loop = ClusterLoop(self.s, "GPU", gains="gpu_zone_gains",
                                         margin="gpu_zone_margin_c")
        self.cpu_limit = 1.0
        self.workloads = {}
        # Effective CPU setpoint (guard-margin back-off) and fan idle timer.
        self.cpu_setpoint = cpu_setpoint_ceiling(self.s)
        self.near_miss_s = 0.0
        self.idle_s = 0.0
        # Predictive fan state: expected power (peak-held), load-start anticipation.
        self.fan_power_w = None
        self.fan_anticipate_left_s = 0.0
        self.fan_gpu_was_active = False
        self.fan_down_s = 0.0
        self.fan_info = {}

    def _fault(self, reason):
        s = self.s
        self.cap, self.cpu_cap, self.fan_state = s.minimum_mhz, 0.0, 12
        self.busy_s = self.low_s = self.cool_s = 0.0
        self.fan_pressure_s = self.fan_calm_s = 0.0
        self.emergency_latched = True
        self.cpu_track_offset = 0.0
        # Reset estimator state; do not differentiate across an observation gap.
        self.cpu, self.gpu = PID(s.cpu_gains), PID(s.gpu_gains)
        self.clusters = [ClusterLoop(s, name) for name in CLUSTER_NAMES]
        self.gpu_zone_loop = ClusterLoop(s, "GPU", gains="gpu_zone_gains", margin="gpu_zone_margin_c")
        self.cpu_limit = 0.0
        return Command(self.cap, 0.0, 12, "FAULT", reason)

    def _balance(self, o, u_c, u_g, reserve):
        """Split the two PIDs' requested reliefs by thermal effect and cost.

        Each PID output is read as a requested temperature relief at its own
        sensor, scaled by the twin at nominal load. Returns balanced actuator
        outputs and the served-equivalent PID outputs for anti-windup.
        """
        s, p = self.s, self.twin
        if not s.balance:
            u_c = max(u_c, reserve)
            self.balance_info = {"enabled": False}
            return u_c, u_g, u_c, u_g
        cpu_load = 0.5 if o.cpu_util is None else o.cpu_util
        span_c = cpu_power_w(p, 1.0, cpu_load) - cpu_power_w(p, 0.0, cpu_load)
        span_g = gpu_power_w(p, s.maximum_mhz, o.gpu_util) - gpu_power_w(p, s.minimum_mhz, o.gpu_util)
        nominal_c = cpu_power_w(p, 1.0, 0.5) - cpu_power_w(p, 0.0, 0.5)
        nominal_g = gpu_power_w(p, s.maximum_mhz, 1.0) - gpu_power_w(p, s.minimum_mhz, 1.0)
        resistance = steady_resistance(p, max(0.2, self.fan_state / 12))
        (r_cc, r_cg), (r_gc, r_gg) = resistance
        # Full authority: output 0 always means "remove all available heat".
        scale_c, scale_g = r_cc * max(span_c, nominal_c), r_gg * nominal_g
        need_c, need_g = (1.0 - u_c) * scale_c, (1.0 - u_g) * scale_g
        cost_c, cost_g = self._workload_costs(o)
        x_c, x_g = balance_reductions(need_c, need_g, resistance, cost_c,
                                      cost_g, (1.0 - reserve) * span_c, span_g)
        # Bound GPU cuts made on behalf of the CPU: on GB10 a GPU watt barely
        # cools a P-cluster hot spot, so an (often spike-inflated) CPU demand
        # otherwise cut the GPU to ~550 MHz (live). The GPU's own need is unbounded.
        x_g_own = need_g / r_gg if r_gg > 0 else 0.0
        # Below its target the CPU is relieved by its own caps only: fast-core
        # cuts cost ~3 % LLM throughput (doc/45) while P-cluster spikes near
        # 77-79 C still spilled 25 % of the GPU span (live, 27 September 2026).
        spill = s.cpu_spill_fraction if o.cpu_c >= s.cpu_target_c else 0.0
        x_g = min(x_g, max(x_g_own, spill * span_g))
        # Map watts back to a cap ratio against at least the nominal span. At low
        # utilisation the removable power is tiny; mapping against it turned a
        # small relief into a full cut (GPU to 500 MHz at 5 % load, live
        # 26 September 2026). At or above nominal load this is unchanged.
        cpu_out = (clamp(1.0 - x_c / max(span_c, nominal_c), 0.0, 1.0)
                   if span_c > 1e-9 else max(u_c, reserve))
        gpu_out = (clamp(1.0 - x_g / max(span_g, nominal_g), 0.0, 1.0)
                   if span_g > 1e-9 else u_g)
        relief_c = r_cc * x_c + r_cg * x_g
        relief_g = r_gc * x_c + r_gg * x_g
        cpu_eq = u_c if relief_c >= need_c - 1e-7 else clamp(1.0 - relief_c / scale_c, 0.0, 1.0)
        gpu_eq = u_g if relief_g >= need_g - 1e-7 else clamp(1.0 - relief_g / scale_g, 0.0, 1.0)
        self.balance_info = {"enabled": True, "need_cpu_c": need_c, "need_gpu_c": need_g,
                             "cut_cpu_w": x_c, "cut_gpu_w": x_g, "reserve": reserve,
                             "cost_cpu": cost_c, "cost_gpu": cost_g, **self.workloads}
        return cpu_out, gpu_out, cpu_eq, gpu_eq

    def _adapt_cpu_setpoint(self, o, dt):
        """Back the CPU setpoint off while the guard's projection nears the abort.

        The guard aborts without confirmation once a zone's trend is within
        3 C of 93 C and trend + 2 s x rise reaches 93 C (safety.py). Holding a
        90 C trend with P-cluster ripple trips it within seconds (doc/42,
        defect 28), so the effective setpoint settles where near-misses
        (projection >= abort - guard_margin_c) are rare: back-off/recovery
        rates give a ~0.5 % near-miss fraction. Bounded, and never above the
        operator target; a calmer load recovers toward the target by itself.
        """
        s = self.s
        ceiling_c = cpu_setpoint_ceiling(s)
        floor_c = ceiling_c - s.setpoint_backoff_max_c
        if (o.cpu_projected_c is not None
                and o.cpu_projected_c >= s.cpu_emergency_c - s.guard_margin_c):
            self.cpu_setpoint -= s.setpoint_backoff_c_s * dt
            self.near_miss_s += dt
        else:
            self.cpu_setpoint += s.setpoint_recovery_c_s * dt
        self.cpu_setpoint = clamp(self.cpu_setpoint, floor_c, ceiling_c)

    def _recovery_rate(self, o):
        """Upward CPU cap rate, tapered by the projection's headroom below the setpoint."""
        s = self.s
        projected = o.cpu_projected_c if o.cpu_projected_c is not None else o.cpu_c
        scale = clamp((self.cpu_setpoint - projected) / s.recovery_taper_band_c,
                      s.recovery_taper_min, 1.0)
        return s.cpu_recovery_s * scale

    def _load_fan(self, dt, loaded, near_abort):
        """Load policy: fan_load_state under any load, fan_min_state after a long idle."""
        s = self.s
        self.idle_s = 0.0 if loaded else self.idle_s + dt
        self.fan_pressure_s = self.fan_calm_s = 0.0
        if near_abort:
            self.fan_state = 12
        elif loaded or self.idle_s < s.fan_idle_delay_s:
            self.fan_state = max(s.fan_min_state, s.fan_load_state)
        else:
            self.fan_state = s.fan_min_state

    def _predictive_fan(self, o, dt, gpu_active, near_abort, headroom_terms):
        """Fan level from the expected power (feed-forward through the fitted
        cooler) and the loops' guard projections (feedback); up at once, down
        one level per fan_down_dwell_s. Pure; see Settings (predictive fan)."""
        s = self.s
        # Expected power: measured, peak-held; anticipation at a GPU load start.
        measured = sum(v for v in (o.gpu_w, o.cpu_w) if v is not None and isfinite(v) and v >= 0)
        if o.gpu_w is None and o.cpu_w is None:
            measured = gpu_power_w(self.twin, self.cap, o.gpu_util)   # twin fallback
        # Anticipate only on evidence of real GPU work (prefill, model load,
        # owned jobs, or busy at the ramp threshold): a desktop or dashboard
        # blip of 20 % utilisation is no load (live, 27 September 2026).
        start = gpu_active and not self.fan_gpu_was_active
        self.fan_gpu_was_active = gpu_active
        if start or o.model_loading:
            self.fan_anticipate_left_s = s.fan_anticipate_s
        self.fan_anticipate_left_s = max(0.0, self.fan_anticipate_left_s - dt)
        # The peak-hold smooths the measured power only; the anticipation is
        # added on top and ends with the evidence that raised it.
        held = self.fan_power_w if self.fan_power_w is not None else measured
        decay = exp(-dt / s.fan_power_decay_s)
        self.fan_power_w = max(measured, held * decay + measured * (1 - decay))
        expected = self.fan_power_w
        anticipating = self.fan_anticipate_left_s > 0 and gpu_active
        if anticipating:
            expected = max(expected, GB10_GPU_MATMUL_W(self.cap) + (o.cpu_w or 0.0))
        total = expected + s.fan_background_w
        # Feed-forward: lowest level whose steady plate estimate stays at target.
        ceiling = max(s.fan_min_state, min(12, s.fan_load_state))
        feed = ceiling
        for level in range(s.fan_min_state, ceiling + 1):
            g_air = s.fan_air_g0_w_k + s.fan_air_g1_w_k * max(0.2, level / 12)
            plate = s.fan_room_c + total * (1 / s.fan_neck_w_k + 1 / g_air)
            if plate <= s.fan_plate_target_c:
                feed = level
                break
        # Feedback: the tightest loop's projection against its setpoint.
        pressure = 0.0
        for projection, setpoint in headroom_terms:
            if projection is None or setpoint is None or not isfinite(projection):
                continue
            span = max(0.5, s.fan_fb_band_c - s.fan_fb_full_c)
            pressure = max(pressure, clamp((projection - (setpoint - s.fan_fb_band_c)) / span, 0.0, 1.0))
        back = int(-(-12 * pressure // 1))            # ceil
        target = max(s.fan_min_state, min(ceiling, max(feed, back)))
        if near_abort:
            target = 12
        if target >= self.fan_state:
            self.fan_state, self.fan_down_s = target, 0.0
        else:
            self.fan_down_s += dt
            if self.fan_down_s >= s.fan_release_step_s:
                self.fan_state, self.fan_down_s = self.fan_state - 1, 0.0
        self.fan_state = max(s.fan_min_state, min(12, self.fan_state))
        self.fan_info = {"expected_w": round(expected, 1), "feed": feed, "feedback": back,
                         "target": target, "pressure": round(pressure, 2),
                         "anticipating": anticipating}

    def _workload_costs(self, o):
        """Per-watt cut costs from the detected workloads and their priorities."""
        s = self.s
        # The GPU workload: LLM work (prefill, loading, queued jobs) or any busy
        # GPU (burn-in, other GPU jobs) when the queue is unknown.
        gpu_load = o.model_loading or o.gpu_util >= s.idle_util_threshold   # utilisation only (28 Sep)
        cpu_jobs = o.cpu_util is not None and o.cpu_util >= s.cpu_job_util
        r_gpu = max(0.2, (self.cap - s.minimum_mhz) / (s.maximum_mhz - s.minimum_mhz))
        r_cpu = max(0.2, self.cpu_cap)
        w_gpu = s.priority_gpu / r_gpu if gpu_load else 0.0
        w_cpu = s.priority_cpu / r_cpu if cpu_jobs else 0.0
        self.workloads = {"gpu_active": gpu_load, "cpu_jobs_active": cpu_jobs,
                          "weight_gpu": w_gpu, "weight_cpu": w_cpu}
        floor = 0.01  # an idle actuator still costs a little: no cuts for nothing
        cost_g = floor + s.gpu_cost_per_w * w_gpu
        cost_c = floor + s.cpu_cost_per_w * w_gpu + s.cpu_job_cost_per_w * w_cpu
        return cost_c, cost_g

    def _stage_fan(self, o, dt, working, pressure, release, near_abort):
        """Additive floor: preferred level under load, 12 only when needed."""
        s = self.s
        max_t = max(o.cpu_c, o.gpu_c)
        base = max(s.fan_min_state, min(s.fan_preferred_state, fan_curve(max_t)))
        if working:
            base = max(base, s.fan_preferred_state)  # feedforward on load entry
        if near_abort:
            self.fan_state, self.fan_pressure_s, self.fan_calm_s = 12, 0.0, 0.0
            return
        self.fan_pressure_s = self.fan_pressure_s + dt if pressure else 0.0
        self.fan_calm_s = self.fan_calm_s + dt if release else 0.0
        if self.fan_state < base:
            self.fan_state = base
        elif self.fan_pressure_s >= s.fan_derate_dwell_s and self.fan_state < 12:
            self.fan_state += 1  # Spend fan before clocks; one state per fan_step_s.
            self.fan_pressure_s = max(0.0, s.fan_derate_dwell_s - s.fan_step_s)
        elif self.fan_state > base and self.fan_calm_s >= s.fan_down_dwell_s:
            self.fan_state -= 1  # Slow, hysteretic decrease limits thermal cycling.
            self.fan_calm_s = 0.0
        self.fan_state = max(self.fan_state, s.fan_min_state)

    def step(self, observation, dt, *, track_cpu=True):
        s, o = self.s, observation
        valid = (o.valid and o.fan_ok and o.memory_ok and isfinite(dt) and 0 < dt <= 1
                 and all(isfinite(v) for v in (o.cpu_c, o.gpu_c, o.gpu_util))
                 and -10 < o.cpu_c < 150 and -10 < o.gpu_c < 150 and 0 <= o.gpu_util <= 1
                 and (o.cpu_util is None or
                      (type(o.cpu_util) in (int, float) and isfinite(o.cpu_util)
                       and 0 <= o.cpu_util <= 1))
                 and (o.cpu_projected_c is None or
                      (type(o.cpu_projected_c) in (int, float) and isfinite(o.cpu_projected_c)
                       and -10 < o.cpu_projected_c < 200))
                 and (o.cpu_zones is None or
                      (type(o.cpu_zones) is tuple and len(o.cpu_zones) == 4
                       and all(type(z) is tuple and len(z) == 2
                               and all(type(v) in (int, float) and isfinite(v)
                                       and -10 < v < 200 for v in z)
                               and z[0] <= z[1] for z in o.cpu_zones)))
                 and (o.gpu_zone is None or
                      (type(o.gpu_zone) is tuple and len(o.gpu_zone) == 2
                       and all(type(v) in (int, float) and isfinite(v) and -10 < v < 200
                               for v in o.gpu_zone)
                       and o.gpu_zone[0] <= o.gpu_zone[1]))
                 and (o.cluster_util is None or
                      (type(o.cluster_util) is tuple and len(o.cluster_util) == 4
                       and all(type(u) in (int, float) and isfinite(u) and 0 <= u <= 1
                               for u in o.cluster_util)))
                 and (o.cpu_projected_basis_c is None or
                      (o.cpu_projected_c is not None
                       and type(o.cpu_projected_basis_c) in (int, float)
                       and isfinite(o.cpu_projected_basis_c)
                       and -10 < o.cpu_projected_basis_c <= o.cpu_projected_c))
                 and (o.active_jobs is None or
                      (type(o.active_jobs) is int and 0 <= o.active_jobs <= 20))
                 and (o.cpu_demand_active is None or type(o.cpu_demand_active) is bool)
                 and type(o.cpu_work_arrival) is bool
                 and type(o.model_loading) is bool
                 and (not o.model_loading or o.cpu_demand_active is True)
                 and (not o.cpu_work_arrival or o.cpu_demand_active is True)
                 and (not self.cpu_signal_seen or o.cpu_demand_active is not None))
        if (not valid or o.cpu_c >= s.cpu_emergency_c or o.gpu_c >= s.gpu_emergency_c
                or (o.gpu_zone is not None and o.gpu_zone[0] >= s.cpu_emergency_c)):
            return self._fault("invalid input / emergency / fan or memory fault")
        if self.emergency_latched:
            cool = (o.cpu_c <= s.cpu_emergency_c - s.emergency_hysteresis_c
                    and o.gpu_c <= s.gpu_emergency_c - s.emergency_hysteresis_c)
            self.cool_s = self.cool_s + dt if cool else 0.0
            if self.cool_s < s.emergency_recovery_s:
                return Command(s.minimum_mhz, 0.0, 12, "FAULT", "emergency latched: cooling dwell")
            # Recover from the entry ceilings, never from the previous ramp.
            self.emergency_latched = False
            self.cap, self.cpu_cap = s.baseline_mhz, min(s.cpu_entry_ratio, 1.0)
            self.cpu_limit = min(s.cpu_entry_ratio, 1.0)

        self._adapt_cpu_setpoint(o, dt)
        cpu_pid = self.cpu.propose(o.cpu_c, self.cpu_setpoint, dt)
        gpu_pid = self.gpu.propose(o.gpu_c, s.gpu_target_c, dt)
        if o.cpu_c < self.cpu_setpoint - s.pid_band_c:
            cpu_pid = 1.0
        if o.gpu_c < s.gpu_target_c - s.pid_band_c:
            gpu_pid = 1.0
        projected_cpu = o.cpu_c + (s.prediction_s * max(0.0, self.cpu.slope)
                                   if o.cpu_c >= s.cpu_emergency_c - s.prediction_band_c else 0.0)
        projected_gpu = o.gpu_c + (s.prediction_s * max(0.0, self.gpu.slope)
                                   if o.gpu_c >= s.gpu_emergency_c - s.prediction_band_c else 0.0)
        # The predicted-breach abort mirrors the guard (safety.py) on the guard's
        # own quantities when the policy supplies them: a differently filtered
        # slope faulted before the guard would have (cluster twin, 27 September).
        if o.cpu_projected_basis_c is not None:
            breach_cpu = (o.cpu_projected_c >= s.cpu_emergency_c
                          and o.cpu_projected_basis_c >= s.cpu_emergency_c - s.prediction_band_c)
            near_cpu = o.cpu_projected_basis_c >= s.cpu_emergency_c - s.prediction_immediate_c
        else:
            breach_cpu = projected_cpu >= s.cpu_emergency_c
            near_cpu = o.cpu_c >= s.cpu_emergency_c - s.prediction_immediate_c
        breach_zone = near_zone = False
        if o.gpu_zone is not None:  # the ACPI GPU zone, on the guard's own quantities
            breach_zone = (o.gpu_zone[1] >= s.cpu_emergency_c
                           and o.gpu_zone[0] >= s.cpu_emergency_c - s.prediction_band_c)
            near_zone = o.gpu_zone[0] >= s.cpu_emergency_c - s.prediction_immediate_c
        if breach_cpu or breach_zone or projected_gpu >= s.gpu_emergency_c:
            self.projected_s += dt
            near = ((breach_cpu and near_cpu) or (breach_zone and near_zone)
                    or o.gpu_c >= s.gpu_emergency_c - s.prediction_immediate_c)
            if self.projected_s >= s.prediction_confirm_s or near:
                return self._fault("predicted abort-limit breach (ACPI 93 C / GPU 85 C)")
        else:
            self.projected_s = 0.0

        llm_active = o.model_loading or o.gpu_util >= s.idle_util_threshold   # utilisation only (28 Sep)
        reserve = (s.cpu_reservation_ratio
                   if llm_active and o.cpu_c < s.cpu_target_c + s.reservation_yield_c else 0.0)
        clustered = o.cpu_zones is not None
        # With cluster loops the CPU's own heat is relieved by its own clusters
        # only; the balance allocates just the GPU's need (to the GPU or, by
        # cost, to CPU caps through the shared copper).
        cpu_bal, gpu_bal, cpu_eq, gpu_eq = self._balance(o, 1.0 if clustered else cpu_pid,
                                                         gpu_pid, reserve)

        previous_cpu_limit = self.cpu_limit
        self.cpu_limit = min(1.0, previous_cpu_limit + s.cpu_recovery_s * dt)
        if o.cpu_demand_active is not None:
            if o.cpu_work_arrival or (o.cpu_demand_active and not self.previous_cpu_demand):
                self.cpu_limit = min(previous_cpu_limit, s.cpu_entry_ratio)
            elif not o.cpu_demand_active:
                self.cpu_limit = min(previous_cpu_limit,
                                     max(s.cpu_entry_ratio, previous_cpu_limit - s.cpu_normal_down_s * dt))
        if o.model_loading:
            self.cpu_limit = min(previous_cpu_limit, s.cpu_entry_ratio)

        previous_cpu_cap = self.cpu_cap
        self.cpu_cap = min(cpu_bal, previous_cpu_cap + self._recovery_rate(o) * dt)
        if o.cpu_demand_active is not None:
            if o.cpu_work_arrival or (o.cpu_demand_active and not self.previous_cpu_demand):
                # Announced before work starts; never raise a derated cap.
                self.cpu_cap = min(self.cpu_cap, previous_cpu_cap, s.cpu_entry_ratio)
            elif not o.cpu_demand_active:
                self.cpu_cap = min(self.cpu_cap, previous_cpu_cap,
                                   max(s.cpu_entry_ratio, previous_cpu_cap - s.cpu_normal_down_s * dt))
            self.cpu_signal_seen = True
            self.previous_cpu_demand = o.cpu_demand_active

        if o.model_loading:
            # Model initialization includes CPU performance-core work. Keep the
            # entry envelope for the entire loading phase, not just one tick.
            self.cpu_cap = min(self.cpu_cap, previous_cpu_cap, s.cpu_entry_ratio)

        # Last-resort guard band below each fixed abort, independent of the PIDs.
        # Last-resort guard bands, each on its own actuator. On GB10 the CPU
        # clusters and the GPU die couple only weakly (through the shared copper):
        # capping the GPU for a CPU P-cluster spike cost throughput without
        # cooling the cluster (live, 26 September 2026). Cross-coupling is left
        # to the power balance, which uses the measured twin.
        # A projection spike alone (trend still below the band) cuts at most to
        # the entry ratio; a real runaway lifts the trend too and gets the full
        # cut. Live 15:17: one 94.9 C projection at a 84 C trend cut every
        # cluster to minimum for ~30 s. The independent guard is unchanged.
        guard_floor = (0.0 if o.cpu_c >= s.cpu_emergency_c - s.guard_band_c - 1.0
                       else min(s.cpu_entry_ratio, 1.0))
        cpu_guard = clamp((s.cpu_emergency_c - projected_cpu) / s.guard_band_c, guard_floor, 1.0)
        gpu_guard = clamp((s.gpu_emergency_c - projected_gpu) / s.guard_band_c, 0.0, 1.0)
        self.cpu_cap = min(self.cpu_cap, cpu_guard)
        cluster_ratios = None
        if clustered:
            bound = min(self.cpu_limit, cpu_bal, cpu_guard)
            utils = o.cluster_util or (None,) * 4
            maxima = s.cluster_max_ratio
            ratios = [loop.step(zone, min(bound, maximum), dt, util)
                      for loop, zone, util, maximum
                      in zip(self.clusters, o.cpu_zones, utils, maxima)]
            # Fast-first per side (retired guard, doc/48 §0): an E cluster drops
            # below full only once its P neighbour is under 75 % (E0-P0, E1-P1)
            # of that P cluster's operator maximum; an operator cap is not a
            # thermal derate and never drags its E neighbour down.
            for e, pc in ((0, 1), (2, 3)):
                relative = ratios[pc] / maxima[pc] if maxima[pc] > 0 else 1.0
                ratios[e] = min(ratios[e], min(1.0, relative / 0.75))
                self.clusters[e].cap = ratios[e]
            cluster_ratios = tuple(ratios)
            self.cpu_cap = max(ratios[1], ratios[3])  # aggregate view: the faster P cluster
        # The GPU cap's zone loop on TGPU (defect 34): the CPU clusters' guard-
        # aware loop, with a last-resort band below the 93 C ACPI abort that
        # cuts a projection spike at most to the entry ceiling.
        zone_cap, ramp_scale = 1.0, 1.0
        if o.gpu_zone is not None:
            span = s.maximum_mhz - s.minimum_mhz
            entry_ratio = clamp((s.baseline_mhz - s.minimum_mhz) / span, 0.0, 1.0)
            # A spike alone steps down (spike_step per tick, not below the entry
            # ceiling); a real runaway (trend in the band) gets the full cut.
            zone_floor = (0.0 if o.gpu_zone[0] >= s.cpu_emergency_c - s.guard_band_c - 1.0
                          else max(entry_ratio, self.gpu_zone_loop.cap - s.gpu_zone_spike_step))
            zone_guard = clamp((s.cpu_emergency_c - o.gpu_zone[1]) / s.guard_band_c,
                               zone_floor, 1.0)
            zone_cap = self.gpu_zone_loop.step(o.gpu_zone, zone_guard, dt)
            ramp_scale = clamp((self.gpu_zone_loop.setpoint - o.gpu_zone[1])
                               / s.gpu_zone_taper_band_c, s.recovery_taper_min, 1.0)
        thermal_cap = (s.minimum_mhz + (s.maximum_mhz - s.minimum_mhz)
                       * min(gpu_bal, gpu_guard, zone_cap))

        self.low_s = self.low_s + dt if o.gpu_util < s.idle_util_threshold else 0.0
        prefill_rearm = o.prefill_arrival and s.entry_fallback and s.prefill_rearm
        # A single request can complete while other queued/active LLM work
        # still drives the GPU. Its completion event alone is not proof of
        # whole-device idleness and must not collapse a productive cap.
        completion_idle = (o.workload_done and o.gpu_util < s.idle_util_threshold
                           and o.active_jobs in (None, 0))
        idle = o.gpu_util == 0 or self.low_s >= s.low_load_timeout_s or completion_idle
        rearm = prefill_rearm or idle or o.model_loading
        if o.model_loading:
            self.busy_s = 0.0
            proposed = min(self.cap, s.baseline_mhz)
            mode, reason = "STARTUP", "model loading: CPU/GPU entry ceilings held"
        elif prefill_rearm:
            self.busy_s = 0.0
            # Admission is protected before work; a reset never raises a derated cap.
            proposed = min(self.cap, s.baseline_mhz)
            mode, reason = "REARM", "new prefill: baseline ceiling"
        elif idle and s.entry_fallback:
            self.busy_s = 0.0
            proposed = min(self.cap, max(s.baseline_mhz, self.cap - s.normal_down_mhz_s * dt))
            mode, reason = "COOLDOWN", "idle: gradual clock-cap reduction"
        elif idle:
            self.busy_s = 0.0
            proposed = self.cap
            mode, reason = "HOLD", "idle: qualified entry step, no fallback"
        else:
            self.busy_s = self.busy_s + dt if o.gpu_util >= s.busy_threshold else 0.0
            permit_up = self.cap < s.baseline_mhz or self.busy_s >= s.busy_dwell_s
            limit = s.maximum_mhz if self.busy_s >= s.busy_dwell_s else s.baseline_mhz
            proposed = (min(limit, self.cap + s.ramp_mhz_s * ramp_scale * dt) if permit_up
                        else self.cap)
            mode, reason = ("RAMP", "busy dwell satisfied") if limit > s.baseline_mhz else ("HOLD", "waiting for sustained load")
        self.cap = min(proposed, thermal_cap, s.maximum_mhz)
        if self.cap < proposed:
            mode, reason = "DERATED", "thermal PID / power balance / guard band"
        elif not rearm and self.cap >= s.maximum_mhz:
            mode, reason = "RUN", "simulation maximum reached"

        working = llm_active or o.gpu_util >= s.busy_threshold or o.cpu_work_arrival
        above_target = (o.cpu_c > s.cpu_target_c + s.fan_target_band_c
                        or o.gpu_c > s.gpu_target_c + s.fan_target_band_c)
        headroom = min(gpu_bal, cpu_bal) if working else 1.0
        near_abort = (projected_cpu >= s.cpu_emergency_c - s.guard_band_c
                      or projected_gpu >= s.gpu_emergency_c - s.guard_band_c
                      or (o.gpu_zone is not None
                          and o.gpu_zone[1] >= s.cpu_emergency_c - s.guard_band_c))
        if s.fan_policy == "predictive":
            terms = [(projected_cpu, self.cpu_setpoint), (projected_gpu, s.gpu_target_c)]
            if o.gpu_zone is not None:
                terms.append((o.gpu_zone[1], self.gpu_zone_loop.setpoint))
            if o.cpu_zones is not None:
                terms += [(zone[1], loop.setpoint) for zone, loop in zip(o.cpu_zones, self.clusters)]
            if o.cpu_projected_c is not None:
                terms.append((o.cpu_projected_c, self.cpu_setpoint))
            gpu_work = o.model_loading or o.gpu_util >= s.busy_threshold   # utilisation only (28 Sep)
            self._predictive_fan(o, dt, gpu_work, near_abort, terms)
        elif s.fan_policy == "load":
            loaded = working or o.model_loading or o.cpu_demand_active is True
            self._load_fan(dt, loaded, near_abort)
        else:
            self._stage_fan(o, dt, working, above_target or headroom < s.fan_boost_headroom,
                            not above_target and headroom >= s.fan_release_headroom, near_abort)

        # Anti-windup on what each loop was actually served, plus any
        # downstream limit (ramps, entry, guard band) on its own actuator.
        self.cpu_track_offset = cpu_eq - cpu_bal
        if track_cpu:
            self.cpu.track(clamp(self.cpu_cap + self.cpu_track_offset, 0.0, 1.0), dt)
        applied_gpu = (self.cap - s.minimum_mhz) / (s.maximum_mhz - s.minimum_mhz)
        self.gpu.track(clamp(applied_gpu + gpu_eq - gpu_bal, 0.0, 1.0), dt)
        return Command(self.cap, self.cpu_cap, self.fan_state, mode, reason, cluster_ratios)


class ClusterLoop:
    """One CPU cluster's thermal loop on its own zone (doc/48 §0, D7): the
    conditional PID, the guard-aware setpoint and the recovery taper of the
    aggregate loop, applied per cluster. ``bound`` carries the shared
    non-thermal limit, the derate band and any balance cut."""

    def __init__(self, settings, name="P0", gains="cpu_gains", margin=None):
        self.s = settings
        self.name = name
        self.gains_attr = gains  # the Settings field holding this loop's gains
        self.margin_attr = margin  # Settings field: extra distance below the ceiling
        self.pid = PID(getattr(settings, gains))
        self.setpoint = self.ceiling(settings)
        self.cap = 1.0
        self.learned = settings.learned_cap_initial_p if name.startswith("P") else 1.0

    def ceiling(self, settings):
        extra = getattr(settings, self.margin_attr) if self.margin_attr else 0.0
        return cpu_setpoint_ceiling(settings) - extra

    def step(self, zone, bound, dt, util=None):
        s = self.s
        trend, projection = zone
        busy = util is None or util >= s.cluster_busy_util
        full = util is not None and util >= s.cluster_full_util
        if util is not None and not full and self.name.startswith("P"):
            bound = min(bound, max(s.cpu_entry_ratio, self.learned - s.partial_cap_margin))
        ceiling = self.ceiling(s)
        if projection >= s.cpu_emergency_c - s.guard_margin_c:
            self.setpoint -= s.setpoint_backoff_c_s * dt
        else:
            self.setpoint += s.setpoint_recovery_c_s * dt
        self.setpoint = clamp(self.setpoint, ceiling - s.setpoint_backoff_max_c, ceiling)
        self.pid.gains = getattr(s, self.gains_attr)
        wanted = self.pid.propose(trend, self.setpoint, dt)
        if busy:
            if trend < self.setpoint - s.pid_band_c:
                wanted = 1.0  # far below target: no relief (defect-17 band)
        elif projection < self.setpoint - s.relief_projection_margin_c:
            wanted = 1.0  # a burst on a lightly loaded cluster, far from the limit
        rate = s.cpu_recovery_s * clamp((self.setpoint - projection) / s.recovery_taper_band_c,
                                        s.recovery_taper_min, 1.0)
        self.cap = min(wanted, bound, self.cap + rate * dt)
        self.pid.track(self.cap, dt)  # no-op for the conditional integrator
        if full and (trend >= self.setpoint - 2.0 or self.cap >= 0.999):
            # Regulating at the setpoint (or cool at full clock): that is the
            # cap this cluster can hold under full load right now.
            self.learned += (1.0 - exp(-dt / s.learn_tau_s)) * (self.cap - self.learned)
        return self.cap


@dataclass(frozen=True)
class PlantParameters:
    # Entire RC network is illustrative and must be identified from measurements.
    ambient_c: float = 25.0
    cpu_capacity_j_k: float = 8.0
    gpu_capacity_j_k: float = 20.0
    sink_capacity_j_k: float = 150.0
    cpu_conductance_w_k: float = 0.65
    gpu_conductance_w_k: float = 2.2
    passive_conductance_w_k: float = 0.8
    fan_conductance_w_k: float = 2.4
    fan_tau_s: float = 3.0
    clock_tau_s: float = 0.08
    sensor_tau_s: float = 0.0  # synthetic indicated-temperature lag; not firmware age
    gpu_power_exponent: float = 2.0
    # User's approximate 70/30 W reports anchor scale only, not a fitted curve.
    gpu_reference_w: float = 70.0
    gpu_idle_w: float = 8.0
    cpu_reference_w: float = 30.0
    other_input_w: float = 40.0  # illustrative residual, not NIC power

    def __post_init__(self):
        if not all(isfinite(v) for v in vars(self).values()):
            raise ValueError("finite plant parameters required")
        if any(v <= 0 for k, v in vars(self).items()
               if k not in ("ambient_c", "sensor_tau_s")):
            raise ValueError("positive physical constants required")
        if not 0 <= self.sensor_tau_s <= 10:
            raise ValueError("sensor lag must be 0..10 simulated seconds")
        if self.gpu_reference_w <= self.gpu_idle_w or self.cpu_reference_w < 4:
            raise ValueError("reference powers below assumed idle power")


# Measured on the Lenovo PGX / GB10 (26 September 2026, doc/44): GPU node,
# copper sink and fan from the fan-floor identification runs (two-node fit,
# leave-one-run-out medians; holdout 0.7-1.8 C), GPU power from the 4-job entry
# trials (~4.5 W idle, ~15 W at 1800 MHz, exponent ~1.3). The CPU node and CPU
# power remain synthetic: the hottest ACPI zone is not a clean CPU node on GB10.
GB10_FIT = PlantParameters(
    ambient_c=27.0, gpu_capacity_j_k=11.0, sink_capacity_j_k=165.0,
    gpu_conductance_w_k=1.9, passive_conductance_w_k=0.8, fan_conductance_w_k=3.3,
    gpu_idle_w=4.5, gpu_reference_w=16.5, gpu_power_exponent=1.3)


@dataclass(frozen=True)
class LlmThroughput:
    """Measured LLM throughput versus clocks (doc/45, 27 September 2026).

    GB10 + nvidia/Gemma-4-26B-A4B-NVFP4 in vLLM, 4 concurrent requests of
    ~20k prompt / 10k generated tokens (unique prompts, real prefill).
    Healthy per-request decode: 1/rate = gpu_s_mhz/gpu + cpu_s_mhz/cpu +
    fixed_s (20 trials, RMSE 1.8 tok/s; cpu = busiest-core clock). Prefill
    rate ~ proportional to the GPU clock. vLLM's occasional slow requests
    (~10 tok/s, clock-independent) are not modelled.
    """
    gpu_s_mhz: float = 27.0
    cpu_s_mhz: float = 3.0
    fixed_s: float = 0.0091
    prefill_tok_s_per_mhz: float = 0.92

    def decode_tok_s(self, gpu_mhz, cpu_mhz):
        return 1.0 / (self.gpu_s_mhz / gpu_mhz + self.cpu_s_mhz / cpu_mhz + self.fixed_s)

    def prefill_tok_s(self, gpu_mhz):
        return self.prefill_tok_s_per_mhz * gpu_mhz


GB10_LLM = LlmThroughput()


@dataclass(frozen=True)
class CpuClusterModel:
    """Per-cluster CPU zone model fitted 27 September 2026 (doc/44).

    T_zone = baseline + sum_cluster gain[zone][cluster] * x_cluster, where
    x lags u**gamma (u = cluster utilisation 0..1) with tau_up_p / tau_up_e
    while rising and tau_down while falling. Training: step run
    20260927T050012 (measured cluster utilisation). Holdout (93 s natural
    load): E and GPU zones <= 1.8 C RMSE, P-cluster zones ~6.6 C because
    sub-second single-thread bursts are invisible at 1 Hz. Measured clocks
    under multi-core load are firmware-clamped, so gains are per load share,
    not per watt.
    """
    clusters: tuple = ("E0", "P0", "E1", "P1")
    zones: tuple = ("TS0E", "TS0P", "TS1E", "TS1P", "TSOC", "TUNC", "TGPU")
    gain_c: tuple = ((17.4, 10.2, 3.3, 1.7), (8.6, 42.9, 8.2, -4.7), (5.1, 4.4, 15.0, 5.9),
                     (5.3, -1.2, 4.2, 38.1), (7.2, 33.5, 7.8, 24.0), (5.9, 29.1, 3.5, 2.6),
                     (1.2, 1.5, 1.1, 1.5))
    baseline_c: tuple = (36.5, 39.6, 36.3, 39.4, 39.7, 37.3, 38.9)
    tau_up_p_s: float = 6.0
    tau_up_e_s: float = 3.0
    tau_down_s: float = 1.0
    gamma: float = 0.8

    def step(self, state, utilisation, dt):
        """Advance lag states (dict cluster -> x) and return zone temperatures."""
        state = dict(state or {c: 0.0 for c in self.clusters})
        for c in self.clusters:
            u = max(0.0, min(1.0, utilisation.get(c, 0.0))) ** self.gamma
            tau = self.tau_up_p_s if c.startswith("P") else self.tau_up_e_s
            if u < state[c]:
                tau = self.tau_down_s
            state[c] += (1.0 - exp(-dt / tau)) * (u - state[c])
        temps = {z: b + sum(g * state[c] for g, c in zip(row, self.clusters))
                 for z, b, row in zip(self.zones, self.baseline_c, self.gain_c)}
        return state, temps


GB10_CPU_CLUSTERS = CpuClusterModel()
CPU_FAST_MIN_MHZ, CPU_FAST_MAX_MHZ = 1378.0, 3900.0


@dataclass
class Plant:
    p: PlantParameters = field(default_factory=PlantParameters)
    cpu_c: float = 60.0
    gpu_c: float = 45.0
    sink_c: float = 40.0
    fan: float = 1.0
    actual_gpu_mhz: float = 200.0
    gpu_w: float = 8.0
    cpu_w: float = 4.0

    def derivatives(self, cpu_w, gpu_w):
        p = self.p
        cpu_flow = p.cpu_conductance_w_k * (self.cpu_c - self.sink_c)
        gpu_flow = p.gpu_conductance_w_k * (self.gpu_c - self.sink_c)
        exhaust = (p.passive_conductance_w_k + p.fan_conductance_w_k * self.fan) * (self.sink_c - p.ambient_c)
        return ((cpu_w - cpu_flow) / p.cpu_capacity_j_k,
                (gpu_w - gpu_flow) / p.gpu_capacity_j_k,
                (cpu_flow + gpu_flow - exhaust) / p.sink_capacity_j_k)

    def advance(self, command, gpu_load, cpu_load, dt, fan_stuck=False):
        if not isfinite(dt) or not 0 < dt <= 1 or not all(0 <= v <= 1 for v in (gpu_load, cpu_load)):
            raise ValueError("bounded load and 0 < dt <= 1 required")
        p = self.p
        remaining = dt
        while remaining > 1e-12:
            h = min(0.02, remaining)
            # State zero is firmware automatic, represented by a synthetic 20% floor.
            fan_target = 0.0 if fan_stuck else max(0.2, command.fan_state / 12)
            self.fan += (fan_target - self.fan) * (1.0 - exp(-h / p.fan_tau_s))
            target_clock = command.gpu_cap_mhz if gpu_load else 200.0
            self.actual_gpu_mhz += (target_clock - self.actual_gpu_mhz) * (1.0 - exp(-h / p.clock_tau_s))
            self.cpu_w = cpu_power_w(p, command.cpu_ratio, cpu_load, self.cpu_c >= 95)
            self.gpu_w = gpu_power_w(p, self.actual_gpu_mhz, gpu_load)
            dc, dg, ds = self.derivatives(self.cpu_w, self.gpu_w)
            self.cpu_c += h * dc
            self.gpu_c += h * dg
            self.sink_c += h * ds
            remaining -= h


@dataclass(frozen=True)
class CoreThermalParameters:
    """Unfitted per-core RC extension; slots are NOT Linux CPU/policy IDs.

    Slots 0..9 describe fast cores; 10..19 describe slow cores. Totals retain
    the old model's CPU heat capacity/conductance, but the split is synthetic.
    Watts must be supplied explicitly; process count is not a power reading.
    """
    capacities_j_k: tuple = (0.5,) * 10 + (0.3,) * 10
    conductances_w_k: tuple = (0.04,) * 10 + (0.025,) * 10
    gpu_capacity_j_k: float = 20.0
    gpu_conductance_w_k: float = 2.2
    sink_capacity_j_k: float = 150.0
    ambient_conductance_w_k: float = 0.8  # passive term
    fan_conductances_w_k: tuple = (1.2, 1.2)  # synthetic combined maximum: 3.2 W/K
    fan_time_constants_s: tuple = (3.0, 3.0)
    ambient_c: float = 25.0

    def __post_init__(self):
        for values in (self.capacities_j_k, self.conductances_w_k):
            if (type(values) is not tuple or len(values) != 20
                    or any(type(v) not in (int, float) or not isfinite(v) or v <= 0 for v in values)):
                raise ValueError("twenty positive per-core thermal constants required")
        for name in ("gpu_capacity_j_k", "gpu_conductance_w_k", "sink_capacity_j_k",
                     "ambient_conductance_w_k"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not isfinite(value) or value <= 0:
                raise ValueError("positive thermal constant required")
        if not isfinite(self.ambient_c):
            raise ValueError("finite ambient required")
        for values in (self.fan_conductances_w_k, self.fan_time_constants_s):
            if (type(values) is not tuple or len(values) != 2
                    or any(type(v) not in (int, float) or not isfinite(v) or v <= 0 for v in values)):
                raise ValueError("two positive fan constants required")


@dataclass
class CoreThermalPlant:
    """Pure 20-core/GPU/shared-copper state; no device or sensor mapping claims.

    This extension is separate from the aggregate TUI plant until class-specific
    workload/power inputs exist. It cannot identify hardware limits on its own.
    """
    p: CoreThermalParameters = field(default_factory=CoreThermalParameters)
    cores_c: tuple = (25.0,) * 20
    gpu_c: float = 25.0
    sink_c: float = 25.0
    fans: tuple = (1.0, 1.0)  # normalized cooling response, not measured RPM

    @property
    def cooling_conductance_w_k(self):
        if (type(self.fans) is not tuple or len(self.fans) != 2
                or any(type(a) not in (int, float) or not isfinite(a) or not 0 <= a <= 1
                       for a in self.fans)):
            raise ValueError("two bounded fan responses required")
        return self.p.ambient_conductance_w_k + sum(
            g * a for g, a in zip(self.p.fan_conductances_w_k, self.fans))

    def derivatives(self, core_w, gpu_w):
        if (type(core_w) is not tuple or len(core_w) != 20
                or any(type(w) not in (int, float) or not isfinite(w) or w < 0
                       for w in (*core_w, gpu_w))):
            raise ValueError("twenty finite nonnegative core powers and GPU power required")
        if (type(self.cores_c) is not tuple or len(self.cores_c) != 20
                or not all(isfinite(t) for t in (*self.cores_c, self.gpu_c, self.sink_c))):
            raise ValueError("finite thermal state required")
        p = self.p
        flows = tuple(g * (t - self.sink_c) for g, t in zip(p.conductances_w_k, self.cores_c))
        gpu_flow = p.gpu_conductance_w_k * (self.gpu_c - self.sink_c)
        exhaust = self.cooling_conductance_w_k * (self.sink_c - p.ambient_c)
        return (tuple((w - q) / c for w, q, c in zip(core_w, flows, p.capacities_j_k)),
                (gpu_w - gpu_flow) / p.gpu_capacity_j_k,
                (sum(flows) + gpu_flow - exhaust) / p.sink_capacity_j_k)

    def advance(self, core_w, gpu_w, dt, *, fan_targets=None):
        if type(dt) not in (int, float) or not isfinite(dt) or not 0 < dt <= 1:
            raise ValueError("bounded thermal integration interval required")
        self.derivatives(core_w, gpu_w)  # Validate before any state change.
        targets = self.fans if fan_targets is None else fan_targets
        if (type(targets) is not tuple or len(targets) != 2
                or any(type(a) not in (int, float) or not isfinite(a) or not 0 <= a <= 1
                       for a in targets)):
            raise ValueError("two bounded synthetic fan targets required")
        p = self.p
        stable_step = 0.2 * min(*(c / g for c, g in zip(p.capacities_j_k, p.conductances_w_k)),
                               p.gpu_capacity_j_k / p.gpu_conductance_w_k,
                               p.sink_capacity_j_k / (sum(p.conductances_w_k)
                                   + p.gpu_conductance_w_k + p.ambient_conductance_w_k
                                   + sum(p.fan_conductances_w_k)))
        if stable_step < 0.0001:
            raise ValueError("thermal constants require an unsupported integration step")
        remaining = dt
        while remaining > 1e-12:
            h = min(0.02, stable_step, remaining)
            self.fans = tuple(a + (target - a) * (1 - exp(-h / tau))
                              for a, target, tau in zip(self.fans, targets, p.fan_time_constants_s))
            cores, gpu, sink = self.derivatives(core_w, gpu_w)
            self.cores_c = tuple(t + h * slope for t, slope in zip(self.cores_c, cores))
            self.gpu_c += h * gpu
            self.sink_c += h * sink
            remaining -= h


SCENARIOS = ("bursts", "long-prefill", "combined", "sensor-loss", "fan-failure", "idle", "random", "queue")


def workload(name, t):
    """Idealized utilization demand, not a vLLM throughput/phase timing model."""
    if name not in SCENARIOS:
        raise ValueError("unknown scenario")
    if name == "idle" or t < 10:
        return 0.0, 0.10, "idle", True, True
    phase = (t - 10) % 40
    if name == "bursts":
        # A second prompt arrives while the first request is still decoding.
        gpu, label = ((1.0, "prefill") if phase < 8 or 18 <= phase < 22
                      else (0.65, "decode") if phase < 30 else (0.0, "idle"))
    else:
        gpu, label = 1.0, "prefill"
    valid = not (name == "sensor-loss" and 30 <= t < 35)
    fan_ok = not (name == "fan-failure" and t >= 30)
    return gpu, 1.0 if name == "combined" else 0.2, label, valid, fan_ok


class Experiment:
    def __init__(self, scenario="bursts", settings=None, plant_parameters=None):
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario")
        self.scenario = scenario
        self.plant = Plant(plant_parameters or PlantParameters())
        # The synthetic twin equals the synthetic plant unless a test says otherwise.
        self.control = Supervisor(settings, twin=self.plant.p)
        self.sensed_cpu_c = self.plant.cpu_c
        self.sensed_gpu_c = self.plant.gpu_c
        self.time = 0.0
        self.previous_phase = "idle"
        self.events = []
        self.previous_mode = None
        self.previous_slot = None
        self.previous_queue = None
        self.pending_prefill_arrival = False
        self.previous_cpu_load = 0.0
        self.aborted = False
        self._cpu_history = []  # (t, sensed CPU C) over the guard's 2 s trend window

    def _cpu_projection(self):
        """The live policy's guard-style projection (energy_control/policy.py):
        2 s least-squares trend + 2 s x positive slope within 10 C of the abort."""
        history = self._cpu_history
        if len(history) < 2:
            return None, None
        n = len(history)
        mean_t = sum(t for t, _ in history) / n
        mean_v = sum(v for _, v in history) / n
        spread = sum((t - mean_t) ** 2 for t, _ in history)
        slope = sum((t - mean_t) * (v - mean_v) for t, v in history) / spread
        trend = mean_v + slope * (history[-1][0] - mean_t)
        if trend < self.control.s.cpu_emergency_c - 10.0:
            return trend, trend
        return trend + 2.0 * max(0.0, slope), trend

    def inject_prefill_arrival(self):
        """Model one admission with unchanged queue/active counts on the next tick."""
        if self.scenario != "queue" or self.aborted or self.control.s.queue_load.active_jobs == 0:
            raise ValueError("prefill injection requires an active queue scenario")
        self.pending_prefill_arrival = True

    def update_parameters(self, settings, plant_parameters):
        """Apply live edits without resetting time, physical state or PID memory."""
        old, new = self.control.s.random_load, settings.random_load
        gpu_changed = any(getattr(old, name) != getattr(new, name) for name in
                          ("gpu_min_pct", "gpu_max_pct", "hold_s", "seed"))
        self.control.s = settings
        self.control.cpu.gains = settings.cpu_gains
        self.control.gpu.gains = settings.gpu_gains
        self.plant.p = plant_parameters
        self.control.twin = plant_parameters
        if gpu_changed:
            self.previous_slot = None  # New demand is protected as a prefill epoch.
        self.events.append(f"{self.time:7.2f}s EDIT    parameters updated live")
        self.events = self.events[-12:]

    def step(self, dt=0.25):
        queued_jobs = active_jobs = cpu_cores = 0
        if self.aborted:
            gpu_load, cpu_load, phase, valid, fan_ok, arrival = 0.0, 0.0, "aborted", True, True, False
        elif self.scenario == "queue":
            demand = self.control.s.queue_load
            cpu_cores, queued_jobs, active_jobs = demand.cpu_cores, demand.queued_jobs, demand.active_jobs
            cpu_load = cpu_cores / 20
            # Illustrative saturation, not a measured vLLM scheduler response.
            gpu_load = (0.0, 0.55, 0.85, 1.0)[min(active_jobs, 3)]
            phase, valid, fan_ok = ("prefill" if active_jobs else "idle"), True, True
            previous = self.previous_queue
            arrival = (active_jobs > 0 and
                       (self.pending_prefill_arrival or previous is None
                        or queued_jobs > previous[0] or active_jobs > previous[1]))
            self.pending_prefill_arrival = False
            self.previous_queue = (queued_jobs, active_jobs)
        elif self.scenario == "random":
            gpu_load, cpu_load, slot = self.control.s.random_load.sample(self.time)
            phase, valid, fan_ok = "prefill" if gpu_load else "idle", True, True
            arrival = gpu_load > 0 and slot != self.previous_slot
            self.previous_slot = slot
        else:
            gpu_load, cpu_load, phase, valid, fan_ok = workload(self.scenario, self.time)
            arrival = phase == "prefill" and self.previous_phase != "prefill"
        done = phase == "idle" and self.previous_phase != "idle"
        p = self.plant
        if p.p.sensor_tau_s == 0:
            self.sensed_cpu_c, self.sensed_gpu_c = p.cpu_c, p.gpu_c
        self._cpu_history = [(t, v) for t, v in self._cpu_history if self.time - t < 2.0]
        self._cpu_history.append((self.time, self.sensed_cpu_c))
        observation = Observation(self.sensed_cpu_c, self.sensed_gpu_c,
                                  gpu_load, valid, fan_ok,
                                  prefill_arrival=arrival, workload_done=done,
                                  active_jobs=active_jobs if self.scenario == "queue" else None,
                                  cpu_demand_active=cpu_load > 0,
                                  cpu_work_arrival=cpu_load > self.previous_cpu_load,
                                  cpu_util=cpu_load,
                                  **dict(zip(("cpu_projected_c", "cpu_projected_basis_c"),
                                             self._cpu_projection())))
        self.previous_cpu_load = cpu_load
        cmd = self.control.step(observation, dt)
        if cmd.mode == "FAULT":
            self.aborted = True
            # No further synthetic work in this run after a test abort.
            gpu_load = cpu_load = 0.0
            active_jobs = queued_jobs = cpu_cores = 0
            phase = "aborted"
        # Log observations and decision BEFORE applying the command over [t,t+dt).
        # This event buffer is RAM-only synthetic output, not the future crash recorder.
        row = dict(t_s=self.time, phase=phase, mode=cmd.mode, reason=cmd.reason,
                   cpu_c=p.cpu_c, gpu_c=p.gpu_c, sink_c=p.sink_c,
                   observed_cpu_c=self.sensed_cpu_c,
                   observed_gpu_c=self.sensed_gpu_c,
                   gpu_util_pct=100 * gpu_load, cpu_util_pct=100 * cpu_load,
                   cpu_cores=cpu_cores, queued_jobs=queued_jobs, active_jobs=active_jobs,
                   test_aborted=self.aborted, gpu_cap_mhz=cmd.gpu_cap_mhz,
                   gpu_actual_mhz=p.actual_gpu_mhz, cpu_cap_ratio=cmd.cpu_ratio,
                   # Measured throughput model (doc/45), per request while LLM work runs.
                   llm_decode_tok_s=(GB10_LLM.decode_tok_s(
                       max(p.actual_gpu_mhz, 1.0),
                       CPU_FAST_MIN_MHZ + (CPU_FAST_MAX_MHZ - CPU_FAST_MIN_MHZ) * cmd.cpu_ratio)
                       if active_jobs else 0.0),
                   gpu_w=p.gpu_w, cpu_w=p.cpu_w, input_estimate_w=p.gpu_w + p.cpu_w + p.p.other_input_w,
                   fan_state=cmd.fan_state, fan_pct=p.fan * 100,
                   cpu_p=self.control.cpu.p, cpu_i=self.control.cpu.integral, cpu_d=self.control.cpu.d,
                   gpu_p=self.control.gpu.p, gpu_i=self.control.gpu.integral, gpu_d=self.control.gpu.d,
                   balance_cut_cpu_w=self.control.balance_info.get("cut_cpu_w", 0.0),
                   balance_cut_gpu_w=self.control.balance_info.get("cut_gpu_w", 0.0),
                   cpu_reserve_ratio=self.control.balance_info.get("reserve", 0.0),
                   sensor_valid=valid, fan_ok=fan_ok, prefill_arrival=arrival,
                   cpu_work_arrival=observation.cpu_work_arrival)
        if arrival or cmd.mode != self.previous_mode:
            self.events.append(f"{self.time:7.2f}s {cmd.mode:7s} {cmd.reason}")
            self.events = self.events[-12:]
        p.advance(cmd, gpu_load, cpu_load, dt, fan_stuck=not fan_ok)
        if p.p.sensor_tau_s == 0:
            self.sensed_cpu_c, self.sensed_gpu_c = p.cpu_c, p.gpu_c
        else:
            alpha = 1.0 - exp(-dt / p.p.sensor_tau_s)
            self.sensed_cpu_c += alpha * (p.cpu_c - self.sensed_cpu_c)
            self.sensed_gpu_c += alpha * (p.gpu_c - self.sensed_gpu_c)
        self.previous_phase, self.previous_mode = phase, cmd.mode
        self.time += dt
        return row
