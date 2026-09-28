"""Hardware-free root-broker policy core with independent safety limits.

This module has no socket, file or device I/O. A future privileged process must
obtain peer UID from SO_PEERCRED, persist an audit/intent before writes, and use
qualified adapters. Do not connect this prototype to physical actuators.
"""

from dataclasses import asdict, dataclass, replace
from hashlib import scrypt, sha256
from hmac import compare_digest
import json
from math import isfinite
from secrets import token_bytes, token_urlsafe
from threading import RLock
from time import monotonic
from typing import Protocol

from .safety import ABORT_C, GPU_ABORT_C


from .limits import CPU_TARGET_QUALIFIED_MAX_C, GPU_HARD_MAX_MHZ  # noqa: E402 (single source)
GPU_FLOOR_MHZ = 500
CPU_FAST_HARD_MAX_MHZ = 3900
CPU_SLOW_HARD_MAX_MHZ = 2808
CPU_FAST_FLOOR_MHZ = 1378
CPU_SLOW_FLOOR_MHZ = 338
TARGET_MARGIN_C = 2.0  # Targets stay at least this far below the fixed aborts.
# The qualified maximum (limits.py) is the stricter bound today (90 C).
CPU_TARGET_MAX_C = min(ABORT_C - TARGET_MARGIN_C, CPU_TARGET_QUALIFIED_MAX_C)
GPU_TARGET_MAX_C = GPU_ABORT_C - TARGET_MARGIN_C
PROPOSAL_TTL_S = 300
AUTH_TTL_S = 60
MAX_PENDING = 64
_FIELDS = frozenset({"gpu_max_mhz", "gpu_entry_mhz", "cpu_fast_max_mhz",
                     "cpu_slow_max_mhz", "fan_min_state", "fan_preferred_state",
                     "cpu_target_c",
                     "gpu_target_c", "gpu_ramp_up_mhz_s", "gpu_ramp_down_mhz_s",
                     "cpu_kp", "cpu_ki", "cpu_kd", "gpu_kp", "gpu_ki", "gpu_kd",
                     "cpu_derivative_tau_s", "gpu_derivative_tau_s",
                     "cpu_entry_ratio", "cpu_recovery_ratio_s", "cpu_idle_down_ratio_s",
                     "cpu_tracking_tau_s", "gpu_tracking_tau_s", "fan_curve",
                     "fan_policy", "fan_load_state", "fan_idle_delay_s",
                     "guard_margin_c", "pid_integrator", "cpu_control",
                     "priority_gpu", "priority_cpu", "gpu_busy_threshold",
                     "cpu_e0_max_mhz", "cpu_p0_max_mhz", "cpu_e1_max_mhz",
                     "cpu_p1_max_mhz", "tuning"})
