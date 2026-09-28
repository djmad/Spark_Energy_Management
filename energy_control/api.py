"""Unprivileged loopback API over in-memory telemetry and optional broker RPC.

Mutation routes are disabled unless an explicit local broker client is supplied.
No hardware command or arbitrary path is accepted from HTTP.
"""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
import os
import socket
from threading import BoundedSemaphore, RLock, Timer
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .history import GraphHistory, GraphSample
from .recorder import TelemetryRecord
from .parameter_api import MAX_HTTP_BODY_BYTES, MutationRouter
from .nvml_event_process import EventProcessStatus, clock_notification


POST_BODY_DEADLINE_S = 2.0
HTTP_CONNECTION_DEADLINE_S = 6.0


def _read_post_body(stream, connection, length: int, *, clock=monotonic) -> bytes:
    """Read one bounded HTTP body within a total, non-resetting deadline."""
    if type(length) is not int or not 1 <= length <= MAX_HTTP_BODY_BYTES:
        raise ValueError("invalid POST body length")
    deadline = clock() + POST_BODY_DEADLINE_S
    body = bytearray()
    while len(body) < length:
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("POST body deadline exceeded")
        connection.settimeout(remaining)
        chunk = stream.read1(min(4096, length - len(body)))
        if clock() > deadline:
            raise TimeoutError("POST body deadline exceeded")
        if not chunk:
            raise ValueError("incomplete POST body")
        body.extend(chunk)
    return bytes(body)


class TelemetryHub:
    def __init__(self, *, stale_after_s: float = 3.0, boot_id: str | None = None):
        if (type(stale_after_s) not in (int, float)
                or not 0 < stale_after_s <= 180):
            raise ValueError("invalid telemetry freshness threshold")
        if boot_id is not None and (not isinstance(boot_id, str) or len(boot_id) > 64):
            raise ValueError("invalid boot identity")
        self._history = GraphHistory()
        self._latest: TelemetryRecord | None = None
        self._latest_mono_s: float | None = None
        self._latest_utc_ns: int | None = None
        self._stale_after_s = stale_after_s
        self._boot_id = boot_id
        self._generation = uuid4().hex
        self._sample_id = 0
        self._lock = RLock()
        self._gpu_events = None

    def update_gpu_events(self, status: EventProcessStatus, clock_events: int):
        if (type(status) is not EventProcessStatus
                or status.state not in ("UNAVAILABLE", "WAITING", "QUIET", "CLOCK_CHANGE", "FAULT")
                or type(clock_events) is not int or not 0 <= clock_events < 2**63
                or (status.observed_monotonic_s is not None and
                    (type(status.observed_monotonic_s) not in (int, float)
                     or not isfinite(status.observed_monotonic_s) or status.observed_monotonic_s < 0))
                or (status.state in ("QUIET", "CLOCK_CHANGE") and status.observed_monotonic_s is None)
                or (status.state == "CLOCK_CHANGE" and
                    not clock_notification(status.event_type, status.event_data))
                or (status.state != "FAULT" and status.state != "CLOCK_CHANGE" and
                    (status.event_type is not None or status.event_data is not None))
                or any(value is not None and (type(value) is not int or not 0 <= value < 2**64)
                       for value in (status.event_type, status.event_data))):
            raise ValueError("invalid event-monitor diagnostic")
        with self._lock:
            self._gpu_events = (status, clock_events)

    def ingest(self, graph: GraphSample, latest: TelemetryRecord):
        if not isinstance(graph, GraphSample) or not isinstance(latest, TelemetryRecord):
            raise ValueError("typed graph and latest telemetry required")
        # The cards and state are one acquisition, not two independently
        # interchangeable observations. In particular, never show a graph
        # point next to a different clock-limit/readback state.
        if (latest.sample_mono_ns is None or latest.sample_utc_ns is None
                or abs(graph.monotonic_s * 1_000_000_000 - latest.sample_mono_ns) > 1000):
            raise ValueError("graph and state monotonic samples differ")
        if graph.utc_ns != latest.sample_utc_ns:
            raise ValueError("graph and state UTC samples differ")
        cpu_temperatures = [t.celsius for t in latest.temperatures
                            if t.sensor.lower().startswith(("acpi", "cpu"))]
        gpu_temperatures = [t.celsius for t in latest.temperatures
                            if t.sensor.lower() == "gpu"]
        if (not cpu_temperatures or len(gpu_temperatures) != 1
                or graph.cpu_temp_c != max(cpu_temperatures)
                or graph.gpu_temp_c != gpu_temperatures[0]
                or graph.cpu_util_pct != latest.cpu_util_pct
                or graph.gpu_util_pct != latest.gpu_util_pct):
            raise ValueError("graph and state values differ")
        with self._lock:
            self._history.add(graph)
            self._latest = latest
            self._latest_mono_s = graph.monotonic_s
            self._latest_utc_ns = graph.utc_ns
            self._sample_id += 1

    def _freshness(self, now_s):
        if (self._latest_mono_s is None or type(now_s) not in (int, float)
                or not isfinite(now_s) or now_s < self._latest_mono_s):
            return None, True
        age = now_s - self._latest_mono_s
        return age, age > self._stale_after_s

    def get(self, path: str, *, now_s: float | None = None):
        """Return (HTTP status, JSON body) without exposing commands or paths."""
        if not isinstance(path, str) or len(path) > 1024:
            return 400, {"error": "invalid request target"}
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return 400, {"error": "invalid request target"}
        if parsed.path == "/health/live" and not parsed.query:
            return 200, {"live": True}
        if parsed.path == "/api/v1/gpu-events" and not parsed.query:
            with self._lock:
                status, count = self._gpu_events or (EventProcessStatus("UNAVAILABLE"), 0)
                stamp = status.observed_monotonic_s
                age = (now_s - stamp if stamp is not None and type(now_s) in (int, float)
                       and isfinite(now_s) and now_s >= stamp else None)
                fresh = age is not None and age <= 0.5
                return 200, {"schema_version": 1, "boot_id": self._boot_id,
                             "monitor": asdict(status), "clock_notifications": count,
                             "age_s": age, "stale": not fresh,
                             "reader_responsive": fresh and status.state in ("QUIET", "CLOCK_CHANGE"),
                             "reset_coverage_qualified": False,
                             "clock_enforcement_verified": False}
        if parsed.path == "/health/ready" and not parsed.query:
            with self._lock:
                age, stale = self._freshness(now_s)
                return (503 if stale else 200), {"ready": not stale, "age_s": age,
                                                  "sample_id": self._sample_id}
        if parsed.path == "/api/v1/history":
            try:
                params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            except ValueError:
                return 422, {"error": "invalid query"}
            if any(len(v) != 1 for v in params.values()) or set(params) - {"view", "window", "pixels"}:
                return 422, {"error": "invalid query"}
            view = params.get("view", ["thermal-cards"])[0]
            window = params.get("window", ["15m"])[0]
            pixels_text = params.get("pixels", ["600"])[0]
            if view != "thermal-cards" or not pixels_text.isdecimal() or len(pixels_text) > 3:
                return 422, {"error": "invalid history request"}
            pixels = int(pixels_text)
            with self._lock:
                try:
                    result = self._history.query(window, pixels, now_s=now_s)
                except ValueError:
                    return 422, {"error": "invalid history request"}
                sample_id = self._sample_id
            return 200, {"schema_version": 1, "boot_id": self._boot_id,
                         "history_generation": self._generation,
                         "last_sample_id": sample_id,
                         "units": {"cpu_temp_c": "C", "gpu_temp_c": "C",
                                   "cpu_util_pct": "%", "gpu_util_pct": "%"}, **result}
        if parsed.path == "/api/v1/state" and not parsed.query:
            with self._lock:
                if self._latest is None:
                    return 503, {"error": "telemetry unavailable"}
                age, stale = self._freshness(now_s)
                return 200, {"schema_version": 1, "boot_id": self._boot_id,
                             "history_generation": self._generation,
                             "sample_id": self._sample_id,
                             "sample_utc_ns": self._latest_utc_ns,
                             "age_s": age,
                             "stale": stale,
                             "state": asdict(self._latest)}
        return 404, {"error": "not found"}


