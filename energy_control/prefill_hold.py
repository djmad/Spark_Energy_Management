"""Run-bound pending-execution hold; trusted engine reader, no device I/O."""
from math import isfinite
import re
from time import monotonic

from .engine_receipts import ExecutionReceipt


class PrefillHold:
    def __init__(self, read_receipt, read_epoch, *, run_id, engine_epoch, clock=monotonic):
        if (not callable(read_receipt) or not callable(read_epoch) or not callable(clock)
                or type(run_id) is not str or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
                or type(engine_epoch) is not str
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", engine_epoch) is None):
            raise ValueError("trusted run-bound execution reader required")
        self._read, self._epoch = read_receipt, read_epoch
        self._clock = clock
        self.run_id, self.engine_epoch = run_id, engine_epoch
        self._pending = set()
        self._seen = set()
        self.faulted = False

    def register(self, identifier):
        if (self.faulted or type(identifier) is not str
                or re.fullmatch(r"[0-9a-f]{32}", identifier) is None
                or identifier in self._seen or len(self._pending) >= 20
                or len(self._seen) >= 4096):
            self.faulted = True
            raise RuntimeError("invalid or exhausted prefill generation")
        self._seen.add(identifier)
        self._pending.add(identifier)

    def pending(self, now_s):
        try:
            started = self._clock()
            if (self.faulted or type(now_s) not in (int, float)
                    or not isfinite(now_s) or now_s < 0
                    or type(started) not in (int, float) or not isfinite(started)
                    or not now_s <= started <= now_s + .5
                    or self._epoch() != self.engine_epoch):
                raise RuntimeError("prefill evidence identity unavailable")
            receipts = [(identifier, self._read(identifier)) for identifier in self._pending]
            epoch = self._epoch()
            finished = self._clock()
            if (epoch != self.engine_epoch or type(finished) not in (int, float)
                    or not isfinite(finished) or not started <= finished <= started + .1
                    or finished - now_s > .5):
                raise RuntimeError("execution observation stale or too slow")
            completed = []
            for identifier, receipt in receipts:
                if receipt is None:
                    continue  # Unknown/queued: hold, never infer execution from utilization.
                if (type(receipt) is not ExecutionReceipt
                        or (receipt.run_id, receipt.workload_id, receipt.engine_epoch)
                        != (self.run_id, identifier, self.engine_epoch)
                        or type(receipt.first_completed_batch) is not int
                        or not 0 < receipt.first_completed_batch < 2**63
                        or type(receipt.observed_monotonic_s) not in (int, float)
                        or not isfinite(receipt.observed_monotonic_s)
                        or not 0 <= receipt.observed_monotonic_s <= finished
                        or finished - receipt.observed_monotonic_s > .5):
                    raise RuntimeError("invalid execution receipt")
                completed.append(identifier)
            self._pending.difference_update(completed)
            return bool(self._pending)
        except BaseException:
            self.faulted = True
            raise
