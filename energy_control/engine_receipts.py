"""Engine-thread ledger for a future scoped vLLM acknowledgement adapter.

No network, inference, CUDA calls or installed-engine modification. Hooks must
run on the scheduler's owning thread. Removal and drain hooks are trusted
engine observations, never client parameters or HTTP-disconnect assertions.
"""

from dataclasses import dataclass
from math import isfinite
import os
import re
from threading import get_ident
from time import monotonic

from .terminal_receipt import TerminalReceipt


@dataclass
class _Entry:
    registered: bool = False
    fenced: bool = False
    submitted: bool = False
    scheduler_removed: bool = False
    drained: bool = False
    first_completed_batch: int | None = None


@dataclass(frozen=True)
class ExecutionReceipt:
    """A completed owned batch, not dispatch or complete-prefill evidence."""
    run_id: str
    workload_id: str
    engine_epoch: str
    observed_monotonic_s: float
    first_completed_batch: int


class EngineReceiptLedger:
    def __init__(self, *, run_id, engine_epoch, read_engine_epoch, capacity=256, clock=monotonic):
        if (type(run_id) is not str or re.fullmatch(r"[0-9a-f]{32}", run_id) is None
                or type(engine_epoch) is not str
                or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", engine_epoch) is None
                or not callable(read_engine_epoch) or not callable(clock)
                or type(capacity) is not int or not 1 <= capacity <= 4096):
            raise ValueError("invalid engine ledger binding")
        self._run_id, self._epoch = run_id, engine_epoch
        self._read_epoch, self._clock = read_engine_epoch, clock
        self._capacity = capacity
        self._owner = (os.getpid(), get_ident())
        self._entries = {}
        self._faulted = False
        self._last_time = None
        self._batches = {}
        self._last_batch_id = 0

    def _check(self, identifier):
        if self._owner != (os.getpid(), get_ident()):
            raise RuntimeError("engine hooks must run on their owning thread")
        if type(identifier) is not str or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise ValueError("opaque workload ID required")
        if self._faulted:
            raise RuntimeError("engine ledger faulted")
        try:
            now = self._clock()
            if (self._read_epoch() != self._epoch or type(now) not in (int, float)
                    or not isfinite(now) or now < 0
                    or (self._last_time is not None and now < self._last_time)):
                raise RuntimeError("engine identity or clock changed")
            self._last_time = now
            return now
        except Exception:
            self._faulted = True
            raise

    def _entry(self, identifier):
        if identifier not in self._entries:
            if len(self._entries) >= self._capacity:
                self._faulted = True
                raise RuntimeError("engine receipt capacity exhausted; no eviction/reuse")
            self._entries[identifier] = _Entry()
        return self._entries[identifier]

    def register(self, identifier):
        self._check(identifier)
        entry = self._entry(identifier)
        if entry.registered:
            raise RuntimeError("workload ID already registered")
        entry.registered = True
        return not entry.fenced

    def submit(self, identifier, enqueue):
        """Synchronous scheduler insertion only; mark uncertain before callback."""
        self._check(identifier)
        entry = self._entries.get(identifier)
        if entry is None or not entry.registered or not callable(enqueue):
            raise RuntimeError("registered workload and scheduler callback required")
        if entry.fenced:
            return False
        if entry.submitted:
            raise RuntimeError("duplicate scheduler submission")
        entry.submitted = True
        # Internal ID is chosen here, not supplied by a prompt/body. An enqueue
        # exception leaves ambiguous work submitted until removal/drain proof.
        enqueue(self.engine_request_id(identifier))
        return True

    def engine_request_id(self, identifier):
        self._check(identifier)
        return f"energy-{self._run_id}-{identifier}"

    def cancel(self, identifier):
        self._check(identifier)
        entry = self._entry(identifier)
        entry.fenced = True  # Tombstone also prevents a delayed first register/ADD.
        return self.engine_request_id(identifier) if entry.submitted else None

    def scheduler_removed(self, identifier):
        self._check(identifier)
        entry = self._entries.get(identifier)
        if entry is None or not entry.submitted:
            raise RuntimeError("cannot acknowledge unknown scheduler work")
        entry.fenced = True
        entry.scheduler_removed = True

    def begin_batch(self, batch_id, identifiers):
        """Record owned dependencies BEFORE executor submission on engine thread.

        The hook supplies every owned member; unrelated requests are not added
        to this ledger. Up to 64 outstanding batches are retained, with no ID
        reuse in a run. A failed/ambiguous executor call leaves its batch pending.
        """
        self._check("0" * 32)
        if (type(batch_id) is not int or not self._last_batch_id < batch_id < 2**63
                or type(identifiers) is not tuple or not 1 <= len(identifiers) <= self._capacity
                or any(type(identifier) is not str for identifier in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise ValueError("invalid or reused batch identity/membership")
        if len(self._batches) >= 64:
            self._faulted = True
            raise RuntimeError("outstanding batch capacity exhausted")
        for identifier in identifiers:
            self._check(identifier)
            entry = self._entries.get(identifier)
            if (entry is None or not entry.submitted or entry.fenced
                    or entry.scheduler_removed or entry.drained):
                raise RuntimeError("cannot execute unknown or fenced workload")
        self._batches[batch_id] = frozenset(identifiers)
        self._last_batch_id = batch_id

    def batch_completed(self, batch_id):
        """Trusted hook AFTER completion across all required executor workers.

        This must not mean just message send, scheduler output, or first rank
        response. CUDA/connector coverage remains the adapter's responsibility.
        """
        self._check("0" * 32)
        if type(batch_id) is not int or batch_id not in self._batches:
            raise RuntimeError("unknown or duplicate batch completion")
        for identifier in self._batches.pop(batch_id):
            entry = self._entries[identifier]
            if entry.first_completed_batch is None:
                entry.first_completed_batch = batch_id

    def execution_receipt(self, identifier):
        """Proof that actual owned execution completed at least one batch.

        No receipt for enqueue alone, queued work, or executor submission.
        The same trusted all-worker completion hook as drain accounting applies.
        This does not mean all prefill chunks finished or authorize a clock rise.
        """
        now = self._check(identifier)
        entry = self._entries.get(identifier)
        if (entry is None or not entry.registered or entry.fenced
                or entry.first_completed_batch is None):
            return None
        return ExecutionReceipt(self._run_id, identifier, self._epoch, now,
                                entry.first_completed_batch)

    def gpu_drained(self, identifier):
        self._check(identifier)
        entry = self._entries.get(identifier)
        if entry is None or not entry.scheduler_removed:
            raise RuntimeError("drain proof must follow scheduler removal")
        if any(identifier in members for members in self._batches.values()):
            raise RuntimeError("owned GPU batches still pending")
        entry.drained = True

    def receipt(self, identifier):
        now = self._check(identifier)
        entry = self._entries.get(identifier)
        if entry is None or not entry.registered or not entry.fenced:
            return None
        if entry.submitted and not (entry.scheduler_removed and entry.drained):
            return None
        # Re-observe immutable terminal facts while checking the same engine
        # epoch; never refresh a cached external acknowledgement blindly.
        return TerminalReceipt(self._run_id, identifier, self._epoch, now, True, True, True)
