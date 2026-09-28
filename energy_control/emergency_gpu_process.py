"""Single-use emergency executor prototype, not a live actuator owner.

Prestart before arming. The supplied callback must construct child-local
resources and enforce the same actuator ownership as the normal controller.
No hardware callback is provided here. A timed-out operation is uncertain:
never retry or permit normal writes until its outcome has been reconciled.
"""
from multiprocessing import get_context
from threading import Lock


def _worker(channel, apply):
    try:
        channel.send_bytes(b"READY")
        if channel.recv_bytes(8) != b"CAP500":
            return
        try:
            verified = apply(500) is True
        except BaseException:
            verified = False
        channel.send_bytes(b"OK" if verified else b"FAILED")
    finally:
        channel.close()


class EmergencyGpuProcess:
    def __init__(self, apply):
        if not callable(apply):
            raise ValueError("child-local emergency adapter required")
        context = get_context("spawn")
        self._parent, self._child = context.Pipe()
        self._process = context.Process(target=_worker, args=(self._child, apply), daemon=True)
        self._resource = apply
        self._lock = Lock()
        self._started = False
        self._attempted = False
        self._ready = False
        self._closed = False
        self._outcome = None

    def start(self):
        if self._started or self._closed:
            raise RuntimeError("emergency executor cannot restart")
        self._started = True
        try:
            self._process.start()
            self._child.close()
            if not self._parent.poll(2) or self._parent.recv_bytes(8) != b"READY":
                raise RuntimeError("emergency executor unavailable")
            self._ready = True
        except BaseException:
            self.close()
            raise

    def __call__(self, maximum):
        # Only one small frame is ever sent, so there is no queue to fill.
        with self._lock:
            if type(maximum) is not int or maximum != 500:
                raise ValueError("emergency ceiling is fixed at 500 MHz")
            if not self._ready or self._closed or self._attempted:
                raise RuntimeError("emergency executor unavailable or already used")
            self._attempted = True
            if not self._process.is_alive():
                raise RuntimeError("emergency executor exited")
            self._parent.send_bytes(b"CAP500")
            # A stalled driver or disk in the child cannot delay cancellation
            # in the caller beyond this observation interval.
            if not self._parent.poll(.1):
                return False
            self._outcome = self._parent.recv_bytes(8) == b"OK"
            return self._outcome

    def outcome(self):
        """Nonblocking late-result observation; never dispatches or rearms."""
        with self._lock:
            if self._outcome is not None or not self._attempted or self._closed:
                return self._outcome
            try:
                if self._parent.poll(0):
                    self._outcome = self._parent.recv_bytes(8) == b"OK"
            except (EOFError, OSError):
                self._outcome = False
            return self._outcome

    def close(self):
        """Reap prototype worker; does not prove a device command was undone."""
        self._closed = True
        self._ready = False
        self._parent.close()
        self._child.close()
        if self._process.pid is not None:
            self._process.join(.2)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(.2)
            if self._process.is_alive():
                raise RuntimeError("emergency worker termination unverified")
        self._resource = None
