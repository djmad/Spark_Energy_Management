"""In-memory accounting for requests owned by a commissioning harness.

No network calls or prompt bodies live here. An integration must register every
request before dispatch, supply its own cancellation operation, and acknowledge
terminal status only after the upstream request has really ended. This ledger
cannot account for clients that bypass the future admission gateway.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass


class AdmissionClosed(RuntimeError):
    pass


@dataclass
class _Request:
    cancel: Callable[[], None]
    phase: str = "queued"
    cancel_sent: bool = False


class OwnedRequestGate:
    """Fail-closed ownership ledger for a single bounded commissioning run."""

    def __init__(self, *, max_requests: int = 20):
        if type(max_requests) is not int or not 0 <= max_requests <= 20:
            raise ValueError("max_requests must be 0..20")
        self.max_requests = max_requests
        self._lock = threading.RLock()
        self._state = "bootstrap"
        self._next_id = 1
        self._requests: dict[int, _Request] = {}

    def register(self, cancel: Callable[[], None]) -> int:
        if not callable(cancel):
            raise TypeError("cancel must be callable")
        with self._lock:
            if self._state != "open":
                raise AdmissionClosed("test admission is closed")
            if (sum(r.phase in ("queued", "active") for r in self._requests.values()) >= self.max_requests
                    or len(self._requests) >= 4096):
                raise AdmissionClosed("test request limit reached")
            request_id = self._next_id
            self._next_id += 1
            self._requests[request_id] = _Request(cancel)
            return request_id

    def mark_active(self, request_id: int) -> None:
        with self._lock:
            request = self._requests[request_id]
            if request.phase != "queued":
                raise ValueError("request is not queued")
            request.phase = "active"

    def mark_terminal(self, request_id: int) -> None:
        """Call only after upstream confirms completion/cancellation."""
        with self._lock:
            request = self._requests[request_id]
            request.phase = "terminal"
            del self._requests[request_id]

    def mark_client_done(self, request_id: int) -> None:
        """Client finished, server termination unverified; retain ownership."""
        with self._lock:
            self._requests[request_id].phase = "client_done"

    def unverified_client_completions(self) -> int:
        with self._lock:
            return sum(r.phase == "client_done" for r in self._requests.values())

    def accounting_counts(self) -> tuple[int, int, int]:
        """Atomic queued/active/client-done-unverified counts, not engine load."""
        with self._lock:
            return (*self.counts(), self.unverified_client_completions())

    def close_admission(self) -> None:
        with self._lock:
            self._state = "closed"

    def arm_admission(self) -> None:
        """One-shot transition; caller must first prove the full preflight."""
        with self._lock:
            if self._state != "bootstrap" or self._requests:
                raise AdmissionClosed("admission cannot be rearmed in this run")
            self._state = "open"

    def cancel_owned_requests(self) -> None:
        """Try all nonterminal requests; a failed callback can be retried."""
        with self._lock:
            pending = [(rid, request.cancel) for rid, request in self._requests.items()
                       if not request.cancel_sent]
            for rid, _ in pending:
                self._requests[rid].cancel_sent = True
        errors = []
        for rid, cancel in pending:
            try:
                cancel()
            except Exception as exc:
                with self._lock:
                    if rid in self._requests:
                        self._requests[rid].cancel_sent = False
                errors.append(f"request {rid}: {type(exc).__name__}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def verify_admission_and_requests(self) -> bool:
        with self._lock:
            return self._state != "open" and not self._requests

    def counts(self) -> tuple[int, int]:
        """Owned queued and active counts; never include prompts or IDs."""
        with self._lock:
            return (sum(r.phase == "queued" for r in self._requests.values()),
                    sum(r.phase == "active" for r in self._requests.values()))