# Live model tunables (operator, 27 September 2026: "alle Variablen / PID
# exportieren und änderbar machen"; a restart only for model changes). Each is
# a simulation.model Settings field (or the CPU wind-down factor) with a safe
# range. Not tunable: the aborts and the guard-mirror rules (prediction_*),
# the hard and entry clocks (Config fields) and REARM (entry_fallback, PSU).
# The independent guard is unaffected by any of them.
TUNABLES = {
    "busy_dwell_s": (0.25, 10.0), "low_load_timeout_s": (0.25, 10.0),
    "idle_util_threshold": (0.05, 0.5),
    "guard_band_c": (0.5, 3.0), "pid_band_c": (4.0, 20.0),
    "cpu_reservation_ratio": (0.0, 0.5), "reservation_yield_c": (0.0, 5.0),
    "gpu_cost_per_w": (0.1, 5.0), "cpu_cost_per_w": (0.1, 5.0),
    "cpu_job_cost_per_w": (0.1, 5.0), "cpu_job_util": (0.05, 0.5),
    "cpu_spill_fraction": (0.0, 1.0),
    "fan_derate_dwell_s": (1.0, 30.0), "fan_step_s": (1.0, 30.0),
    "fan_down_dwell_s": (5.0, 120.0), "fan_boost_headroom": (0.1, 0.9),
    "fan_target_band_c": (0.0, 5.0), "fan_release_headroom": (0.2, 1.0),
    "emergency_hysteresis_c": (2.0, 15.0), "emergency_recovery_s": (5.0, 120.0),
    "gpu_zone_taper_band_c": (2.0, 30.0), "trend_margin_c": (1.0, 8.0),
    "setpoint_backoff_c_s": (0.2, 5.0), "setpoint_recovery_c_s": (0.001, 0.2),
    "setpoint_backoff_max_c": (1.0, 15.0), "recovery_taper_band_c": (3.0, 30.0),
    "recovery_taper_min": (0.01, 1.0), "relief_projection_margin_c": (0.0, 10.0),
    "cluster_busy_util": (0.1, 0.9), "cluster_full_util": (0.5, 1.0),
    "partial_cap_margin": (0.0, 0.3), "learn_tau_s": (5.0, 600.0),
    "learned_cap_initial_p": (0.3, 1.0), "cpu_wind_down_factor": (1.0, 10.0),
    # TGPU zone loop (doc/55): its own gains and the spike step.
    "gpu_zone_kp": (0.005, 0.15), "gpu_zone_ki": (0.0, 0.03), "gpu_zone_kd": (0.0, 0.15),
    "gpu_zone_derivative_tau_s": (0.25, 10.0), "gpu_zone_spike_step": (0.005, 0.3),
    "gpu_zone_margin_c": (0.0, 10.0),
    # Predictive fan (fan_policy "predictive"): targets, feedback band, the
    # fitted cooler and the load-start anticipation.
    "fan_plate_target_c": (35.0, 75.0), "fan_fb_band_c": (4.0, 25.0), "fan_fb_full_c": (0.0, 10.0),
    "fan_neck_w_k": (1.0, 10.0), "fan_air_g0_w_k": (0.5, 5.0), "fan_air_g1_w_k": (0.2, 6.0),
    "fan_room_c": (10.0, 40.0), "fan_background_w": (0.0, 40.0),
    "fan_anticipate_s": (0.0, 120.0), "fan_power_decay_s": (5.0, 900.0), "fan_release_step_s": (5.0, 300.0),
    # REARM to the entry ceiling on each new prefill: 0 = off (operator, 28 Sep
    # 2026: load is detected by GPU utilisation only), 1 = on, e.g. for owned
    # cold-to-prefill qualification trials through the live override.
    "prefill_rearm": (0.0, 1.0),
}


# Renamed fields: old configuration files and audit records still load.
LEGACY_FIELDS = {"priority_llm": "priority_gpu"}   # 27 September 2026: "LLM" was wrong


def migrate_legacy_fields(values):
    """Copy of ``values`` with renamed fields under their current names."""
    values = dict(values)
    for old, new in LEGACY_FIELDS.items():
        if old in values:
            value = values.pop(old)
            values.setdefault(new, value)
    return values


def normalize_tuning(value):
    """Canonical tuning: a sorted tuple of (name, float) from a dict or pairs."""
    if isinstance(value, dict):
        pairs = list(value.items())
    elif isinstance(value, (list, tuple)):
        pairs = []
        for pair in value:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("tuning entries must be (name, value) pairs")
            pairs.append((pair[0], pair[1]))
    else:
        raise ValueError("tuning must be a mapping of tunable names to numbers")
    result = {}
    for name, number in pairs:
        if name not in TUNABLES or name in result:
            raise ValueError(f"unknown or repeated tunable: {name!r}")
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValueError(f"tunable {name} must be a number")
        result[name] = float(number)
    return tuple(sorted(result.items()))
# Operator per-cluster CPU maxima (E0, P0, E1, P1) and their class envelopes.
CPU_CLUSTER_MAX_FIELDS = ("cpu_e0_max_mhz", "cpu_p0_max_mhz", "cpu_e1_max_mhz",
                          "cpu_p1_max_mhz")
CPU_CONTROLS = ("cluster", "class")
FAN_POLICIES = ("load", "staging", "predictive")
PID_INTEGRATORS = ("conditional", "tracking")


def _number(value, low, high):
    return type(value) in (int, float) and isfinite(value) and low <= value <= high


