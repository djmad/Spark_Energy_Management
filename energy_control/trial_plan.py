"""Offline validation of proposed Lenovo qualification runs.

This module has no executor, device I/O or approval mechanism. A valid proposal
does not establish that a machine, workload transport or guard is safe to use.
"""

from dataclasses import dataclass
import re

from .broker import Config, config_fingerprint
from .limits import GPU_HARD_MAX_MHZ


@dataclass(frozen=True)
class TrialProposal:
    stage: int
    repetition: int
    duration_s: int
    cpu_cores: int
    active_llm: int
    waiting_llm: int
    gpu_max_mhz: int | None = None
    gpu_entry_mhz: int | None = None
    fan_min_state: int | None = None
    prompt_token_cap: int | None = None
    output_token_cap: int | None = None
    new_prefill_during_decode: bool = False
    baseline_active_llm: int | None = None
    admission_cap: int | None = None
    reserved_token_cap: int | None = None
    cpu_fast_max_mhz: int | None = None
    cpu_slow_max_mhz: int | None = None
    config_digest: str | None = None


# Stage 8 is the installed service (goal v2): no owned test loads, the same
# fixed actuator envelope, no trial deadline (the service guard has none).
SERVICE_STAGE = 8
_REPETITIONS = (2, 2, 2, 3, 3, 3, 10, 2, 1)
# Stage 3 (entry qualification) allows 1200 s so 4 concurrent ~20k-token prompts
# with ~10k generated tokens each can complete at capped clocks (operator direction, 26 September 2026).
_DURATION_CEILINGS = (120, 120, 30, 1800, 60, 120, 120, 600, 366 * 86400)


def _int(value: object, name: str, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")


def validate_trial_proposal(proposal: TrialProposal) -> None:
    """Reject proposals outside the *draft* stage envelope; grant no authority."""
    if not isinstance(proposal, TrialProposal):
        raise TypeError("TrialProposal required")
    _int(proposal.stage, "stage", 0, SERVICE_STAGE)
    stage = proposal.stage
    _int(proposal.repetition, "repetition", 1, _REPETITIONS[stage])
    _int(proposal.duration_s, "duration_s", 1, _DURATION_CEILINGS[stage])
    _int(proposal.cpu_cores, "cpu_cores", 0, 20)
    _int(proposal.active_llm, "active_llm", 0, 20)
    _int(proposal.waiting_llm, "waiting_llm", 0, 20)
    if type(proposal.new_prefill_during_decode) is not bool:
        raise ValueError("new_prefill_during_decode must be boolean")

    if stage == 0:
        if (proposal.cpu_cores, proposal.active_llm, proposal.waiting_llm) != (0, 0, 0):
            raise ValueError("read-only baseline adds no test load")
        if any(value is not None for value in (
                proposal.gpu_max_mhz, proposal.gpu_entry_mhz,
                proposal.fan_min_state, proposal.prompt_token_cap,
                proposal.output_token_cap, proposal.baseline_active_llm,
                proposal.admission_cap, proposal.reserved_token_cap,
                proposal.cpu_fast_max_mhz, proposal.cpu_slow_max_mhz,
                proposal.config_digest)) or proposal.new_prefill_during_decode:
            raise ValueError("read-only baseline proposes no actuator or workload settings")
        return

    _int(proposal.gpu_max_mhz, "gpu_max_mhz", 1, GPU_HARD_MAX_MHZ)
    _int(proposal.gpu_entry_mhz, "gpu_entry_mhz", 1, proposal.gpu_max_mhz)
    _int(proposal.fan_min_state, "fan_min_state", 0, 12)
    _int(proposal.cpu_fast_max_mhz, "cpu_fast_max_mhz", 1378, 3900)
    _int(proposal.cpu_slow_max_mhz, "cpu_slow_max_mhz", 338, 2808)
    if (type(proposal.config_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", proposal.config_digest) is None):
        raise ValueError("validated configuration digest required")

    allowed = {
        1: lambda p: (p.cpu_cores, p.active_llm, p.waiting_llm) == (0, 0, 0),
        2: lambda p: p.cpu_cores in (1, 2, 4) and (p.active_llm, p.waiting_llm) == (0, 0),
        # Entry qualification: 1-4 concurrent owned requests (operator direction,
        # 26 September 2026: large prompts and more than one job).
        3: lambda p: p.cpu_cores == 0 and 1 <= p.active_llm <= 4 and 0 <= p.waiting_llm <= 4,
        4: lambda p: p.cpu_cores == 4 and p.active_llm == 1 and 0 <= p.waiting_llm <= 4,
        5: lambda p: p.cpu_cores in (4, 5) and 1 <= p.active_llm <= 4 and 10 <= p.waiting_llm <= 12,
        6: lambda p: p.cpu_cores in (4, 5) and 2 <= p.active_llm <= 5 and 10 <= p.waiting_llm <= 12,
        7: lambda p: p.cpu_cores in (4, 5) and 1 <= p.active_llm <= 4 and 10 <= p.waiting_llm <= 12,
        SERVICE_STAGE: lambda p: (p.cpu_cores, p.active_llm, p.waiting_llm) == (0, 0, 0),
    }
    if not allowed[stage](proposal):
        raise ValueError("demand exceeds or contradicts this draft stage")
    if proposal.new_prefill_during_decode != (stage == 6):
        raise ValueError("new prefill is only part of stage 6")
    if stage == 6:
        _int(proposal.baseline_active_llm, "baseline_active_llm", 1, 4)
        if proposal.active_llm != proposal.baseline_active_llm + 1:
            raise ValueError("stage 6 permits exactly one additional active request")
    elif proposal.baseline_active_llm is not None:
        raise ValueError("baseline_active_llm is only part of stage 6")
    if stage <= 2 or stage == SERVICE_STAGE:
        if any(value is not None for value in (proposal.prompt_token_cap,
                                               proposal.output_token_cap,
                                               proposal.admission_cap,
                                               proposal.reserved_token_cap)):
            raise ValueError("non-LLM stage has no request budget")
    else:
        # These are only broad proposal caps; the operator must approve exact,
        # potentially much smaller bounds before a hardware run.
        _int(proposal.prompt_token_cap, "prompt_token_cap", 1, 32768)
        _int(proposal.output_token_cap, "output_token_cap", 1, 16384)
        maximum_admissions = {3: 8, 4: 5, 5: 16, 6: 17, 7: 200}[stage]
        _int(proposal.admission_cap, "admission_cap",
             proposal.active_llm + proposal.waiting_llm, maximum_admissions)
        _int(proposal.reserved_token_cap, "reserved_token_cap", 1, 1_000_000)


def validate_trial_against_config(proposal: TrialProposal, config: Config) -> None:
    """Require exact predeclared broker policy, not merely safe-looking caps."""
    validate_trial_proposal(proposal)
    if proposal.stage == 0:
        raise ValueError("read-only baseline has no committed trial policy")
    if type(config) is not Config:
        raise TypeError("trusted committed Config required")
    if (proposal.config_digest != config_fingerprint(config)
            or proposal.gpu_max_mhz != config.gpu_max_mhz
            or proposal.gpu_entry_mhz != config.gpu_entry_mhz
            or proposal.cpu_fast_max_mhz != config.cpu_fast_max_mhz
            or proposal.cpu_slow_max_mhz != config.cpu_slow_max_mhz
            or proposal.fan_min_state != config.fan_min_state):
        raise ValueError("trial proposal does not match committed policy")
