"""Offline process-isolated heartbeat tripwire; not a commissioned guard.

The child has its own monotonic deadline and an injected abort adapter. A
controller heartbeat cannot replace independent temperature/GPU-limit reads,
and this prototype must never be used to arm live workloads. The fork-only
test seam also is not a safe way to launch a service from a multithreaded API.
"""

from math import isfinite
from multiprocessing import get_context
import os
from time import monotonic

from .abort import AbortCoordinator


def _deadline_worker(receive, send, abort: AbortCoordinator,
                     deadline_s: float, poll_s: float):
    send.close()
    deadline = monotonic() + deadline_s
    reason = "guard heartbeat missed"
    try:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            if not receive.poll(min(poll_s, remaining)):
                continue
            try:
                message = receive.recv_bytes(1)
            except EOFError:
                reason = "guard heartbeat channel closed"
                break
            except (OSError, ValueError):
                reason = "guard heartbeat channel invalid"
                break
            if message != b"H":
                reason = "guard heartbeat invalid"
                break
            deadline = monotonic() + deadline_s
    except Exception:
        reason = "guard heartbeat worker failed"
    finally:
        receive.close()
        # Never let a transport or deadline failure exit without attempting
        # all owned-workload protective actions.
        try:
            result = abort.trip(reason)
        except BaseException:
            # A supervisor must not mistake an abort-callback crash for
            # verified quiescence. This still is not a durable crash record.
            raise SystemExit(3)
    raise SystemExit(1 if result.verified_quiescent else 2)


class GuardDeadlineProcess:
    """A separate fake-test process that trips on silence, EOF or bad data."""

    def __init__(self, abort: AbortCoordinator, *, deadline_s: float = 0.5,
                 poll_s: float = 0.05):
        if (not isinstance(abort, AbortCoordinator)
                or type(deadline_s) not in (int, float) or not isfinite(deadline_s)
                or not 0.1 <= deadline_s <= 2.0
                or type(poll_s) not in (int, float) or not isfinite(poll_s)
                or not 0.01 <= poll_s <= min(0.1, deadline_s / 2)):
            raise ValueError("invalid guard process setup")
        self._context = get_context("fork")
        self._creator_pid = os.getpid()
        self._receive, self._send = self._context.Pipe(duplex=False)
        self._process = self._context.Process(
            target=_deadline_worker,
            args=(self._receive, self._send, abort, deadline_s, poll_s),
            name="energy-guard-deadline", daemon=False)
        self._started = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def exitcode(self) -> int | None:
        return self._process.exitcode

    def start(self):
        if self._started:
            raise RuntimeError("guard process already started")
        if os.getpid() != self._creator_pid:
            raise RuntimeError("guard must start in its creating process")
        self._process.start()
        self._started = True
        self._receive.close()

    def heartbeat(self):
        if not self._started or not self._process.is_alive():
            raise RuntimeError("guard process is not alive")
        self._send.send_bytes(b"H")

    def close_heartbeat(self):
        """Closing while armed is a fault, not a graceful disarm."""
        if self._started:
            self._send.close()

    def join(self, timeout_s: float = 3.0):
        if (type(timeout_s) not in (int, float) or not isfinite(timeout_s)
                or not 0 < timeout_s <= 10 or not self._started):
            raise ValueError("invalid guard join")
        self._process.join(timeout_s)
        if self._process.is_alive():
            raise RuntimeError("guard did not finish; do not assume work is safe")
