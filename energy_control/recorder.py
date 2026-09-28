"""Bounded commissioning intent log; no sensors, workloads or device writes.

An intent method returns only after fdatasync. Callers must fail closed on any
exception and must never admit work or increase a cap before that return.
"""

import json
import os
from functools import wraps
from dataclasses import MISSING, asdict, dataclass, fields
from math import isfinite
from pathlib import Path
import re
import stat
from threading import RLock
from time import monotonic_ns, time_ns
from uuid import UUID, uuid4

from .trial_plan import SERVICE_STAGE, TrialProposal, validate_trial_proposal
from .gpu_evidence import GpuSetterEvidence
from .limits import GPU_HARD_MAX_MHZ


def _serialized(method):
    """Keep validation, durable append and bookkeeping in one transaction."""
    @wraps(method)
    def call(self, *args, **kwargs):
        if os.getpid() != self._owner_pid:
            raise RuntimeError("recorder belongs to a different process")
        with self._lock:
            return method(self, *args, **kwargs)
    return call


MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 4096
_INTENT_KINDS = frozenset({"admit_workload", "raise_gpu_cap", "raise_cpu_fast_cap", "raise_cpu_slow_cap"})
_PHASES = frozenset({"unclassified", "idle", "queued", "admission", "prefill",
                     "decode", "cpu_load", "cooldown", "aborting"})
_DECISION_MODES = frozenset({"STARTUP", "REARM", "COOLDOWN", "RAMP", "HOLD", "DERATED", "RUN", "ABORT"})
_DECISION_CODES = frozenset({"model_loading", "idle", "new_prefill", "busy_dwell", "sustained_load",
                             "thermal_headroom", "maximum_reached", "temperature",
                             "projected_temperature", "telemetry", "gpu_limit",
                             "fan", "memory", "cpu_actuator", "gpu_actuator",
                             "workload_control", "recorder", "ownership", "reset",
                             "other_fault"})
_RUN_ID = re.compile(r"[0-9a-f]{32}\Z")
_WORKLOAD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_META_FIELDS = frozenset({"seq", "run_id", "boot_id", "utc_ns", "mono_ns", "kind"})
_TERMINAL_FIELDS = frozenset({"guard_exit_code", "admission_closed",
                              "local_processes_terminal", "llm_requests_terminal",
                              "actuators_safe", "gpu_limit_verified",
                              "evidence_run_id", "check_mono_ns"})


def _valid_number(value, low, high):
    return value is None or (type(value) in (int, float) and isfinite(value) and low <= value <= high)


def _setter_request_valid(minimum, maximum, driver, owner):
    return (type(minimum) is int and type(maximum) is int
            and 0 <= minimum <= maximum <= GPU_HARD_MAX_MHZ and maximum > 0
            and all(type(value) is str and 1 <= len(value) <= 128
                    and all(c.isalnum() or c in "-_." for c in value)
                    for value in (driver, owner)))


def _setter_result_valid(status, exit_code):
    return ((status == "success" and type(exit_code) is int and exit_code == 0)
            or (status == "failed" and type(exit_code) is int and -255 <= exit_code <= 255
                and exit_code != 0)
            or (status == "timeout" and exit_code is None))


def _receipt_matches_log(proof, latest):
    if latest is None:
        return False
    sequence, request, result_ns = latest
    return (proof.intent_seq == sequence
            and proof.requested_min_mhz == request["minimum_mhz"]
            and proof.requested_max_mhz == request["maximum_mhz"]
            and proof.driver_epoch == request["driver_epoch"]
            and proof.owner_epoch == request["owner_epoch"]
            and proof.completed_monotonic_s == result_ns / 1e9)


@dataclass(frozen=True)
class TemperatureRecord:
    sensor: str
    celsius: float | None
    slope_c_per_s: float | None
    age_s: float | None

    def __post_init__(self):
        if (not isinstance(self.sensor, str) or not 1 <= len(self.sensor) <= 32
                or not all(c.isalnum() or c in "-_." for c in self.sensor)):
            raise ValueError("invalid sensor identity")
        if not _valid_number(self.celsius, -10, 150):
            raise ValueError("invalid temperature")
        if not _valid_number(self.slope_c_per_s, -100, 100):
            raise ValueError("invalid temperature slope")
        if not _valid_number(self.age_s, 0, 60):
            raise ValueError("invalid temperature age")


