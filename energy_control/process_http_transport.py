"""Spawn-isolated HTTP clients with a shared, one-way guard cancellation latch.

Only the child opens the HTTP socket. Guard cancellation therefore does not
depend on the policy thread making progress. Socket closure still does not
prove engine drain. This transport is not a qualified hardware harness.
"""
from multiprocessing import get_context
from threading import Lock
from time import monotonic

from .http_llm_transport import HttpLlmTransport, HttpRequest


class GuardHttpCancellation:
    """Child-side guard callback: cancel this run, never claim engine drain.

    This is only the HTTP part of an abort. A full supervisor must also fence
    admission, stop owned CPU loads and request the emergency GPU limit.
    """
    def __init__(self, cancellation_event):
        self.cancellation_event = cancellation_event

    def __call__(self, owned, reason):
        self.cancellation_event.set()
        return False


def _request_worker(channel, parent_channel, factory, body, identifier,
                    cancelled, guard_cancelled, deadline_s=180):
    parent_channel.close()
    request = HttpRequest(factory, body, identifier, deadline_s)
    try:
        if cancelled.is_set() or guard_cancelled.is_set():
            request.cancel()
        request.start()
        while not request.wait_local_done(.02):
            if cancelled.is_set() or guard_cancelled.is_set() or channel.poll():
                # Parent endpoint closure also cancels; no command payloads
                # are accepted on this channel.
                request.cancel()
        channel.send((request.response_completed, request.error,
                      request.wait_terminal(0)))
    except (BrokenPipeError, EOFError, OSError):
        request.cancel()
    finally:
        channel.close()


class ProcessHttpLlmTransport(HttpLlmTransport):
    def __init__(self, *, guard_cancelled, enable_live=False, connection_factory=None,
                 deadline_s=180):
        super().__init__(enable_live=enable_live, connection_factory=connection_factory,
                         deadline_s=deadline_s)
        # A spawn-context multiprocessing Event is supplied by the supervisor
        # and shared with its independent guard. Never clear it within a run.
        self.guard_cancelled = guard_cancelled

    def prepare(self, request, *, workload_id):
        validated = super().prepare(request, workload_id=workload_id)
        return ProcessHttpRequest(validated, self.guard_cancelled)


class ProcessHttpRequest:
    def __init__(self, request, guard_cancelled):
        context = get_context("spawn")
        self._channel, child = context.Pipe()
        self._cancelled = context.Event()
        self._resources = (child, guard_cancelled)
        self._process = context.Process(
            target=_request_worker,
            args=(child, self._channel, request._factory, request._body,
                  request._identifier, self._cancelled, guard_cancelled, request._deadline_s),
            name="energy-http-request", daemon=True)
        request._body = None
        self._lock = Lock()
        self._started = self._done = self._never_sent = False
        self._received = False
        self.response_completed = False
        self.error = None

    def start(self):
        with self._lock:
            if self._started:
                raise RuntimeError("request cannot start twice")
            self._started = True
            if self._cancelled.is_set() or self._resources[1].is_set():
                self._done = self._never_sent = True
                self._channel.close()
                self._resources[0].close()
                return
            try:
                self._process.start()
            except BaseException:
                self.error = "WorkerStartFailure"
                self._done = self._never_sent = True
                self._channel.close()
                self._resources[0].close()
                raise
            self._resources[0].close()

    def cancel(self):
        self._cancelled.set()

    def wait_local_done(self, timeout_s):
        with self._lock:
            if self._done:
                return True
            if not self._started:
                if self._cancelled.is_set() or self._resources[1].is_set():
                    self._done = self._never_sent = True
                    self._channel.close()
                    self._resources[0].close()
                    return True
                return False
            deadline = monotonic() + max(0, timeout_s)
            if not self._received and self._channel.poll(max(0, deadline - monotonic())):
                try:
                    self.response_completed, self.error, self._never_sent = self._channel.recv()
                except EOFError:
                    self.error = "WorkerExitWithoutOutcome"
                self._channel.close()
                self._received = True
            if self._received:
                self._process.join(max(0, deadline - monotonic()))
                # Result delivery precedes process exit. Reap before reporting
                # local completion, so no socket-owning worker is left behind.
                self._done = not self._process.is_alive()
                return self._done
            return False

    def wait_terminal(self, timeout_s):
        return self.wait_local_done(timeout_s) and self._never_sent
