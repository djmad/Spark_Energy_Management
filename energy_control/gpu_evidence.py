"""Typed setter evidence for the operator-approved commissioning mode.

No hardware I/O. Only the privileged harness may produce this record after a
synced intent and successful bounded command. A valid value is not independent
numeric lock readback or authentication of its producer.
"""

from dataclasses import dataclass
from math import isfinite
import re
from .limits import GPU_HARD_MAX_MHZ


def valid_context(context):
    return (type(context) is tuple and len(context) == 4
            and all(type(value) is str and 1 <= len(value) <= 128 for value in context)
            and re.fullmatch(r"[0-9a-f]{32}", context[3]) is not None)


# GPU ownership/setter evidence freshness, 1.5 s (was 0.5 s), as for the CPU
# owner (defect 30). While the driver tears down a large CUDA context (vLLM
# stop, end of a 100 GB burn-in) /proc/driver/nvidia reads block 0.5-0.7 s;
# the ownership check alone took 0.54 s and the 0.5 s window tripped the GPU
# owner at every burn-in end (27 September 2026, doc/42 defect 33). A hung
# owner is still caught within 1.5 s; the temperature limits and the
# measured-clock-above-lock check are independent of this evidence.
GPU_EVIDENCE_MAX_AGE_S = 1.5

@dataclass(frozen=True)
class GpuOwnershipReading:
    """Fresh result from a trusted local ownership monitor, not an API claim."""

    boot_id: str
    driver_epoch: str
    owner_epoch: str
    run_id: str
    observed_monotonic_s: float
    exclusive: bool

    def matches(self, context, now_s):
        return (valid_context(context)
                and (self.boot_id, self.driver_epoch, self.owner_epoch, self.run_id) == context
                and self.exclusive is True
                and all(type(value) in (int, float) and isfinite(value)
                        for value in (now_s, self.observed_monotonic_s))
                and 0 <= self.observed_monotonic_s <= now_s
                and now_s - self.observed_monotonic_s <= GPU_EVIDENCE_MAX_AGE_S)


@dataclass(frozen=True)
class GpuSetterEvidence:
    requested_min_mhz: int
    requested_max_mhz: int
    intent_seq: int
    exit_code: int
    completed_monotonic_s: float
    ownership_checked_monotonic_s: float
    boot_id: str
    driver_epoch: str
    owner_epoch: str
    run_id: str

    def matches(self, requested_max, now_s, context) -> bool:
        numeric = (now_s, self.completed_monotonic_s, self.ownership_checked_monotonic_s)
        return (valid_context(context)
                and (self.boot_id, self.driver_epoch, self.owner_epoch, self.run_id) == context
                and type(self.requested_min_mhz) is int
                and type(self.requested_max_mhz) is int
                and 0 <= self.requested_min_mhz <= self.requested_max_mhz <= GPU_HARD_MAX_MHZ
                and self.requested_max_mhz > 0
                and type(requested_max) in (int, float) and isfinite(requested_max)
                and self.requested_max_mhz == requested_max
                and type(self.intent_seq) is int and self.intent_seq > 0
                and type(self.exit_code) is int and self.exit_code == 0
                and all(type(value) in (int, float) and isfinite(value) for value in numeric)
                and 0 <= self.completed_monotonic_s <= self.ownership_checked_monotonic_s <= now_s
                and now_s - self.ownership_checked_monotonic_s <= GPU_EVIDENCE_MAX_AGE_S)
