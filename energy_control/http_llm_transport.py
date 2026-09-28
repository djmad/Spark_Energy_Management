"""Cancellable loopback vLLM streaming client; live requests opt-in only.

Local socket closure requests cancellation; it does not prove engine/GPU
quiescence. No prompt or response bodies are logged or retained after use.
"""
from http.client import HTTPConnection
import json
import re
import socket
from threading import Event, Lock, Thread, Timer
from time import monotonic


def _loopback_connection():
    return HTTPConnection("127.0.0.1", 8000, timeout=.5)


class HttpLlmTransport:
    def __init__(self, *, enable_live=False, connection_factory=None, deadline_s=180):
        if type(enable_live) is not bool:
            raise ValueError("explicit live request mode required")
        if type(deadline_s) not in (int, float) or not 1 <= deadline_s <= 1800:
            raise ValueError("request deadline must be 1..1800 s")
        self.deadline_s = deadline_s
        if connection_factory is None and not enable_live:
            raise PermissionError("live LLM requests disabled")
        self._factory = connection_factory or _loopback_connection

    def prepare(self, request, *, workload_id):
        if (type(workload_id) is not str or re.fullmatch(r"[0-9a-f]{32}", workload_id) is None
                or type(request) is not dict or not request.get("stream") is True):
            raise ValueError("owned ID and streaming request required")
        body = json.dumps(request, allow_nan=False, separators=(",", ":")).encode()
        # ~20k-token prompts are ~130 KB of JSON (operator direction, 26 Sep 2026).
        if len(body) > 1024 * 1024:
            raise ValueError("request body exceeds transport bound")
        return HttpRequest(self._factory, body, workload_id, self.deadline_s)


class HttpRequest:
    def __init__(self, factory, body, identifier, deadline_s=180):
        self._factory, self._body, self._identifier = factory, body, identifier
        self._deadline_s = deadline_s
        self._lock, self._cancelled, self._done = Lock(), Event(), Event()
        self._connection = None
        self._socket = None
        self._started = False
        self._sent = False
        self.response_completed = False
        self.error = None

    def start(self):
        with self._lock:
            if self._cancelled.is_set():
                self._body = None
                self._done.set()
                return
            if self._started:
                raise RuntimeError("request cannot start twice")
            self._started = True
        Thread(target=self._run, daemon=True, name="energy-llm-http").start()

    def _run(self):
        connection = None
        deadline_timer = None
        try:
            connection = self._factory()
            connection.connect()
            if connection.sock is not None:
                # Prefill may produce no bytes for much longer than connect
                # takes. A separate total deadline closes stalled streams.
                connection.sock.settimeout(self._deadline_s)
            # A cancellation that closes the socket must never cause request()
            # to silently reconnect and send previously cancelled work.
            connection.auto_open = 0
            with self._lock:
                self._connection = connection
                self._socket = connection.sock
                if self._cancelled.is_set():
                    return
                body, self._body = self._body, None
                self._sent = True  # Conservative even if send subsequently fails.
            deadline_timer = Timer(self._deadline_s, self.cancel)
            deadline_timer.daemon = True
            deadline_timer.start()
            connection.request("POST", "/v1/chat/completions", body=body,
                               headers={"Content-Type": "application/json",
                                        "X-Request-Id": self._identifier})
            del body
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError("LLM returned unsuccessful status")
            deadline, received = monotonic() + self._deadline_s, 0
            while not self._cancelled.is_set() and monotonic() < deadline:
                line = response.readline(65537)
                received += len(line)
                if len(line) > 65536 or received > 8 * 1024 * 1024:
                    raise RuntimeError("response exceeds transport bound")
                if line.strip() == b"data: [DONE]":
                    self.response_completed = True
                    return
                if not line:
                    raise RuntimeError("stream ended without completion marker")
            if not self._cancelled.is_set():
                raise TimeoutError("request duration exceeded")
        except Exception as exc:
            self.error = type(exc).__name__
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            self._body = None
            if connection is not None:
                connection.close()
            self._done.set()

    def cancel(self):
        self._cancelled.set()
        with self._lock:
            self._body = None
            connection = self._connection
            active_socket = self._socket
            if not self._started:
                self._done.set()
        if connection is not None:
            try:
                if active_socket is not None:
                    active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def wait_local_done(self, timeout_s):
        return self._done.wait(timeout_s)

    def wait_terminal(self, timeout_s):
        # Never turn disconnect or [DONE] into engine-drain evidence. The
        # request gateway's evidence policy must explicitly handle this client.
        return self._done.wait(timeout_s) and not self._sent