@dataclass(frozen=True)
class TelemetryRecord:
    phase: str
    temperatures: tuple[TemperatureRecord, ...]
    gpu_requested_mhz: float | None
    gpu_accepted_mhz: float | None
    gpu_measured_mhz: float | None
    cpu_fast_requested_mhz: float | None
    cpu_fast_measured_mhz: float | None
    cpu_slow_requested_mhz: float | None
    cpu_slow_measured_mhz: float | None
    fan_floor_state: int | None
    fan_rpm: tuple[int | None, int | None]
    available_memory_bytes: int | None
    cpu_util_pct: float | None
    gpu_util_pct: float | None
    queued_jobs: int | None
    active_jobs: int | None
    gpu_reported_power_w: float | None
    system_input_power_w: float | None
    gpu_application_mhz: float | None = None
    gpu_hardware_max_mhz: float | None = None
    gpu_limit_age_s: float | None = None
    gpu_clock_age_s: float | None = None
    fan_healthy: bool | None = None
    cpu_actuator_healthy: bool | None = None
    gpu_actuator_healthy: bool | None = None
    workload_control_healthy: bool | None = None
    sample_mono_ns: int | None = None  # acquisition completion, not recorder sync
    sample_utc_ns: int | None = None
    cpu_logical_count: int | None = None
    cpu_policy_count: int | None = None
    cpu_fast_hardware_min_mhz: float | None = None
    cpu_fast_hardware_max_mhz: float | None = None
    cpu_fast_cap_ratio: float | None = None
    cpu_slow_hardware_min_mhz: float | None = None
    cpu_slow_hardware_max_mhz: float | None = None
    cpu_slow_cap_ratio: float | None = None
    cpu_demand_active: bool | None = None  # owned admission state, not utilization
    cpu_work_arrival: bool | None = None  # recorded before starting new CPU work
    prefill_arrival: bool | None = None  # explicit admission even during decode
    gpu_setter_evidence: GpuSetterEvidence | None = None
    model_loading: bool | None = None  # explicit lifecycle state, not utilization

    def __post_init__(self):
        if self.phase not in _PHASES:
            raise ValueError("invalid workload phase")
        if self.gpu_setter_evidence is not None:
            proof = self.gpu_setter_evidence
            if (type(proof) is not GpuSetterEvidence or self.gpu_accepted_mhz is not None
                    or self.gpu_limit_age_s is not None
                    or not proof.matches(self.gpu_requested_mhz, proof.ownership_checked_monotonic_s,
                                         (proof.boot_id, proof.driver_epoch, proof.owner_epoch, proof.run_id))):
                raise ValueError("invalid or mislabeled GPU setter evidence")
        if (type(self.temperatures) is not tuple or not 2 <= len(self.temperatures) <= 32
                or not all(isinstance(t, TemperatureRecord) for t in self.temperatures)):
            raise ValueError("invalid temperature collection")
        names = {temperature.sensor.lower() for temperature in self.temperatures}
        if (len(names) != len(self.temperatures) or "gpu" not in names
                or not any(name.startswith(("acpi", "cpu")) for name in names)):
            raise ValueError("missing or duplicate critical temperature")
        for field_name, high in (("gpu_requested_mhz", GPU_HARD_MAX_MHZ), ("gpu_accepted_mhz", GPU_HARD_MAX_MHZ),
                                 ("gpu_measured_mhz", 4000),
                                 ("gpu_application_mhz", 4000),
                                 ("gpu_hardware_max_mhz", 4000),
                                 ("cpu_fast_requested_mhz", 3900),
                                 ("cpu_fast_measured_mhz", 5000), ("cpu_slow_requested_mhz", 2808),
                                 ("cpu_slow_measured_mhz", 4000)):
            if not _valid_number(getattr(self, field_name), 0, high):
                raise ValueError(f"invalid {field_name}")
        if self.fan_floor_state is not None and (type(self.fan_floor_state) is not int
                                                  or not 0 <= self.fan_floor_state <= 12):
            raise ValueError("invalid fan floor")
        if (type(self.fan_rpm) is not tuple or len(self.fan_rpm) != 2
                or any(rpm is not None and (type(rpm) is not int or not 0 <= rpm <= 100000)
                       for rpm in self.fan_rpm)):
            raise ValueError("invalid fan RPM")
        if (self.available_memory_bytes is not None
                and (type(self.available_memory_bytes) is not int or self.available_memory_bytes < 0)):
            raise ValueError("invalid memory")
        for field_name in ("cpu_util_pct", "gpu_util_pct"):
            if not _valid_number(getattr(self, field_name), 0, 100):
                raise ValueError(f"invalid {field_name}")
        for field_name in ("queued_jobs", "active_jobs"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or not 0 <= value <= 100000):
                raise ValueError(f"invalid {field_name}")
        for field_name in ("gpu_reported_power_w", "system_input_power_w"):
            if not _valid_number(getattr(self, field_name), 0, 10000):
                raise ValueError(f"invalid {field_name}")
        if not _valid_number(self.gpu_limit_age_s, 0, 60):
            raise ValueError("invalid GPU limit verification age")
        if not _valid_number(self.gpu_clock_age_s, 0, 60):
            raise ValueError("invalid GPU clock observation age")
        for field_name in ("fan_healthy", "cpu_actuator_healthy",
                           "gpu_actuator_healthy", "workload_control_healthy",
                           "cpu_demand_active", "cpu_work_arrival", "prefill_arrival", "model_loading"):
            if getattr(self, field_name) is not None and type(getattr(self, field_name)) is not bool:
                raise ValueError(f"invalid {field_name}")
        if self.cpu_work_arrival is True and self.cpu_demand_active is not True:
            raise ValueError("CPU arrival requires known active demand")
        if self.model_loading is True and self.cpu_demand_active is not True:
            raise ValueError("model loading requires known active CPU demand")
        for field_name in ("sample_mono_ns", "sample_utc_ns"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"invalid {field_name}")
        for field_name in ("cpu_logical_count", "cpu_policy_count"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or not 1 <= value <= 4096):
                raise ValueError(f"invalid {field_name}")
        for field_name, high in (("cpu_fast_hardware_min_mhz", 3900),
                                 ("cpu_fast_hardware_max_mhz", 3900),
                                 ("cpu_slow_hardware_min_mhz", 2808),
                                 ("cpu_slow_hardware_max_mhz", 2808)):
            if not _valid_number(getattr(self, field_name), 0, high):
                raise ValueError(f"invalid {field_name}")
        for prefix in ("cpu_fast", "cpu_slow"):
            minimum = getattr(self, prefix + "_hardware_min_mhz")
            maximum = getattr(self, prefix + "_hardware_max_mhz")
            ratio = getattr(self, prefix + "_cap_ratio")
            if ((minimum is not None and maximum is not None and minimum >= maximum)
                    or not _valid_number(ratio, 0, 1)):
                raise ValueError(f"invalid {prefix} hardware bounds or ratio")
            if ratio is not None:
                requested = getattr(self, prefix + "_requested_mhz")
                if (minimum is None or maximum is None or requested is None
                        or not minimum <= requested <= maximum
                        or abs(ratio - (requested - minimum) / (maximum - minimum)) > 0.001):
                    raise ValueError(f"inconsistent {prefix} cap ratio")


