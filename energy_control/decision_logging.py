"""Bounded translation of shadow-policy proposals into candidate log records.

This module has no device I/O. Recorder failure must be handled by the caller's
independent abort path before any experimental increase or admission.
"""

from .policy import ProposedLimits
from .recorder import CommissioningRecorder


_NORMAL_CODES = {
    "STARTUP": "model_loading",
    "REARM": "new_prefill",
    "COOLDOWN": "idle",
    "RAMP": "busy_dwell",
    "HOLD": "busy_dwell",
    "DERATED": "thermal_headroom",
    "RUN": "maximum_reached",
}


def _abort_code(reasons: tuple[str, ...]) -> str:
    # Only a fixed classification is persisted; no sensor name, prompt,
    # exception string or other caller-controlled text reaches the log.
    joined = " ".join(reasons).lower()
    if "projected temperature" in joined or "predicted 93" in joined:
        return "projected_temperature"
    if "temperature" in joined:
        return "temperature"
    if "gpu" in joined and ("limit" in joined or "cap" in joined):
        return "gpu_limit"
    if "memory" in joined:
        return "memory"
    if "fan" in joined:
        return "fan"
    if "cpu actuator" in joined:
        return "cpu_actuator"
    if "gpu actuator" in joined:
        return "gpu_actuator"
    if "workload" in joined:
        return "workload_control"
    if any(word in joined for word in ("telemetry", "sensor", "interval", "input")):
        return "telemetry"
    return "other_fault"


def record_shadow_decision(recorder: CommissioningRecorder,
                           proposal: ProposedLimits) -> int:
    """Sync a proposal as a candidate; return its durable sequence number."""
    if not isinstance(recorder, CommissioningRecorder):
        raise TypeError("commissioning recorder required")
    if not isinstance(proposal, ProposedLimits) or proposal.hardware_qualified is not False:
        raise TypeError("unqualified shadow proposal required")
    if (type(proposal.reasons) is not tuple
            or not all(isinstance(reason, str) for reason in proposal.reasons)):
        raise ValueError("invalid proposal reasons")
    if proposal.mode == "ABORT":
        if proposal.abort_owned_loads is not True:
            raise ValueError("inconsistent abort proposal")
        code = _abort_code(proposal.reasons)
    else:
        code = _NORMAL_CODES.get(proposal.mode)
        if code is None or proposal.abort_owned_loads:
            raise ValueError("inconsistent shadow proposal")
    return recorder.write_decision(
        mode=proposal.mode, reason_code=code,
        gpu_candidate_max_mhz=proposal.gpu_max_mhz,
        cpu_fast_candidate_max_mhz=proposal.cpu_fast_max_mhz,
        cpu_slow_candidate_max_mhz=proposal.cpu_slow_max_mhz,
        fan_candidate_min_state=proposal.fan_min_state)