def make_handler(hub: TelemetryHub, mutations: MutationRouter | None = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format, *args):
            # Routine graph reads must not become a second disk telemetry stream.
            return

        def do_GET(self):
            status, body = hub.get(self.path, now_s=monotonic())
            self._respond(status, body)

        def do_POST(self):
            if mutations is None:
                self._respond(405, {"error": "read-only API"})
                return
            expected_host = f"127.0.0.1:{self.server.server_port}"
            if (self.headers.get_all("Host") != [expected_host]
                    or self.headers.get_all("Origin") is not None):
                self._respond(403, {"error": "untrusted origin or host"})
                return
            if self.headers.get_all("Transfer-Encoding") is not None:
                self._respond(411, {"error": "content length required"})
                return
            if self.headers.get_all("Content-Type") not in (["application/json"],
                                                            ["application/json; charset=utf-8"]):
                self._respond(415, {"error": "JSON content type required"})
                return
            lengths = self.headers.get_all("Content-Length")
            length_text = lengths[0] if lengths is not None and len(lengths) == 1 else ""
            if not length_text.isdecimal() or len(length_text) > 4:
                self._respond(411, {"error": "valid content length required"})
                return
            length = int(length_text)
            if not 1 <= length <= MAX_HTTP_BODY_BYTES:
                self._respond(413, {"error": "body too large or empty"})
                return
            try:
                body = _read_post_body(self.rfile, self.connection, length)
            except TimeoutError:
                self._respond(408, {"error": "body deadline exceeded"})
                return
            except OSError:
                self._respond(408, {"error": "body unavailable"})
                return
            except ValueError:
                self._respond(400, {"error": "incomplete body"})
                return
            status, result = mutations.handle(self.path, body)
            self._respond(status, result)

        def do_PUT(self):
            self._respond(405, {"error": "read-only API"})

        def do_DELETE(self):
            self._respond(405, {"error": "read-only API"})

        def _respond(self, status, body):
            payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except OSError:
                pass  # disconnected read-only clients cannot affect control

    return Handler


class _BoundedHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        self._slots = BoundedSemaphore(16)
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(2.0)
        return request, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        def expire():
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        watchdog = Timer(HTTP_CONNECTION_DEADLINE_S, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            watchdog.cancel()
            self._slots.release()


def create_server(hub: TelemetryHub, *, host: str = "127.0.0.1", port: int = 18765,
                  mutations: MutationRouter | None = None):
    """Bind a read-only server explicitly as a non-root user on loopback."""
    if os.geteuid() == 0:
        raise PermissionError("network API must not run as root")
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("only IPv4 loopback and a valid port are supported")
    server = _BoundedHttpServer((host, port), make_handler(hub, mutations))
    return server


def serve(hub: TelemetryHub, *, host: str = "127.0.0.1", port: int = 18765,
          mutations: MutationRouter | None = None):
    """Run explicitly; no auto-start on import."""
    server = create_server(hub, host=host, port=port, mutations=mutations)
    try:
        server.serve_forever()
    finally:
        server.server_close()
