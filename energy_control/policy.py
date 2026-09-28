"""Hardware-free shadow bridge from safety telemetry to candidate limits.

This reuses the synthetic simulator controller for trace replay only. Its gains,
entry clock and plant are not Lenovo-qualified. No result from this module is
permission to write a device or admit a workload.
"""

from dataclasses import dataclass, replace
from math import isfinite

from simulation.model import (GB10_FIT, Gains, Observation, Settings, Supervisor, clamp,
                              cluster_caps_mhz, cpu_class_caps, cpu_setpoint_ceiling)

from .broker import (CPU_FAST_FLOOR_MHZ, CPU_SLOW_FLOOR_MHZ, GPU_FLOOR_MHZ,
                     CPU_FAST_HARD_MAX_MHZ, CPU_SLOW_HARD_MAX_MHZ, TUNABLES,
                     Config)
from .safety import ABORT_C, PREDICTION_BAND_C, PREDICTION_S, CommissioningGuard, Snapshot


# Each cluster's own ACPI zone (doc/44 fit: P0->TS0P 42.9, P1->TS1P 38.1,
# E0->TS0E 17.4, E1->TS1E 15.0 C per load share), in owner order E0, P0, E1, P1.
CLUSTER_ZONES = ("acpi_ts0e", "acpi_ts0p", "acpi_ts1e", "acpi_ts1p")
# The ACPI GPU zone: under the 93 C ACPI abort, but heated by the GPU. It
# drives the GPU cap's zone loop and is no CPU proxy: cutting the CPU for it
# does not cool the GPU (matrix burn-in, 27 September 2026; defect 34).
GPU_ZONE = "acpi_tgpu"
# TSOC is the firmware maximum over all SoC zones, TGPU included: under GPU
# load it equals TGPU (1099 of 1114 samples in the GPU-only burn-in, above
# every exposed zone only by sampling skew, <= 1.1 C). As a CPU proxy it fed
# TGPU spikes into the CPU projection (96.6 C with the CPU zones at 65 C) and
# the balance cut the GPU 2200 -> 1900 MHz in one tick (doc/42 defect 37).
# The CPU zones are read individually; the guard keeps TSOC.
SOC_MAX_ZONE = "acpi_tsoc"


def _tunable_value(settings, name):
    """Effective value of a live tunable (some live inside Gains)."""
    if name == "cpu_wind_down_factor":
        return settings.cpu_gains.wind_down_factor
    gains = {"gpu_zone_kp": "kp", "gpu_zone_ki": "ki", "gpu_zone_kd": "kd",
             "gpu_zone_derivative_tau_s": "derivative_tau"}
    if name in gains:
        return getattr(settings.gpu_zone_gains, gains[name])
    return getattr(settings, name)


