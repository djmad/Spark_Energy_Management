"""Validate scoped upstream terminal evidence; no live vLLM adapter yet.

The trusted reader must derive these fields from engine-side evidence. A
socket close, abort-message send, or scheduler-finished flag alone cannot
supply all three completion facts. This type does not authenticate a source.
"""

from dataclasses import dataclass
from math import isfinite
import re
from time import monotonic


@dataclass(frozen=True)
class TerminalReceipt:
    run_id: str
    workload_id: str
    engine_epoch: str
    observed_monotonic_s: float
    scheduler_removed: bool
    residual_work_complete: bool
    start_fenced: bool


class TerminalReceiptVerifier:
    """Child-side adapter for a bounded independent terminal reader."""

    def __init__(self, read_receipt, read_engine_epoch, *, run_id: str,
                 engine_epoch: str, clock=monotonic):
        if (not callable(read_receipt) or not callable(read_engine_epoch)
                or not callable(clock) or type(run_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
                or type(engine_epoch) is not str
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", engine_epoch) is None):
            raise ValueError("invalid terminal receipt reader binding")
        self._read = read_receipt
        self._read_epoch = read_engine_epoch
        self._run_id = run_id
        self._epoch = engine_epoch
        self._clock = clock

    @property
    def run_id(self) -> str:
        return self._run_id

    def __call__(self, workload_id: str) -> bool:
        if (type(workload_id) is not str
                or re.fullmatch(r"[0-9a-f]{32}", workload_id) is None):
            return False
        try:
            receipt = self._read(workload_id)
            epoch = self._read_epoch()
            now = self._clock()
            return (
                type(receipt) is TerminalReceipt
                and receipt.run_id == self._run_id
                and receipt.workload_id == workload_id
                and receipt.engine_epoch == self._epoch == epoch
                and type(now) in (int, float) and isfinite(now)
                and type(receipt.observed_monotonic_s) in (int, float)
                and isfinite(receipt.observed_monotonic_s)
                and 0 <= receipt.observed_monotonic_s <= now
                and now - receipt.observed_monotonic_s <= 0.5
                and receipt.scheduler_removed is True
                and receipt.residual_work_complete is True
                and receipt.start_fenced is True)
        except Exception:
            return False