class RecorderFull(OSError):
    pass


def _sample_row_valid(row: dict) -> bool:
    """Validate persisted telemetry, including optional older-schema fields."""
    known = {field.name for field in fields(TelemetryRecord)}
    if set(row) - _META_FIELDS - known:
        return False
    try:
        values = {field.name: row[field.name] if field.name in row else field.default
                  for field in fields(TelemetryRecord)}
        if any(value is MISSING for value in values.values()):
            return False
        temperatures = values["temperatures"]
        if type(temperatures) is not list or not 2 <= len(temperatures) <= 32:
            return False
        values["temperatures"] = tuple(TemperatureRecord(**item)
                                       for item in temperatures)
        if type(values["fan_rpm"]) is not list:
            return False
        values["fan_rpm"] = tuple(values["fan_rpm"])
        if values["gpu_setter_evidence"] is not None:
            proof = GpuSetterEvidence(**values["gpu_setter_evidence"])
            if proof.run_id != row["run_id"] or proof.boot_id != row["boot_id"]:
                return False
            values["gpu_setter_evidence"] = proof
        TelemetryRecord(**values)
    except (TypeError, ValueError, KeyError):
        return False
    return True


class CommissioningRecorder:
    """Create a private, single-file run log beneath a trusted directory.

    The supplied parent must be an existing trusted directory. This class does
    not manage total historical storage or authorize hardware operations.
    """

    def __init__(self, parent: Path, *, boot_id: str, budget_bytes: int = MAX_RUN_BYTES,
                 keep_segments: int | None = None):
        self._owner_pid = os.getpid()
        self._lock = RLock()
        if type(budget_bytes) is not int or not MAX_RECORD_BYTES <= budget_bytes <= MAX_RUN_BYTES:
            raise ValueError("invalid run budget")
        # Service runs only (stage-8 plan): rotate full segments and keep the
        # newest N. Commissioning runs fail closed when full, as before.
        if keep_segments is not None and (type(keep_segments) is not int
                                          or not 2 <= keep_segments <= 64):
            raise ValueError("service segment count must be 2..64")
        self._keep_segments = keep_segments
        self._segment = 0
        try:
            self.boot_id = str(UUID(boot_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("boot_id must be a UUID") from exc
        parent = Path(parent)
        if not parent.is_absolute():
            raise ValueError("recorder parent must be absolute")
        self._parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._fd = None
        self._closed = False
        self._used = 0
        self._sequence = 0
        self._failed = False
        self._unsafe = False
        self._terminal_verified = False
        self._plan_written = False
        self._last_sample_mono_ns: int | None = None
        self.trial_proposal: TrialProposal | None = None
        self._intents: dict[int, str] = {}
        self._dispatch_intents: set[int] = set()
        self._intent_requested_mhz: dict[int, float] = {}
        self._setter_intent_ns: dict[int, int] = {}
        self._setter_requests = {}
        self._latest_setter = None
        self._budget = budget_bytes
        self.run_id = uuid4().hex
        try:
            os.mkdir(self.run_id, 0o700, dir_fd=self._parent_fd)
            self._run_fd = os.open(self.run_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=self._parent_fd)
            self._fd = os.open("events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                               0o600, dir_fd=self._run_fd)
            os.fsync(self._run_fd)
            os.fsync(self._parent_fd)
            self._append({"kind": "run_start"})
        except BaseException:
            if self._fd is not None:
                os.close(self._fd)
            if hasattr(self, "_run_fd"):
                os.close(self._run_fd)
            os.close(self._parent_fd)
            raise

    @property
    @_serialized
    def ready(self) -> bool:
        """Whether this recorder can still durably record the current run."""
        return not self._closed and not self._failed and self._fd is not None

    @property
    @_serialized
    def plan_written(self) -> bool:
        return self._plan_written

    @_serialized
    def write_trial_plan(self, proposal: TrialProposal) -> int:
        """Sync an immutable, non-authorizing proposal immediately after run start."""
        validate_trial_proposal(proposal)
        if self._sequence != 1 or self._plan_written:
            raise RuntimeError("trial plan must precede every other run event")
        sequence = self._append({"kind": "trial_plan", "proposal": asdict(proposal)})
        self._plan_written = True
        self.trial_proposal = proposal
        return sequence

    @_serialized
    def _append(self, event: dict) -> int:
        if self._closed or self._failed:
            raise RuntimeError("recorder is closed or failed")
        if self._terminal_verified and event.get("kind") != "run_clean_end":
            raise RuntimeError("terminal run cannot accept further events")
        self._sequence += 1
        row = {"seq": self._sequence, "run_id": self.run_id, "boot_id": self.boot_id,
               "utc_ns": time_ns(), "mono_ns": monotonic_ns(), **event}
        sample_mono_ns = None
        if event.get("kind") == "sample":
            sample_mono_ns = event["sample_mono_ns"]
            if sample_mono_ns is None:
                sample_mono_ns = row["mono_ns"]
            if (sample_mono_ns > row["mono_ns"]
                    or (self._last_sample_mono_ns is not None
                        and sample_mono_ns <= self._last_sample_mono_ns)):
                self._sequence -= 1
                self._unsafe = True
                raise ValueError("sample acquisition time is future or not increasing")
        payload = (json.dumps(row, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > MAX_RECORD_BYTES:
            self._sequence -= 1
            raise ValueError("record too large")
        if self._used + len(payload) > self._budget:
            if not self._can_rotate():
                self._sequence -= 1
                self._failed = True
                raise RecorderFull("commissioning run budget exhausted")
            try:
                self._rotate()
            except OSError:
                self._sequence -= 1
                self._failed = True
                raise
        try:
            offset = 0
            while offset < len(payload):
                n = os.write(self._fd, payload[offset:])
                if n <= 0:
                    raise OSError("short recorder write")
                offset += n
            self._used += len(payload)
            os.fdatasync(self._fd)
        except OSError:
            self._failed = True
            raise
        if sample_mono_ns is not None:
            self._last_sample_mono_ns = sample_mono_ns
        self._last_record_mono_ns = row["mono_ns"]
        return self._sequence

    @_serialized
    def write_event(self, kind: str) -> int:
        # Run boundaries are owned by construction/close, never by callers.
        if kind != "abort":
            raise ValueError("unsupported event")
        sequence = self._append({"kind": "abort"})
        self._unsafe = True
        return sequence

    @_serialized
    def write_intent(self, kind: str, *, requested_mhz: float | None = None,
                     workload_id: str | None = None) -> int:
        if kind not in _INTENT_KINDS:
            raise ValueError("unsupported intent")
        if kind == "admit_workload":
            if (requested_mhz is not None or not isinstance(workload_id, str)
                    or not 1 <= len(workload_id) <= 64
                    or not all(c.isalnum() or c in "-_" for c in workload_id)):
                raise ValueError("invalid workload intent")
            details = {"workload_id": workload_id}
        else:
            maximum = {"raise_gpu_cap": GPU_HARD_MAX_MHZ, "raise_cpu_fast_cap": 3900,
                       "raise_cpu_slow_cap": 2808}[kind]
            if (workload_id is not None or type(requested_mhz) not in (int, float)
                    or not isfinite(requested_mhz) or not 0 < requested_mhz <= maximum):
                raise ValueError("invalid frequency intent")
            details = {"requested_mhz": requested_mhz}
        sequence = self._append({"kind": "intent", "action": kind, **details})
        self._intents[sequence] = kind
        if requested_mhz is not None:
            self._intent_requested_mhz[sequence] = requested_mhz
        return sequence

    @_serialized
    def write_gpu_setter_intent(self, *, minimum_mhz: int, maximum_mhz: int,
                                driver_epoch: str, owner_epoch: str) -> int:
        if not _setter_request_valid(minimum_mhz, maximum_mhz, driver_epoch, owner_epoch):
            raise ValueError("invalid GPU setter request")
        proposal = self.trial_proposal
        if (not self._plan_written or proposal is None or proposal.stage == 0
                or maximum_mhz > proposal.gpu_max_mhz):
            raise ValueError("GPU setter requires a matching durable trial envelope")
        sequence = self._append({"kind": "gpu_setter_intent", "minimum_mhz": minimum_mhz,
                                 "maximum_mhz": maximum_mhz, "driver_epoch": driver_epoch,
                                 "owner_epoch": owner_epoch})
        self._intents[sequence] = "gpu_setter"
        self._setter_intent_ns[sequence] = self._last_record_mono_ns
        self._setter_requests[sequence] = dict(minimum_mhz=minimum_mhz, maximum_mhz=maximum_mhz,
                                               driver_epoch=driver_epoch, owner_epoch=owner_epoch)
        self._latest_setter = None
        return sequence

    @_serialized
    def write_gpu_setter_outcome(self, intent_seq: int, *, status: str,
                                 exit_code: int | None, result_mono_ns: int) -> int:
        if (type(intent_seq) is not int or self._intents.get(intent_seq) != "gpu_setter"
                or not _setter_result_valid(status, exit_code)
                or type(result_mono_ns) is not int
                or not self._setter_intent_ns[intent_seq] <= result_mono_ns <= monotonic_ns()):
            raise ValueError("invalid GPU setter outcome")
        sequence = self._append({"kind": "gpu_setter_outcome", "intent_seq": intent_seq,
                                 "status": status, "exit_code": exit_code,
                                 "result_mono_ns": result_mono_ns})
        del self._intents[intent_seq]
        del self._setter_intent_ns[intent_seq]
        request = self._setter_requests.pop(intent_seq)
        if status == "success":
            self._latest_setter = (intent_seq, request, result_mono_ns)
        if status != "success":
            self._unsafe = True
        return sequence

    @_serialized
    def write_dispatch_intent(self, intent_seq: int) -> int:
        """Sync an impending start; this does not prove upstream execution."""
        if (type(intent_seq) is not int
                or self._intents.get(intent_seq) != "admit_workload"
                or intent_seq in self._dispatch_intents):
            raise ValueError("dispatch requires an undispatched pending admission")
        sequence = self._append({"kind": "dispatch_intent", "intent_seq": intent_seq})
        self._dispatch_intents.add(intent_seq)
        return sequence

    @_serialized
    def write_outcome(self, intent_seq: int, *, accepted_mhz: float | None = None,
                      measured_mhz: float | None = None, verified: bool):
        action = self._intents.get(intent_seq)
        if action is None or action == "gpu_setter":
            raise ValueError("outcome has no durable intent in this run")
        maximum = {"raise_gpu_cap": GPU_HARD_MAX_MHZ, "raise_cpu_fast_cap": 3900,
                   "raise_cpu_slow_cap": 2808}.get(action)
        if maximum is None:
            if accepted_mhz is not None or measured_mhz is not None:
                raise ValueError("workload admission has no MHz outcome")
        elif not _valid_number(accepted_mhz, 0, maximum) or not _valid_number(measured_mhz, 0, 5000):
            raise ValueError("invalid clock outcome")
        if type(verified) is not bool or (verified and maximum is not None and accepted_mhz is None):
            raise ValueError("invalid verification state")
        if (verified and maximum is not None
                and not 0 < accepted_mhz <= self._intent_requested_mhz[intent_seq]):
            raise ValueError("verified accepted clock exceeds requested clock")
        sequence = self._append({"kind": "outcome", "intent_seq": intent_seq,
                                 "action": action, "accepted_mhz": accepted_mhz,
                                 "measured_mhz": measured_mhz, "verified": verified})
        del self._intents[intent_seq]
        self._dispatch_intents.discard(intent_seq)
        self._intent_requested_mhz.pop(intent_seq, None)
        if not verified:
            self._unsafe = True
        return sequence

    @_serialized
    def write_sample(self, sample: TelemetryRecord):
        if not isinstance(sample, TelemetryRecord):
            raise ValueError("expected TelemetryRecord")
        proof = sample.gpu_setter_evidence
        if proof is not None and (proof.run_id != self.run_id or proof.boot_id != self.boot_id
                                  or not _receipt_matches_log(proof, self._latest_setter)
                                  or self._setter_intent_ns):
            self._unsafe = True
            raise ValueError("GPU setter evidence lacks matching completed command")
        return self._append({"kind": "sample", **asdict(sample)})

    @_serialized
    def write_decision(self, *, mode: str, reason_code: str,
                       gpu_candidate_max_mhz: int, cpu_fast_candidate_max_mhz: int,
                       cpu_slow_candidate_max_mhz: int, fan_candidate_min_state: int):
        """Sync a bounded candidate decision; never claim a device applied it.

        Fixed codes keep prompts, credentials and free-form exception text out
        of the crash record. Actuator outcomes require separate intent/readback.
        """
        if mode not in _DECISION_MODES or reason_code not in _DECISION_CODES:
            raise ValueError("invalid decision mode or reason code")
        for name, value, maximum in (
            ("gpu_candidate_max_mhz", gpu_candidate_max_mhz, GPU_HARD_MAX_MHZ),
            ("cpu_fast_candidate_max_mhz", cpu_fast_candidate_max_mhz, 3900),
            ("cpu_slow_candidate_max_mhz", cpu_slow_candidate_max_mhz, 2808),
        ):
            if type(value) is not int or not 0 < value <= maximum:
                raise ValueError(f"invalid {name}")
        if (type(fan_candidate_min_state) is not int
                or not 0 <= fan_candidate_min_state <= 12):
            raise ValueError("invalid fan candidate floor")
        sequence = self._append({"kind": "decision", "scope": "candidate", "mode": mode,
                                 "reason_code": reason_code,
                                 "gpu_candidate_max_mhz": gpu_candidate_max_mhz,
                                 "cpu_fast_candidate_max_mhz": cpu_fast_candidate_max_mhz,
                                 "cpu_slow_candidate_max_mhz": cpu_slow_candidate_max_mhz,
                                 "fan_candidate_min_state": fan_candidate_min_state})
        if mode == "ABORT":
            self._unsafe = True
        return sequence

    @_serialized
    def write_terminal_verified(self, *, guard_exit_code: int,
                                admission_closed: bool,
                                local_processes_terminal: bool,
                                llm_requests_terminal: bool,
                                actuators_safe: bool,
                                gpu_limit_verified: bool,
                                evidence_run_id: str,
                                check_mono_ns: int) -> int:
        """Sync declared finalization evidence; qualified adapters must supply it."""
        values = (admission_closed, local_processes_terminal,
                  llm_requests_terminal, actuators_safe, gpu_limit_verified)
        if (type(guard_exit_code) is not int or guard_exit_code != 0
                or any(value is not True for value in values)
                or evidence_run_id != self.run_id
                or type(check_mono_ns) is not int or check_mono_ns <= 0
                or self._unsafe or self._intents or self._terminal_verified):
            raise RuntimeError("run terminal state not verified")
        sequence = self._append({"kind": "terminal_verified",
                                 "guard_exit_code": guard_exit_code,
                                 "admission_closed": admission_closed,
                                 "local_processes_terminal": local_processes_terminal,
                                 "llm_requests_terminal": llm_requests_terminal,
                                 "actuators_safe": actuators_safe,
                                 "gpu_limit_verified": gpu_limit_verified,
                                 "evidence_run_id": evidence_run_id,
                                 "check_mono_ns": check_mono_ns})
        self._terminal_verified = True
        return sequence

    def _can_rotate(self):
        proposal = self.trial_proposal
        return (self._keep_segments is not None and self._plan_written
                and proposal is not None and proposal.stage == SERVICE_STAGE)

    def _rotate(self):
        """Close the full segment under a new name and start a fresh one."""
        os.fdatasync(self._fd)
        os.close(self._fd)
        self._fd = None
        self._segment += 1
        os.rename("events.jsonl", f"events.{self._segment:06d}.jsonl",
                  src_dir_fd=self._run_fd, dst_dir_fd=self._run_fd)
        self._fd = os.open("events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                           0o600, dir_fd=self._run_fd)
        self._used = 0
        expired = self._segment - self._keep_segments + 1
        if expired >= 1:
            try:
                os.unlink(f"events.{expired:06d}.jsonl", dir_fd=self._run_fd)
            except FileNotFoundError:
                pass
        os.fsync(self._run_fd)

    @property
    def segment(self) -> int:
        return self._segment

    @_serialized
    def close(self, *, clean: bool = False):
        if self._closed:
            if clean:
                raise RuntimeError("closed run cannot be marked clean")
            return
        try:
            if clean and (self._unsafe or self._intents or self._failed
                          or not self._terminal_verified):
                raise RuntimeError("failed, aborted or pending run cannot close cleanly")
            if clean:
                self._append({"kind": "run_clean_end"})
        finally:
            self._closed = True
            if self._fd is not None:
                os.close(self._fd)
            os.close(self._run_fd)
            os.close(self._parent_fd)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # A normal scope exit is not evidence of verified workload quiescence.
        self.close()


def inspect_run(path: Path, *, next_boot_utc_ns: int | None = None):
    """Read a bounded valid prefix; never treat a lone intent as applied.

    A possible-stop interval is only a bound between the last durable record
    and an independently supplied next-boot observation, not an exact crash time.
    """
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError("run event source is not a regular file")
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(MAX_RUN_BYTES + 1)
    if len(raw) > MAX_RUN_BYTES:
        raise ValueError("run exceeds inspection budget")
    incomplete_tail = bool(raw and not raw.endswith(b"\n"))
    lines = raw.split(b"\n")
    records = []
    corrupt_record = False
    run_id = boot_id = None
    last_mono_ns = None
    for line in lines[:-1]:
        if not line or len(line) + 1 > MAX_RECORD_BYTES:
            corrupt_record = True
            break
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            corrupt_record = True
            break
        if (not isinstance(record, dict) or type(record.get("seq")) is not int
                or record["seq"] != len(records) + 1
                or type(record.get("utc_ns")) is not int
                or record["utc_ns"] < 0
                or type(record.get("mono_ns")) is not int
                or record["mono_ns"] < 0
                or (last_mono_ns is not None and record["mono_ns"] < last_mono_ns)):
            corrupt_record = True
            break
        if not records:
            run_id, boot_id = record.get("run_id"), record.get("boot_id")
            try:
                valid_boot = isinstance(boot_id, str) and str(UUID(boot_id)) == boot_id
            except (TypeError, ValueError, AttributeError):
                valid_boot = False
            if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)
                    or not valid_boot):
                corrupt_record = True
                break
        elif record.get("run_id") != run_id or record.get("boot_id") != boot_id:
            corrupt_record = True
            break
        last_mono_ns = record["mono_ns"]
        records.append(record)
    parsed_records = records
    records = []  # Return only the semantically valid durable prefix.
    pending = {}
    dispatched = set()
    pending_requested_mhz = {}
    setter_intent_ns = {}
    setter_requests = {}
    latest_setter = None
    trial_proposal = None
    failed_or_aborted = False
    terminal_verified = False
    plan_seen = False
    terminal_record_mono_ns = None
    last_sample_mono_ns = None
    for record in parsed_records:
        kind = record.get("kind")
        if kind not in {"run_start", "trial_plan", "run_clean_end", "terminal_verified",
                        "abort", "sample", "decision",
                        "intent", "dispatch_intent", "outcome",
                        "gpu_setter_intent", "gpu_setter_outcome"}:
            corrupt_record = True
            break
        if terminal_verified and kind != "run_clean_end":
            corrupt_record = True
            break
        if not records and kind != "run_start":
            corrupt_record = True
            break
        if kind == "run_start" and record is not parsed_records[0]:
            corrupt_record = True
            break
        if kind == "trial_plan":
            if (plan_seen or len(records) != 1
                    or set(record) != _META_FIELDS | {"proposal"}
                    or type(record.get("proposal")) is not dict):
                corrupt_record = True
                break
            try:
                proposal = record["proposal"]
                if set(proposal) != {field.name for field in fields(TrialProposal)}:
                    raise ValueError("unexpected trial-plan fields")
                validate_trial_proposal(TrialProposal(**proposal))
                trial_proposal = TrialProposal(**proposal)
            except (TypeError, ValueError):
                corrupt_record = True
                break
            plan_seen = True
        if kind == "run_clean_end" and record is not parsed_records[-1]:
            corrupt_record = True
            break
        if kind == "run_clean_end" and not terminal_verified:
            corrupt_record = True
            break
        if (kind == "run_clean_end" and terminal_record_mono_ns is not None
                and not 0 <= record["mono_ns"] - terminal_record_mono_ns <= 2_000_000_000):
            corrupt_record = True
            break
        if kind == "terminal_verified":
            if (set(record) != _META_FIELDS | _TERMINAL_FIELDS
                    or record.get("guard_exit_code") != 0
                    or type(record.get("guard_exit_code")) is not int
                    or record.get("evidence_run_id") != run_id
                    or type(record.get("check_mono_ns")) is not int
                    or not 0 <= record["mono_ns"] - record["check_mono_ns"] <= 1_000_000_000
                    or any(record.get(name) is not True for name in
                           _TERMINAL_FIELDS - {"guard_exit_code", "evidence_run_id",
                                               "check_mono_ns"})
                    or pending or failed_or_aborted):
                corrupt_record = True
                break
            terminal_verified = True
            terminal_record_mono_ns = record["mono_ns"]
        if kind in {"run_start", "run_clean_end", "abort"} and set(record) != _META_FIELDS:
            corrupt_record = True
            break
        if kind == "sample":
            if not _sample_row_valid(record):
                corrupt_record = True
                break
            if record.get("gpu_setter_evidence") is not None:
                proof = GpuSetterEvidence(**record["gpu_setter_evidence"])
                if setter_intent_ns or not _receipt_matches_log(proof, latest_setter):
                    corrupt_record = True
                    break
            sample_mono_ns = record.get("sample_mono_ns")
            if sample_mono_ns is None:
                sample_mono_ns = record["mono_ns"]
            if (sample_mono_ns > record["mono_ns"]
                    or (last_sample_mono_ns is not None
                        and sample_mono_ns <= last_sample_mono_ns)):
                corrupt_record = True
                break
            last_sample_mono_ns = sample_mono_ns
        if kind == "decision" and (set(record) != _META_FIELDS | {
                                       "scope", "mode", "reason_code",
                                       "gpu_candidate_max_mhz", "cpu_fast_candidate_max_mhz",
                                       "cpu_slow_candidate_max_mhz", "fan_candidate_min_state"}
                                   or record.get("scope") != "candidate"
                                   or record.get("mode") not in _DECISION_MODES
                                   or record.get("reason_code") not in _DECISION_CODES
                                   or any(type(record.get(name)) is not int
                                          or not 0 < record[name] <= maximum
                                          for name, maximum in (
                                              ("gpu_candidate_max_mhz", GPU_HARD_MAX_MHZ),
                                              ("cpu_fast_candidate_max_mhz", 3900),
                                              ("cpu_slow_candidate_max_mhz", 2808)))
                                   or type(record.get("fan_candidate_min_state")) is not int
                                   or not 0 <= record["fan_candidate_min_state"] <= 12):
            corrupt_record = True
            break
        if kind == "gpu_setter_intent":
            if (set(record) != _META_FIELDS | {"minimum_mhz", "maximum_mhz", "driver_epoch", "owner_epoch"}
                    or not _setter_request_valid(record.get("minimum_mhz"), record.get("maximum_mhz"),
                                                 record.get("driver_epoch"), record.get("owner_epoch"))
                    or trial_proposal is None or trial_proposal.stage == 0
                    or record["maximum_mhz"] > trial_proposal.gpu_max_mhz):
                corrupt_record = True
                break
            pending[record["seq"]] = "gpu_setter"
            setter_intent_ns[record["seq"]] = record["mono_ns"]
            setter_requests[record["seq"]] = record
            latest_setter = None
        elif kind == "gpu_setter_outcome":
            intent_seq = record.get("intent_seq")
            if (set(record) != _META_FIELDS | {"intent_seq", "status", "exit_code", "result_mono_ns"}
                    or type(intent_seq) is not int or pending.get(intent_seq) != "gpu_setter"
                    or not _setter_result_valid(record.get("status"), record.get("exit_code"))
                    or type(record.get("result_mono_ns")) is not int
                    or not setter_intent_ns[intent_seq] <= record["result_mono_ns"] <= record["mono_ns"]):
                corrupt_record = True
                break
            del pending[intent_seq]
            del setter_intent_ns[intent_seq]
            request = setter_requests.pop(intent_seq)
            if record["status"] == "success":
                latest_setter = (intent_seq, request, record["result_mono_ns"])
            if record["status"] != "success":
                failed_or_aborted = True
        elif kind == "intent":
            action = record.get("action")
            if action not in _INTENT_KINDS:
                corrupt_record = True
                break
            if action == "admit_workload":
                workload_id = record.get("workload_id")
                valid_intent = (set(record) == _META_FIELDS | {"action", "workload_id"}
                                and isinstance(workload_id, str)
                                and bool(_WORKLOAD_ID.fullmatch(workload_id)))
            else:
                maximum = {"raise_gpu_cap": GPU_HARD_MAX_MHZ, "raise_cpu_fast_cap": 3900,
                           "raise_cpu_slow_cap": 2808}[action]
                requested = record.get("requested_mhz")
                valid_intent = (set(record) == _META_FIELDS | {"action", "requested_mhz"}
                                and type(requested) in (int, float) and isfinite(requested)
                                and 0 < requested <= maximum)
            if not valid_intent:
                corrupt_record = True
                break
            pending[record["seq"]] = action
            if action != "admit_workload":
                pending_requested_mhz[record["seq"]] = record["requested_mhz"]
        elif kind == "dispatch_intent":
            intent_seq = record.get("intent_seq")
            if (set(record) != _META_FIELDS | {"intent_seq"}
                    or type(intent_seq) is not int
                    or pending.get(intent_seq) != "admit_workload"
                    or intent_seq in dispatched):
                corrupt_record = True
                break
            dispatched.add(intent_seq)
        elif kind == "outcome":
            intent_seq = record.get("intent_seq")
            if (type(intent_seq) is not int or intent_seq not in pending
                    or pending.get(intent_seq) == "gpu_setter"
                    or record.get("action") != pending[intent_seq]
                    or type(record.get("verified")) is not bool
                    or set(record) != _META_FIELDS | {"intent_seq", "action",
                                                       "accepted_mhz", "measured_mhz",
                                                       "verified"}):
                corrupt_record = True
                break
            action = pending[intent_seq]
            maximum = {"raise_gpu_cap": GPU_HARD_MAX_MHZ, "raise_cpu_fast_cap": 3900,
                       "raise_cpu_slow_cap": 2808}.get(action)
            accepted = record["accepted_mhz"]
            measured = record["measured_mhz"]
            if ((maximum is None and (accepted is not None or measured is not None))
                    or (maximum is not None and
                        (not _valid_number(accepted, 0, maximum)
                         or not _valid_number(measured, 0, 5000)
                         or (record["verified"] and
                             (accepted is None or not 0 < accepted <= pending_requested_mhz[intent_seq]))))):
                corrupt_record = True
                break
            del pending[intent_seq]
            dispatched.discard(intent_seq)
            pending_requested_mhz.pop(intent_seq, None)
        if (kind == "abort" or
                (kind == "decision" and record["mode"] == "ABORT") or
                (kind == "outcome" and record["verified"] is not True)):
            failed_or_aborted = True
        records.append(record)
    clean = bool(records and records[0].get("kind") == "run_start"
                 and records[-1].get("kind") == "run_clean_end"
                 and terminal_verified
                 and not incomplete_tail and not corrupt_record and not pending
                 and not failed_or_aborted)
    if next_boot_utc_ns is not None and (type(next_boot_utc_ns) is not int or next_boot_utc_ns < 0):
        raise ValueError("invalid next-boot timestamp")
    last_utc = records[-1]["utc_ns"] if records else None
    interval = None
    if not clean and last_utc is not None and next_boot_utc_ns is not None:
        if next_boot_utc_ns < last_utc:
            raise ValueError("next boot precedes last durable record")
        interval = (last_utc, next_boot_utc_ns)
    return {"records": records, "incomplete_tail": incomplete_tail,
            "corrupt_record": corrupt_record, "clean_end": clean,
            "terminal_verified": terminal_verified, "trial_plan_present": plan_seen,
            "failed_or_aborted": failed_or_aborted,
            "pending_intents": pending, "last_durable_utc_ns": last_utc,
            "possible_stop_interval_utc_ns": interval}