@dataclass(frozen=True)
class Config:
    gpu_max_mhz: int = 1200
    gpu_entry_mhz: int = 1200
    cpu_fast_max_mhz: int = CPU_FAST_HARD_MAX_MHZ
    cpu_slow_max_mhz: int = CPU_SLOW_HARD_MAX_MHZ
    fan_min_state: int = 12
    # Normal-operation staging level for the additive floor. The floor never
    # drops below fan_min_state and may rise to 12 when temperatures require it.
    fan_preferred_state: int = 6
    cpu_target_c: float = 92.0  # operator, 27 September 2026 (was 90)
    gpu_target_c: float = 75.0
    gpu_ramp_up_mhz_s: float = 100.0
    gpu_ramp_down_mhz_s: float = 150.0
    cpu_entry_ratio: float = 0.5
    cpu_recovery_ratio_s: float = 0.03
    cpu_idle_down_ratio_s: float = 0.1
    cpu_kp: float = 0.075
    cpu_ki: float = 0.012
    cpu_kd: float = 0.060
    gpu_kp: float = 0.060
    gpu_ki: float = 0.006
    # 0.04 (was 0.08): the calibrated twin with an integer GPU sensor showed the
    # dither of TH run 12 only with kd 0.08; 0.04 keeps the 1 s cut of a real
    # 2 C/s rise (analysis/tune_gpu_loop.py, 27 September 2026).
    gpu_kd: float = 0.04
    cpu_derivative_tau_s: float = 1.0
    # 3 s (was 1 s): the integer GPU sensor's trend still made derivative kicks
    # that cycled the cap under a near target (TH run 10, 27 September 2026);
    # a real 2 C/s rise is still cut within 1 s (tests/test_coordinated_control).
    gpu_derivative_tau_s: float = 3.0
    cpu_tracking_tau_s: float = 2.0
    gpu_tracking_tau_s: float = 2.0
    # Minimum-cooling floor by hottest sensor. Goal v2 staging holds the
    # preferred level at the 75/90 C targets; this curve adds cooling only
    # past the CPU target, reaching 12 before the 93 C abort.
    fan_curve: tuple[tuple[int, int], ...] = ((70, 3), (85, 5), (90, 6),
                                              (91, 9), (92, 12))
    # doc/48 §0 (27 September 2026): fan level under load (D1), guard-margin
    # adaptive CPU setpoint (D3) and the PID integrator (D2, defect 27).
    fan_policy: str = "load"
    fan_load_state: int = 12
    fan_idle_delay_s: float = 300.0
    guard_margin_c: float = 2.0  # with the 87 C setpoint ceiling (doc/49, doc/50)
    pid_integrator: str = "conditional"
    # Per-cluster CPU loops and caps (D7) or uniform class caps (A/B, rollback).
    cpu_control: str = "cluster"
    # Workload priorities for cuts on shared limits (D5, doc/48 §0.1). Default
    # LLM 2 : CPU jobs 1 from the operator's "2/3 GPU, 1/3 CPU".
    # GPU (LLM) : CPU = 1 : 1 (operator, 27 September 2026; was 2 : 1).
    priority_gpu: float = 1.0
    priority_cpu: float = 1.0
    # GPU utilisation from which the cap may ramp above the entry ceiling
    # (operator, 27 September 2026: detection value 75 %; was 0.95: LLM decode
    # at ~92 % held the GPU at 1700 MHz). The PSU entry logic (REARM on every
    # new prefill, entry ceiling, 100 MHz/s ramp) is unchanged.
    gpu_busy_threshold: float = 0.75
    # Operator per-cluster CPU maxima (operator, 27 September 2026: settable
    # from the dashboard in operation). Live policy bounds below the class
    # maxima above; the cluster loops ramp to a raised value in their
    # controlled way, a lowered value applies at once.
    cpu_e0_max_mhz: int = CPU_SLOW_HARD_MAX_MHZ
    cpu_p0_max_mhz: int = CPU_FAST_HARD_MAX_MHZ
    cpu_e1_max_mhz: int = CPU_SLOW_HARD_MAX_MHZ
    cpu_p1_max_mhz: int = CPU_FAST_HARD_MAX_MHZ
    # Live model tunables (TUNABLES): sorted (name, value) pairs; empty = model
    # defaults. Applied without a restart, bumplessly (policy.update_config).
    tuning: tuple = ()

    def tuning_dict(self):
        return dict(self.tuning)

    def cpu_cluster_maxima(self):
        """Effective (E0, P0, E1, P1) maxima: operator value within its class maximum."""
        return (min(self.cpu_e0_max_mhz, self.cpu_slow_max_mhz),
                min(self.cpu_p0_max_mhz, self.cpu_fast_max_mhz),
                min(self.cpu_e1_max_mhz, self.cpu_slow_max_mhz),
                min(self.cpu_p1_max_mhz, self.cpu_fast_max_mhz))

    def __post_init__(self):
        for field_name, low, high in (
            ("gpu_max_mhz", GPU_FLOOR_MHZ, GPU_HARD_MAX_MHZ),
            ("gpu_entry_mhz", GPU_FLOOR_MHZ, GPU_HARD_MAX_MHZ),
            ("cpu_fast_max_mhz", CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ),
            ("cpu_slow_max_mhz", CPU_SLOW_FLOOR_MHZ, CPU_SLOW_HARD_MAX_MHZ),
            ("cpu_e0_max_mhz", CPU_SLOW_FLOOR_MHZ, CPU_SLOW_HARD_MAX_MHZ),
            ("cpu_p0_max_mhz", CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ),
            ("cpu_e1_max_mhz", CPU_SLOW_FLOOR_MHZ, CPU_SLOW_HARD_MAX_MHZ),
            ("cpu_p1_max_mhz", CPU_FAST_FLOOR_MHZ, CPU_FAST_HARD_MAX_MHZ),
        ):
            value = getattr(self, field_name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{field_name} outside hard envelope")
        if self.gpu_entry_mhz > self.gpu_max_mhz:
            raise ValueError("entry cap must not exceed GPU maximum")
        if isinstance(self.tuning, (list, dict)):
            # JSON-restored configurations (config file, audit records) carry lists.
            object.__setattr__(self, "tuning", normalize_tuning(self.tuning))
        if type(self.tuning) is not tuple or normalize_tuning(self.tuning) != self.tuning:
            raise ValueError("tuning must be canonical (sorted name/value pairs)")
        for name, number in self.tuning:
            low, high = TUNABLES[name]
            if not _number(number, low, high):
                raise ValueError(f"tunable {name} outside {low:g}..{high:g}")
        if type(self.fan_min_state) is not int or not 0 <= self.fan_min_state <= 12:
            raise ValueError("fan minimum state outside 0..12")
        if type(self.fan_preferred_state) is not int or not 0 <= self.fan_preferred_state <= 12:
            raise ValueError("fan preferred state outside 0..12")
        if not _number(self.cpu_target_c, 40, CPU_TARGET_MAX_C):
            raise ValueError(f"CPU target must be at most {CPU_TARGET_MAX_C:g} C "
                             f"(qualified maximum, below the {ABORT_C:g} C abort)")
        if self.fan_policy not in FAN_POLICIES:
            raise ValueError("fan policy must be 'load' or 'staging'")
        if type(self.fan_load_state) is not int or not 0 <= self.fan_load_state <= 12:
            raise ValueError("fan load state outside 0..12")
        if not _number(self.fan_idle_delay_s, 30, 3600):
            raise ValueError("fan idle delay outside 30..3600 s")
        # A smaller margin would let the setpoint sit closer to the guard's
        # immediate-projection abort; it can only be widened, never removed.
        if not _number(self.guard_margin_c, 1.0, 3.0):
            raise ValueError("guard margin outside 1..3 C")
        if self.pid_integrator not in PID_INTEGRATORS:
            raise ValueError("PID integrator must be 'conditional' or 'tracking'")
        if self.cpu_control not in CPU_CONTROLS:
            raise ValueError("CPU control must be 'cluster' or 'class'")
        if not _number(self.gpu_busy_threshold, 0.3, 1.0):
            raise ValueError("gpu_busy_threshold outside 0.3..1.0")
        for field_name in ("priority_gpu", "priority_cpu"):
            if not _number(getattr(self, field_name), 0.1, 10):
                raise ValueError(f"{field_name} outside 0.1..10")
        if not _number(self.gpu_target_c, 40, GPU_TARGET_MAX_C):
            raise ValueError("GPU target must remain below the 85 C GPU abort")
        if not _number(self.gpu_ramp_up_mhz_s, 1, 200):
            raise ValueError("unsafe GPU ramp-up rate")
        if not _number(self.gpu_ramp_down_mhz_s, 1, 500):
            raise ValueError("unsafe GPU ramp-down rate")
        for field_name, low, high in (
            ("cpu_entry_ratio", 0.1, 1),
            ("cpu_recovery_ratio_s", 0.001, 0.1),
            ("cpu_idle_down_ratio_s", 0.001, 0.2),
            ("cpu_kp", 0.01, 0.15), ("cpu_ki", 0, 0.03), ("cpu_kd", 0, 0.15),
            ("gpu_kp", 0.01, 0.12), ("gpu_ki", 0, 0.02), ("gpu_kd", 0, 0.16),
            ("cpu_derivative_tau_s", 0.25, 5),
            ("gpu_derivative_tau_s", 0.25, 5),
            ("cpu_tracking_tau_s", 0.5, 10),
            ("gpu_tracking_tau_s", 0.5, 10),
        ):
            if not _number(getattr(self, field_name), low, high):
                raise ValueError(f"{field_name} outside provisional PID envelope")
        if (type(self.fan_curve) is not tuple or not 1 <= len(self.fan_curve) <= 12
                or any(type(point) is not tuple or len(point) != 2 for point in self.fan_curve)):
            raise ValueError("invalid fan curve")
        previous_temp, previous_state = -100, -1
        for temperature, state in self.fan_curve:
            if (type(temperature) is not int or type(state) is not int
                    or not 20 <= temperature < ABORT_C or not 0 <= state <= 12
                    or temperature <= previous_temp or state < previous_state):
                raise ValueError("fan curve must increase in temperature and not reduce minimum cooling")
            previous_temp, previous_state = temperature, state


def config_fingerprint(config: Config) -> str:
    if type(config) is not Config:
        raise TypeError("validated broker configuration required")
    return sha256(json.dumps(asdict(config), sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PasswordVerifier:
    salt: bytes
    digest: bytes

    @classmethod
    def provision(cls, password: str):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            raise ValueError("operator password must be 12..256 characters")
        salt = token_bytes(16)
        return cls(salt, scrypt(password.encode(), salt=salt, n=2**14, r=8, p=5,
                                maxmem=64 * 1024**2))

    def verify(self, password: str):
        if not isinstance(password, str) or len(password) > 256:
            return False
        candidate = scrypt(password.encode(), salt=self.salt, n=2**14, r=8, p=5,
                           maxmem=64 * 1024**2)
        return compare_digest(candidate, self.digest)


class ConfigActuator(Protocol):
    def apply(self, config: Config) -> None: ...
    def verify(self, config: Config) -> "ActuatorReadback": ...


@dataclass(frozen=True)
class ActuatorReadback:
    """Numeric post-write evidence; no field is inferred from a setter return.

    A qualified adapter must independently obtain fresh values. This type
    checks consistency only; it cannot make an unqualified reader trustworthy.
    """

    applied_config: Config
    gpu_accepted_max_mhz: int
    gpu_measured_mhz: int
    cpu_fast_accepted_max_mhz: int
    cpu_slow_accepted_max_mhz: int
    fan_min_state: int
    observed_monotonic_s: float
    # (E0, P0, E1, P1) accepted maxima; checked against the operator's
    # per-cluster maxima when supplied.
    cpu_cluster_accepted_max_mhz: tuple | None = None

    def _clusters_match(self, requested: Config) -> bool:
        values = self.cpu_cluster_accepted_max_mhz
        if values is None:
            return True
        return (type(values) is tuple and len(values) == 4
                and all(type(v) is int and 0 < v <= m
                        for v, m in zip(values, requested.cpu_cluster_maxima())))

    def matches(self, requested: Config) -> bool:
        return (type(requested) is Config
                and self._clusters_match(requested)
                and type(self.applied_config) is Config
                and self.applied_config == requested
                and type(self.gpu_accepted_max_mhz) is int
                and 0 < self.gpu_accepted_max_mhz <= requested.gpu_max_mhz <= GPU_HARD_MAX_MHZ
                and type(self.gpu_measured_mhz) is int
                and 0 <= self.gpu_measured_mhz <= self.gpu_accepted_max_mhz
                and type(self.cpu_fast_accepted_max_mhz) is int
                and 0 < self.cpu_fast_accepted_max_mhz <= requested.cpu_fast_max_mhz
                and type(self.cpu_slow_accepted_max_mhz) is int
                and 0 < self.cpu_slow_accepted_max_mhz <= requested.cpu_slow_max_mhz
                and type(self.fan_min_state) is int
                and requested.fan_min_state <= self.fan_min_state <= 12
                and type(self.observed_monotonic_s) in (int, float)
                and isfinite(self.observed_monotonic_s)
                and self.observed_monotonic_s >= 0)

    def fresh_matches(self, requested: Config, now_s: float) -> bool:
        return (self.matches(requested)
                and type(now_s) in (int, float) and isfinite(now_s)
                and 0 <= now_s - self.observed_monotonic_s <= 0.5)


class DurableAudit(Protocol):
    def sync_intent(self, proposal: "Proposal", operator: str) -> None: ...
    def sync_outcome(self, proposal: "Proposal", status: str) -> None: ...


class FaultSink(Protocol):
    def trip(self, reason: str) -> object: ...


@dataclass(frozen=True)
class Proposal:
    id: str
    base_revision: int
    expires_s: float
    digest: str
    config: Config
    changes: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class CommitResult:
    status: str
    revision: int
    faulted: bool


class BrokerCore:
    """Serial, one-use proposal authorization; no hardware adapter supplied here."""

    def __init__(self, verifier: PasswordVerifier, actuator: ConfigActuator,
                 audit: DurableAudit,
                 *, api_uid: int, operator_uid: int | None = None, clock=monotonic,
                 initial_config: Config | None = None, initial_revision: int = 0,
                 fault_sink: FaultSink | None = None):
        if type(api_uid) is not int or api_uid <= 0:
            raise ValueError("dedicated non-root API UID required")
        if operator_uid is not None and (type(operator_uid) is not int
                                         or operator_uid <= 0 or operator_uid == api_uid):
            raise ValueError("operator UID must be a distinct non-root identity")
        if (type(initial_revision) is not int or initial_revision < 0
                or (initial_config is not None and type(initial_config) is not Config)
                or (initial_revision > 0 and initial_config is None)):
            raise ValueError("invalid reconciled starting state")
        self._verifier = verifier
        self._actuator = actuator
        self._audit = audit
        self._fault_sink = fault_sink
        self._api_uid = api_uid
        self._operator_uid = operator_uid
        self._clock = clock
        self._lock = RLock()
        self.config = initial_config if initial_config is not None else Config()
        self.revision = initial_revision
        self.faulted = False
        self._proposals: dict[str, Proposal] = {}
        self._tickets: dict[str, tuple[str, str, str, int, float, str, int]] = {}
        self._failed_auth: list[float] = []
        self._last_clock_s: float | None = None

    def _fault(self, reason: str):
        first_fault = not self.faulted
        self.faulted = True
        self._proposals.clear()
        self._tickets.clear()
        if first_fault and self._fault_sink is not None:
            try:
                self._fault_sink.trip(reason)
            except Exception:
                pass  # The independent guard must also monitor broker health.

    def _now(self) -> float:
        try:
            now = self._clock()
        except Exception:
            now = None
        if (type(now) not in (int, float) or not isfinite(now) or not 0 <= now <= 1e12
                or (self._last_clock_s is not None and now < self._last_clock_s)):
            self._fault("broker monotonic clock invalid or reversed")
            raise RuntimeError("broker monotonic clock invalid or reversed")
        self._last_clock_s = now
        return now

    def _purge_expired(self, now: float):
        self._proposals = {key: value for key, value in self._proposals.items()
                           if value.expires_s > now and value.base_revision == self.revision}
        self._tickets = {key: value for key, value in self._tickets.items()
                         if value[4] > now and value[6] == self.revision
                         and value[0] in self._proposals}

    def _peer(self, peer_uid: int):
        if type(peer_uid) is not int or peer_uid not in (self._api_uid, self._operator_uid):
            raise PermissionError("unauthorized local peer")

    def propose(self, changes: dict, *, base_revision: int, peer_uid: int):
        with self._lock:
            self._peer(peer_uid)
            now = self._now()
            self._purge_expired(now)
            if self.faulted or base_revision != self.revision:
                raise RuntimeError("broker fault or stale revision")
            if len(self._proposals) >= MAX_PENDING:
                raise RuntimeError("too many pending proposals")
            if (type(changes) is not dict or not changes or len(changes) > len(_FIELDS)
                    or set(changes) - _FIELDS):
                raise ValueError("unknown or empty configuration change")
            normalized = dict(changes)
            if "tuning" in normalized:
                normalized["tuning"] = normalize_tuning(normalized["tuning"])
            if "fan_curve" in normalized:
                curve = normalized["fan_curve"]
                if (not isinstance(curve, (list, tuple)) or len(curve) > 12
                        or any(not isinstance(point, (list, tuple)) for point in curve)):
                    raise ValueError("invalid fan curve")
                normalized["fan_curve"] = tuple(tuple(point) for point in curve)
            config = replace(self.config, **normalized)
            digest = config_fingerprint(config)
            proposal = Proposal(token_urlsafe(24), self.revision, now + PROPOSAL_TTL_S,
                                digest, config, tuple(sorted(normalized.items())))
            self._proposals[proposal.id] = proposal
            return proposal

    def authorize(self, proposal_id: str, password: str, *, operator: str,
                  session: str, peer_uid: int):
        with self._lock:
            self._peer(peer_uid)
            now = self._now()
            self._purge_expired(now)
            self._failed_auth = [t for t in self._failed_auth if now - t < 60]
            if len(self._failed_auth) >= 5:
                raise PermissionError("authorization throttled")
            proposal = self._proposals.get(proposal_id)
            if (proposal is None or now >= proposal.expires_s
                    or proposal.base_revision != self.revision or self.faulted
                    or not isinstance(operator, str) or not 1 <= len(operator) <= 64
                    or not isinstance(session, str) or not 1 <= len(session) <= 128):
                raise PermissionError("authorization unavailable")
            if not self._verifier.verify(password):
                self._failed_auth.append(now)
                raise PermissionError("authorization unavailable")
            if len(self._tickets) >= MAX_PENDING:
                raise PermissionError("too many pending authorizations")
            token = token_urlsafe(32)
            token_digest = sha256(token.encode()).hexdigest()
            self._tickets[token_digest] = (proposal_id, operator, session, peer_uid,
                                           now + AUTH_TTL_S, proposal.digest, proposal.base_revision)
            return token

    def commit(self, proposal_id: str, token: str, *, operator: str,
               session: str, peer_uid: int):
        with self._lock:
            self._peer(peer_uid)
            now = self._now()
            self._purge_expired(now)
            if not isinstance(token, str) or len(token) > 256:
                raise PermissionError("invalid authorization")
            token_digest = sha256(token.encode()).hexdigest()
            ticket = self._tickets.pop(token_digest, None)  # consume before any write
            proposal = self._proposals.get(proposal_id)
            if (ticket is None or proposal is None or self.faulted
                    or ticket != (proposal_id, operator, session, peer_uid,
                                  ticket[4], proposal.digest, proposal.base_revision)
                    or now >= ticket[4] or now >= proposal.expires_s
                    or proposal.base_revision != self.revision):
                raise PermissionError("invalid or stale authorization")
            # Hard limits are checked again at the final gate, independent of
            # proposal storage and of any API-side validation.
            try:
                Config(**asdict(proposal.config))
                changes = dict(proposal.changes)
                if (proposal.id != proposal_id
                        or not changes or len(changes) != len(proposal.changes)
                        or set(changes) - _FIELDS
                        or tuple(sorted(changes.items())) != proposal.changes
                        or replace(self.config, **changes) != proposal.config
                        or config_fingerprint(proposal.config) != proposal.digest):
                    raise ValueError("proposal identity or digest mismatch")
            except (TypeError, ValueError, AttributeError):
                self._fault("broker proposal envelope violated")
                raise RuntimeError("broker proposal envelope violated")
            try:
                self._audit.sync_intent(proposal, operator)
            except Exception:
                self._fault("broker durable intent failed")
                return CommitResult("failed_before_write", self.revision, True)
            try:
                self._actuator.apply(proposal.config)
                readback = self._actuator.verify(proposal.config)
                if (type(readback) is not ActuatorReadback
                        or not readback.fresh_matches(proposal.config, self._now())):
                    raise RuntimeError("actuator readback did not verify")
            except Exception:
                self._fault("broker actuator verification failed")
                try:
                    self._audit.sync_outcome(proposal, "failed_or_partial")
                except Exception:
                    pass
                return CommitResult("failed_or_partial", self.revision, True)
            try:
                self._audit.sync_outcome(proposal, "verified")
            except Exception:
                self._fault("broker durable outcome failed")
                return CommitResult("failed_or_partial", self.revision, True)
            self.config = proposal.config
            self.revision += 1
            self._proposals.clear()
            self._tickets.clear()
            return CommitResult("applied", self.revision, False)
