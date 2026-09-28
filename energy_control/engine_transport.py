"""Run-bound transport bridge for a future authenticated engine-side client.

No HTTP fallback and no live client supplied. Client methods must enforce their
timeout on all I/O, serialize fences at the engine, and authenticate the source.
The independent guard needs its own client/connection, not this worker's state.
"""

from math import isfinite
import re
from threading import Lock
from time import monotonic, sleep

from .terminal_receipt import TerminalReceiptVerifier


class EngineRequestTransport:
    def __init__(self, client, *, run_id, engine_epoch):
        self._client = client
        self._run_id, self._epoch = run_id, engine_epoch
        self._lock = Lock()
        self._prepared = set()
        self.verifier = TerminalReceiptVerifier(
            lambda identifier: client.read_receipt(run_id, identifier, timeout_s=0.1),
            lambda: client.read_engine_epoch(timeout_s=0.1),
            run_id=run_id, engine_epoch=engine_epoch)

    def prepare(self, request, *, workload_id):
        if type(workload_id) is not str or re.fullmatch(r"[0-9a-f]{32}", workload_id) is None:
            raise ValueError("opaque owned workload ID required")
        with self._lock:
            if workload_id in self._prepared or len(self._prepared) >= 4096:
                raise RuntimeError("duplicate or exhausted engine transport IDs")
            self._prepared.add(workload_id)  # Never reuse an ambiguous reservation.
        if self._client.read_engine_epoch(timeout_s=0.1) != self._epoch:
            raise RuntimeError("engine epoch changed before reservation")
        # reserve must not submit inference. It must preserve prior cancel
        # tombstones; an accepted reservation is not permission to start.
        if self._client.reserve(self._run_id, workload_id, self._epoch, timeout_s=0.1) is not True:
            raise RuntimeError("engine reservation unacknowledged")
        return _EngineHandle(self, workload_id, request)


class _EngineHandle:
    def __init__(self, transport, identifier, request):
        self._transport, self._identifier, self._request = transport, identifier, request
        self._lock = Lock()
        self._cancelled = False
        self._started = False

    def start(self):
        with self._lock:
            if self._cancelled:
                return
            if self._started:
                raise RuntimeError("engine request cannot start twice")
            self._started = True
            request = self._request
            self._request = None
        transport = self._transport
        # Do not hold the local lock across I/O: cancellation must race safely
        # through the server-side fence, not wait behind a blocked start call.
        if transport._client.start(transport._run_id, self._identifier, transport._epoch,
                                   request, timeout_s=0.1) is not True:
            raise RuntimeError("engine start unacknowledged or fenced")

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._request = None
        transport = self._transport
        if transport._client.cancel(transport._run_id, self._identifier, transport._epoch,
                                    timeout_s=0.1) is not True:
            raise RuntimeError("engine cancellation unacknowledged")
        # Cancellation acknowledgement is deliberately not terminal proof.

    def wait_terminal(self, timeout_s):
        if type(timeout_s) not in (int, float) or not isfinite(timeout_s) or not 0 < timeout_s <= 180:
            raise ValueError("invalid terminal wait")
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            verified = self._transport.verifier(self._identifier)
            if monotonic() > deadline:
                return False
            if verified:
                return True
            sleep(min(0.02, max(0, deadline - monotonic())))
        return False