def _finite_or_none(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and _finite(value) and value >= 0 else None


def _cpu_side(name):
    name = name.lower()
    return name.startswith(("acpi", "cpu")) and name not in (GPU_ZONE, SOC_MAX_ZONE)
# CPU integral wind-down above target (Gains.wind_down_factor; doc/50).
CPU_WIND_DOWN_FACTOR = 3.0

# GPU cap commands cost one nvidia-smi call each and the owner session has a
# bounded command budget. Floor to this step (never above the controller cap)
# and raise only past a hysteresis margin; reductions are never delayed.
GPU_COMMAND_STEP_MHZ = 25
GPU_COMMAND_HYSTERESIS_MHZ = 5
CPU_COMMAND_STEP_MHZ = 25
CPU_COMMAND_RAISE_MHZ = 50
# After a live reduction of the GPU maximum the owner may show the previous
# committed limit for this long (telemetry time) before that is a fault.
GPU_LOWERING_GRACE_S = 5.0


def _finite(value):
    try:
        return isfinite(value)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class PolicyInput:
    safety: Snapshot
    gpu_util_pct: float
    dt_s: float
    prefill_arrival: bool = False
    workload_done: bool = False
    active_jobs: int | None = None
    cpu_demand_active: bool | None = None
    cpu_work_arrival: bool = False
    model_loading: bool | None = None  # None is unknown, never an explicit ready signal
    cpu_util_pct: float | None = None  # aggregate, for twin power balance only
    # Per-cluster utilisation % (E0, P0, E1, P1) from the service's 1 Hz /proc/stat
    # sampling; separates single-thread bursts from sustained load.
    cluster_util_pct: dict | None = None
    # Measured GPU power and the CPU power estimate, W (predictive fan feed-forward).
    gpu_power_w: float | None = None
    cpu_power_w: float | None = None


@dataclass(frozen=True)
class ProposedLimits:
    gpu_max_mhz: int
    cpu_fast_max_mhz: int  # class maxima: the highest P / E cluster cap
    cpu_slow_max_mhz: int
    fan_min_state: int
    abort_owned_loads: bool
    mode: str
    reasons: tuple[str, ...]
    hardware_qualified: bool = False
    # Per-cluster caps (E0, P0, E1, P1); None means uniform class caps.
    cpu_cluster_max_mhz: tuple[int, int, int, int] | None = None

    def cpu_clusters(self) -> tuple[int, int, int, int]:
        if self.cpu_cluster_max_mhz is not None:
            return tuple(self.cpu_cluster_max_mhz)
        return (self.cpu_slow_max_mhz, self.cpu_fast_max_mhz,
                self.cpu_slow_max_mhz, self.cpu_fast_max_mhz)


class ShadowPolicy:
    """Guarded candidate policy; never owns an actuator or workload gate."""

    def __init__(self, config: Config, *, gpu_evidence_mode="numeric_readback", gpu_setter_context=None):
        self._validate_config(config)
        self.config = config
        # Power balance uses the measured GB10 twin (GPU/copper/fan; CPU synthetic).
        self.supervisor = Supervisor(self._settings(config), twin=GB10_FIT)
        self.guard = CommissioningGuard(gpu_evidence_mode=gpu_evidence_mode,
                                        gpu_setter_context=gpu_setter_context)
        self._last_time: float | None = None
        self._fault_reasons: tuple[str, ...] = ()
        self._loading_signal_seen = False
        # Shadow proposals only, never accepted hardware clock evidence.
        self._cpu_output_caps: tuple[float, float] | None = None
        self._cluster_output_caps: tuple[float, ...] | None = None
        self._cluster_command_mhz: tuple[int, ...] | None = None
        self._gpu_output_mhz: int | None = None
        # (previous committed GPU maximum, telemetry deadline) after a live
        # reduction: the observed limit may exceed the new maximum only until
        # the owner has applied it, and never for longer than 5 s.
        self._gpu_lowering: tuple[int, float | None] | None = None
        # Last second of (time, worst guard projection, largest raw-over-trend
        # CPU-zone step): real 4 Hz ripple statistics for status and trace.
        self._ripple: list[tuple[float, float, float]] = []

    @staticmethod
    def _validate_config(config):
        if type(config) is not Config:
            raise TypeError("validated broker configuration required")
        if config.gpu_entry_mhz <= GPU_FLOOR_MHZ or config.gpu_max_mhz <= GPU_FLOOR_MHZ:
            raise ValueError("synthetic supervisor needs entry and max above its floor")

    @staticmethod
    def _settings(config):
        tuning = config.tuning_dict()
        wind_down = tuning.pop("cpu_wind_down_factor", CPU_WIND_DOWN_FACTOR)
        zone = {key: tuning.pop(name) for name, key in (
            ("gpu_zone_kp", "kp"), ("gpu_zone_ki", "ki"), ("gpu_zone_kd", "kd"),
            ("gpu_zone_derivative_tau_s", "derivative_tau")) if name in tuning}
        settings = ShadowPolicy._base_settings(config, wind_down)
        if zone:
            tuning["gpu_zone_gains"] = replace(settings.gpu_zone_gains, **zone)
        return replace(settings, **tuning) if tuning else settings

    @staticmethod
    def _base_settings(config, wind_down):
        return Settings(
            minimum_mhz=GPU_FLOOR_MHZ, baseline_mhz=config.gpu_entry_mhz,
            maximum_mhz=config.gpu_max_mhz,
            ramp_mhz_s=config.gpu_ramp_up_mhz_s,
            normal_down_mhz_s=config.gpu_ramp_down_mhz_s,
            cpu_entry_ratio=config.cpu_entry_ratio,
            cpu_recovery_s=config.cpu_recovery_ratio_s,
            cpu_normal_down_s=config.cpu_idle_down_ratio_s,
            cpu_target_c=config.cpu_target_c, gpu_target_c=config.gpu_target_c,
            fan_min_state=config.fan_min_state,
            fan_preferred_state=config.fan_preferred_state,
            fan_policy=config.fan_policy, fan_load_state=config.fan_load_state,
            fan_idle_delay_s=config.fan_idle_delay_s, guard_margin_c=config.guard_margin_c,
            priority_gpu=config.priority_gpu, priority_cpu=config.priority_cpu,
            busy_threshold=config.gpu_busy_threshold,
            cluster_max_ratio=ShadowPolicy._cluster_max_ratios(config),
            cpu_gains=Gains(config.cpu_kp, config.cpu_ki, config.cpu_kd,
                            config.cpu_derivative_tau_s, config.cpu_tracking_tau_s,
                            config.pid_integrator, wind_down),
            gpu_gains=Gains(config.gpu_kp, config.gpu_ki, config.gpu_kd,
                            config.gpu_derivative_tau_s, config.gpu_tracking_tau_s,
                            config.pid_integrator))

    @staticmethod
    def _cluster_max_ratios(config):
        """Operator per-cluster maxima as ratios of each class's hardware span.
        They bound the cluster loops, so a raise ramps in the loops' controlled
        way. The class maxima (restart fields) stay output limits with the
        output slew, as before."""
        spans = ((CPU_SLOW_FLOOR_MHZ, CPU_SLOW_HARD_MAX_MHZ),
                 (CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ)) * 2
        operator = (config.cpu_e0_max_mhz, config.cpu_p0_max_mhz,
                    config.cpu_e1_max_mhz, config.cpu_p1_max_mhz)
        return tuple(clamp((maximum - low) / (high - low), 0.0, 1.0)
                     for maximum, (low, high) in zip(operator, spans))

    def update_config(self, config: Config) -> None:
        """Change provisional policy without resetting PID, ramp, or guard state."""
        self._validate_config(config)
        if config.gpu_max_mhz < self.config.gpu_max_mhz:
            # The owner applies the lowered maximum within a tick or two; until
            # then the observed limit may still show the previous committed one.
            previous = self._gpu_lowering[0] if self._gpu_lowering else 0
            self._gpu_lowering = (max(previous, self.config.gpu_max_mhz), None)
        settings = self._settings(config)
        # The CPU PID runs on the adaptive setpoint, which follows a lowered
        # target at once but recovers toward a raised one gradually; transfer
        # bumplessly on the setpoint the next tick will actually use.
        old_setpoint = self.supervisor.cpu_setpoint
        ceiling = cpu_setpoint_ceiling(settings)
        new_setpoint = clamp(old_setpoint, ceiling - settings.setpoint_backoff_max_c, ceiling)
        self.supervisor.cpu_setpoint = new_setpoint
        for pid, gains, old_target, new_target in (
            (self.supervisor.cpu, settings.cpu_gains, old_setpoint, new_setpoint),
            (self.supervisor.gpu, settings.gpu_gains,
             self.config.gpu_target_c, config.gpu_target_c),
        ):
            if pid.previous is not None and (pid.gains != gains or old_target != new_target):
                old_error = old_target - pid.previous
                new_error = new_target - pid.previous
                pid.integral = clamp(pid.integral + pid.gains.kp * old_error
                                     - gains.kp * new_error
                                     + (gains.kd - pid.gains.kd) * pid.slope, 0, 1)
            pid.gains = gains
        # The cluster loops and the GPU zone loop hold their own Settings and
        # PIDs: hand them the new settings too, bumplessly (a live target or
        # gain change never reached them before, 27 September 2026).
        for loop in (*self.supervisor.clusters, self.supervisor.gpu_zone_loop):
            # Like the aggregate setpoint: a lowered ceiling applies at once, a
            # raised one is reached through the setpoint's gradual recovery.
            new_ceiling = loop.ceiling(settings)
            loop_setpoint = clamp(loop.setpoint,
                                  new_ceiling - settings.setpoint_backoff_max_c, new_ceiling)
            pid = loop.pid
            gains = getattr(settings, loop.gains_attr)
            if pid.previous is not None and (pid.gains != gains
                                             or loop_setpoint != loop.setpoint):
                old_error = loop.setpoint - pid.previous
                new_error = loop_setpoint - pid.previous
                pid.integral = clamp(pid.integral + pid.gains.kp * old_error
                                     - gains.kp * new_error
                                     + (gains.kd - pid.gains.kd) * pid.slope, 0, 1)
            pid.gains = gains
            loop.setpoint = loop_setpoint
            loop.s = settings
        old_entry = self.config.gpu_entry_mhz
        self.config = config
        self.supervisor.s = settings
        self.supervisor.cap = min(self.supervisor.cap, config.gpu_max_mhz)
        if config.gpu_entry_mhz < old_entry:
            self.supervisor.cap = min(self.supervisor.cap, config.gpu_entry_mhz)

    def _quantize_gpu(self, cap_mhz):
        cap = max(GPU_FLOOR_MHZ, min(self.config.gpu_max_mhz, cap_mhz))
        stepped = max(GPU_FLOOR_MHZ, int(cap // GPU_COMMAND_STEP_MHZ) * GPU_COMMAND_STEP_MHZ)
        previous = self._gpu_output_mhz
        if (previous is None or cap < previous
                or cap >= previous + GPU_COMMAND_STEP_MHZ + GPU_COMMAND_HYSTERESIS_MHZ
                or (cap >= self.config.gpu_max_mhz and previous < self.config.gpu_max_mhz)):
            previous = min(stepped, int(cap)) if cap < self.config.gpu_max_mhz else self.config.gpu_max_mhz
        self._gpu_output_mhz = previous
        return previous

    @staticmethod
    def _cpu_projection(snapshot: Snapshot):
        """The guard's projection for the CPU-side zones (safety.py formula):
        2 s trend plus PREDICTION_S x rise rate, within the prediction band.
        Returns (worst projection, that zone's trend) for the adaptive setpoint
        and the policy's predicted-breach check; (None, None) when unavailable."""
        values = []
        for sensor in snapshot.temperatures:
            if not _cpu_side(sensor.name):
                continue
            basis = sensor.trend_c if _finite(sensor.trend_c) and -10 < sensor.trend_c < 150 \
                else sensor.celsius
            rise = sensor.rising_c_per_s if _finite(sensor.rising_c_per_s) else 0.0
            if _finite(basis) and basis >= ABORT_C - PREDICTION_BAND_C:
                values.append((basis + PREDICTION_S * max(0.0, rise), basis))
            elif _finite(basis):
                values.append((basis, basis))
        return max(values) if values else (None, None)

    @staticmethod
    def _cpu_spike(snapshot: Snapshot):
        """Largest raw reading above its own 2 s trend over the CPU-side zones."""
        steps = [sensor.celsius - sensor.trend_c for sensor in snapshot.temperatures
                 if sensor.name.lower().startswith(("acpi", "cpu"))
                 and _finite(sensor.trend_c) and _finite(sensor.celsius)]
        return max(steps) if steps else None

    def _record_ripple(self, snapshot, projection):
        spike = self._cpu_spike(snapshot)
        now = snapshot.monotonic_s
        if not (_finite(now) and _finite(projection) and _finite(spike)):
            return
        self._ripple = [entry for entry in self._ripple if now - entry[0] < 1.0]
        self._ripple.append((now, projection, spike))

    def control_state(self) -> dict:
        """Controller internals for status/trace (no hardware meaning)."""
        control = self.supervisor
        ripple = self._ripple
        return {"cpu_projection_max_1s_c": (round(max(e[1] for e in ripple), 2)
                                            if ripple else None),
                "cpu_spike_max_1s_c": round(max(e[2] for e in ripple), 2) if ripple else None,
                "cpu_setpoint_c": round(control.cpu_setpoint, 2),
                "cpu_target_c": self.config.cpu_target_c,
                "near_miss_s": round(control.near_miss_s, 1),
                "cpu_integral": round(control.cpu.integral, 3),
                "gpu_integral": round(control.gpu.integral, 3),
                "fan_policy": self.config.fan_policy,
                "pid_integrator": self.config.pid_integrator,
                "cpu_control": self.config.cpu_control,
                "cluster_setpoints_c": [round(loop.setpoint, 2) for loop in control.clusters],
                "cluster_learned_caps": [round(loop.learned, 3) for loop in control.clusters],
                "gpu_zone_setpoint_c": round(control.gpu_zone_loop.setpoint, 2),
                "gpu_zone_cap": round(control.gpu_zone_loop.cap, 3),
                # Every live model tunable at its effective value (export).
                "tuning": {name: _tunable_value(control.s, name) for name in TUNABLES},
                "workloads": {key: (round(value, 3) if isinstance(value, float) else value)
                              for key, value in control.workloads.items()},
                "priorities": {"gpu": self.config.priority_gpu, "cpu": self.config.priority_cpu},
                "fan": dict(getattr(control, "fan_info", {}) or {})}

    @staticmethod
    def _temperatures(snapshot: Snapshot):
        # Control on each sensor's 2 s least-squares trend: P-cluster zones spike
        # by several degrees between samples, and a raw spike caused needless
        # derates (live, 26 September 2026). The guard keeps the raw hard limits.
        def value(sensor):
            trend = sensor.trend_c
            return trend if _finite(trend) and -10 < trend < 150 else sensor.celsius
        cpu = [value(sensor) for sensor in snapshot.temperatures if _cpu_side(sensor.name)]
        gpu = [value(sensor) for sensor in snapshot.temperatures
               if sensor.name.lower() == "gpu"]
        if not cpu or len(gpu) != 1:
            raise ValueError("CPU proxy and one GPU temperature required")
        return max(cpu), gpu[0]

    def step(self, sample: PolicyInput) -> ProposedLimits:
        if not isinstance(sample, PolicyInput):
            raise TypeError("typed policy input required")
        decision = self.guard.evaluate(sample.safety)
        invalid = []
        if sample.model_loading is not None:
            if type(sample.model_loading) is not bool:
                invalid.append("invalid model lifecycle signal")
            else:
                self._loading_signal_seen = True
        elif self._loading_signal_seen:
            invalid.append("model lifecycle signal lost")
        if (type(sample.prefill_arrival) is not bool or type(sample.workload_done) is not bool
                or (sample.active_jobs is not None and
                    (type(sample.active_jobs) is not int or not 0 <= sample.active_jobs <= 20))
                or type(sample.gpu_util_pct) not in (int, float)
                or not _finite(sample.gpu_util_pct) or not 0 <= sample.gpu_util_pct <= 100
                or (sample.cpu_util_pct is not None and
                    (type(sample.cpu_util_pct) not in (int, float)
                     or not _finite(sample.cpu_util_pct) or not 0 <= sample.cpu_util_pct <= 100))):
            invalid.append("invalid workload signal")
        if type(sample.dt_s) not in (int, float) or not _finite(sample.dt_s) or not 0 < sample.dt_s <= 1:
            invalid.append("invalid control interval")
        allowed_gpu = self.config.gpu_max_mhz
        if self._gpu_lowering is not None and isinstance(sample.safety, Snapshot):
            previous, deadline = self._gpu_lowering
            observed = [limit for limit in (sample.safety.gpu_requested_max_mhz,
                                            sample.safety.gpu_accepted_max_mhz)
                        if _finite(limit)]
            now = sample.safety.monotonic_s if _finite(sample.safety.monotonic_s) else None
            if deadline is None and now is not None:
                deadline = now + GPU_LOWERING_GRACE_S
                self._gpu_lowering = (previous, deadline)
            if all(limit <= self.config.gpu_max_mhz for limit in observed):
                self._gpu_lowering = None  # applied: the committed maximum holds again
            elif now is not None and deadline is not None and now <= deadline:
                allowed_gpu = previous
        if (isinstance(sample.safety, Snapshot)
                and any(_finite(limit) and limit > allowed_gpu
                        for limit in (sample.safety.gpu_requested_max_mhz,
                                      sample.safety.gpu_accepted_max_mhz))):
            invalid.append("observed GPU limit exceeds committed policy")
        if (self._last_time is not None and isinstance(sample.safety, Snapshot)
                and _finite(sample.safety.monotonic_s) and _finite(sample.dt_s)):
            elapsed = sample.safety.monotonic_s - self._last_time
            # dt is capped at 1 s; a telemetry gap of up to 3 s (slow frames
            # under load) integrates as 1 s instead of faulting.
            if not 0 < elapsed <= 3.0 or abs(min(elapsed, 1.0) - sample.dt_s) > 0.1:
                invalid.append("control interval differs from telemetry time")
        if isinstance(sample.safety, Snapshot) and _finite(sample.safety.monotonic_s):
            self._last_time = sample.safety.monotonic_s
        if invalid and not self._fault_reasons:
            self._fault_reasons = tuple(invalid)
        if decision.abort or self._fault_reasons:
            return ProposedLimits(GPU_FLOOR_MHZ, CPU_FAST_FLOOR_MHZ,
                                  CPU_SLOW_FLOOR_MHZ, 12, True, "ABORT",
                                  decision.reasons + self._fault_reasons)
        try:
            cpu_c, gpu_c = self._temperatures(sample.safety)
            projection, basis = self._cpu_projection(sample.safety)
            self._record_ripple(sample.safety, projection)
            command = self.supervisor.step(
                Observation(cpu_c=cpu_c, gpu_c=gpu_c,
                            gpu_util=sample.gpu_util_pct / 100,
                            prefill_arrival=sample.prefill_arrival,
                            workload_done=sample.workload_done,
                            active_jobs=sample.active_jobs,
                            cpu_demand_active=sample.cpu_demand_active,
                            cpu_work_arrival=sample.cpu_work_arrival,
                            model_loading=sample.model_loading is True,
                            cpu_util=(None if sample.cpu_util_pct is None
                                      else sample.cpu_util_pct / 100),
                            cpu_projected_c=projection,
                            cpu_projected_basis_c=basis,
                            cpu_zones=(self._cluster_zones(sample.safety)
                                       if self.config.cpu_control == "cluster" else None),
                            cluster_util=self._cluster_util(sample.cluster_util_pct),
                            gpu_zone=self._gpu_zone(sample.safety),
                            gpu_w=_finite_or_none(sample.gpu_power_w),
                            cpu_w=_finite_or_none(sample.cpu_power_w)),
                sample.dt_s,
                track_cpu=False)
        except (ValueError, TypeError):
            self._fault_reasons = ("invalid controller input",)
            return ProposedLimits(GPU_FLOOR_MHZ, CPU_FAST_FLOOR_MHZ,
                                  CPU_SLOW_FLOOR_MHZ, 12, True, "ABORT",
                                  self._fault_reasons)
        if command.mode == "FAULT":
            self._fault_reasons = (command.reason,)
            return ProposedLimits(GPU_FLOOR_MHZ, CPU_FAST_FLOOR_MHZ,
                                  CPU_SLOW_FLOOR_MHZ, 12, True, "ABORT",
                                  self._fault_reasons)
        fast, slow = cpu_class_caps(command.cpu_ratio)
        fast = min(fast, self.config.cpu_fast_max_mhz)
        slow = min(slow, self.config.cpu_slow_max_mhz)
        if self._cpu_output_caps is not None:
            previous_fast, previous_slow = self._cpu_output_caps
            step = self.config.cpu_recovery_ratio_s * sample.dt_s
            fast = min(fast, previous_fast +
                       (CPU_FAST_HARD_MAX_MHZ - CPU_FAST_FLOOR_MHZ) * step)
            # Preserve the baseline fast-first mapping's slow-class slope.
            slow = min(slow, previous_slow +
                       (CPU_SLOW_HARD_MAX_MHZ - CPU_SLOW_FLOOR_MHZ) * step / 0.75)
        # Keep fractional MHz internally so small rates do not stall after
        # integer output quantization. Reductions are never slew-limited.
        self._cpu_output_caps = (fast, slow)
        fast_output = max(CPU_FAST_FLOOR_MHZ, int(fast))
        slow_output = max(CPU_SLOW_FLOOR_MHZ, int(slow))
        # Track once, after every downstream constraint and quantization.
        # A single PID cannot exactly represent independently clipped classes;
        # use the most constrained inverse of the baseline class mapping.
        fast_ratio = ((fast_output - CPU_FAST_FLOOR_MHZ) /
                      (CPU_FAST_HARD_MAX_MHZ - CPU_FAST_FLOOR_MHZ))
        slow_ratio = (1.0 if slow_output == CPU_SLOW_HARD_MAX_MHZ else
                      0.75 * (slow_output - CPU_SLOW_FLOOR_MHZ) /
                      (CPU_SLOW_HARD_MAX_MHZ - CPU_SLOW_FLOOR_MHZ))
        # Add the power-balance offset: a CPU demand served by a GPU cut is
        # not actuator saturation and must not wind the CPU integral down.
        self.supervisor.cpu.track(clamp(min(command.cpu_ratio, fast_ratio, slow_ratio)
                                        + self.supervisor.cpu_track_offset, 0, 1), sample.dt_s)
        curve_floor = max((state for temperature, state in self.config.fan_curve
                           if max(cpu_c, gpu_c) >= temperature), default=0)
        clusters = None
        if command.cluster_ratios is not None:
            clusters = self._cluster_outputs(command.cluster_ratios, sample.dt_s)
            slow_output, fast_output = max(clusters[0], clusters[2]), max(clusters[1], clusters[3])
        else:
            self._cluster_output_caps = None
            uniform = (slow_output, fast_output) * 2
            maxima = self.config.cpu_cluster_maxima()
            if any(value > maximum for value, maximum in zip(uniform, maxima)):
                # Class control with operator per-cluster maxima below the class caps.
                clusters = tuple(min(value, maximum) for value, maximum in zip(uniform, maxima))
                slow_output, fast_output = (max(clusters[0], clusters[2]),
                                            max(clusters[1], clusters[3]))
        return ProposedLimits(
            self._quantize_gpu(command.gpu_cap_mhz),
            fast_output, slow_output,
            max(self.config.fan_min_state, command.fan_state, curve_floor), False,
            command.mode, (command.reason,), cpu_cluster_max_mhz=clusters)

    def _cluster_outputs(self, ratios, dt):
        """Per-cluster MHz (E0, P0, E1, P1): class maxima, full-rate output slew
        (the loops taper themselves), integer MHz; reductions never delayed."""
        wanted = cluster_caps_mhz(ratios)
        maxima = self.config.cpu_cluster_maxima()  # operator maxima within the class maxima
        # E clusters keep the fast-first mapping's slope (span / 0.75), as the
        # class path does, so their ramp never binds before their P neighbour's.
        spans = ((CPU_SLOW_HARD_MAX_MHZ - CPU_SLOW_FLOOR_MHZ) / 0.75,
                 CPU_FAST_HARD_MAX_MHZ - CPU_FAST_FLOOR_MHZ) * 2
        previous = self._cluster_output_caps
        caps = []
        for i, value in enumerate(wanted):
            value = min(value, maxima[i])
            if previous is not None:
                value = min(value, previous[i] + spans[i] * self.config.cpu_recovery_ratio_s * dt)
            caps.append(value)
        self._cluster_output_caps = tuple(caps)
        floors = (CPU_SLOW_FLOOR_MHZ, CPU_FAST_FLOOR_MHZ) * 2
        # Command shaping (doc/48 4.6): 25 MHz steps, raises only past 50 MHz,
        # reductions at once. Every CPU command is a 20-policy sysfs batch; per
        # tick commands kept the owner busy and one slow batch under full load
        # outlived the guard's evidence window (27 September 16:59).
        previous_out = self._cluster_command_mhz
        out = []
        for i, (floor, value) in enumerate(zip(floors, caps)):
            stepped = max(floor, int(value) // CPU_COMMAND_STEP_MHZ * CPU_COMMAND_STEP_MHZ)
            stepped = max(floor, min(stepped, int(value)))
            if value >= maxima[i] - 0.5:
                # The maximum (class or operator) is always reached exactly,
                # also when the ratio mapping lands a hair below it.
                stepped = maxima[i]
            elif previous_out is not None and previous_out[i] <= int(value):
                if stepped < previous_out[i] + CPU_COMMAND_RAISE_MHZ:
                    stepped = previous_out[i]  # hold: raise not yet worth a command
            out.append(stepped)
        self._cluster_command_mhz = tuple(out)
        return self._cluster_command_mhz

    @staticmethod
    def _cluster_util(values):
        """(E0, P0, E1, P1) utilisation 0..1, or None when unknown or invalid."""
        if not isinstance(values, dict):
            return None
        result = []
        for name in ("E0", "P0", "E1", "P1"):
            value = values.get(name)
            if not _finite(value) or not 0 <= value <= 100:
                return None
            result.append(value / 100)
        return tuple(result)

    @staticmethod
    def _cluster_zones(snapshot: Snapshot):
        """(trend, guard projection) of TS0E, TS0P, TS1E, TS1P, or None."""
        by_name = {sensor.name.lower(): sensor for sensor in snapshot.temperatures}
        zones = []
        for name in CLUSTER_ZONES:
            sensor = by_name.get(name)
            if sensor is None:
                return None
            trend = sensor.trend_c if _finite(sensor.trend_c) and -10 < sensor.trend_c < 150 \
                else sensor.celsius
            rise = sensor.rising_c_per_s if _finite(sensor.rising_c_per_s) else 0.0
            if not _finite(trend):
                return None
            projection = (trend + PREDICTION_S * max(0.0, rise)
                          if trend >= ABORT_C - PREDICTION_BAND_C else trend)
            zones.append((trend, projection))
        return tuple(zones)

    @staticmethod
    def _gpu_zone(snapshot: Snapshot):
        """(trend, guard projection) of the ACPI GPU zone, or None."""
        for sensor in snapshot.temperatures:
            if sensor.name.lower() != GPU_ZONE:
                continue
            trend = sensor.trend_c if _finite(sensor.trend_c) and -10 < sensor.trend_c < 150 \
                else sensor.celsius
            rise = sensor.rising_c_per_s if _finite(sensor.rising_c_per_s) else 0.0
            if not _finite(trend):
                return None
            projection = (trend + PREDICTION_S * max(0.0, rise)
                          if trend >= ABORT_C - PREDICTION_BAND_C else trend)
            return (trend, projection)
        return None
