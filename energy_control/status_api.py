"""Unprivileged, read-only loopback HTTP API over the service's status file.

The installed service is the single hardware reader and publishes
``/run/spark-energy/status.json`` once per second (dashboards and tools must
not poll sensors themselves). This server only re-serves that file: no
hardware access, no mutation routes, no paths or commands from requests.

Routes: ``GET /healthz``, ``GET /v1/status``, ``GET /v1/limits``.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import time

STATUS_PATH = Path("/run/spark-energy/status.json")
MAX_STATUS_BYTES = 64 * 1024
STALE_AFTER_S = 5.0


def read_status(path=STATUS_PATH, *, now_ns=None):
    """Return (status, age_s) or raise ValueError/OSError."""
    with open(path, "rb") as handle:
        data = handle.read(MAX_STATUS_BYTES + 1)
    if len(data) > MAX_STATUS_BYTES:
        raise ValueError("status file too large")
    status = json.loads(data)
    if type(status) is not dict or type(status.get("utc_ns")) is not int:
        raise ValueError("invalid status document")
    now_ns = time.time_ns() if now_ns is None else now_ns
    return status, (now_ns - status["utc_ns"]) / 1e9


class Handler(BaseHTTPRequestHandler):
    server_version = "spark-energy-status/1"
    status_path = STATUS_PATH

    def _send(self, code, body):
        payload = json.dumps(body, separators=(",", ":"), allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        route = self.path.split("?", 1)[0]
        if route not in ("/healthz", "/v1/status", "/v1/limits"):
            self._send(404, {"error": "not_found"})
            return
        try:
            status, age = read_status(self.status_path)
        except (OSError, ValueError):
            self._send(503, {"ok": False, "error": "status_unavailable"})
            return
        fresh = 0 <= age <= STALE_AFTER_S
        if route == "/healthz":
            self._send(200 if fresh else 503, {"ok": fresh, "age_s": round(age, 1),
                                                "mode": status.get("mode")})
        elif route == "/v1/limits":
            self._send(200, {"age_s": round(age, 1), "stale": not fresh,
                             "limits": status.get("limits"), "control": status.get("control"),
                             "applied": {"gpu_cap_mhz": (status.get("gpu") or {}).get("cap_mhz"),
                                         "cpu_caps_mhz": (status.get("cpu") or {}).get("caps_mhz"),
                                         "fan_floor": (status.get("fan") or {}).get("floor")}})
        else:
            self._send(200, {"age_s": round(age, 1), "stale": not fresh, "status": status})

    def do_POST(self):
        self._send(405, {"error": "read_only"})

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, *args):
        pass  # No request logging (no broad logs).


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only energy_control status API")
    parser.add_argument("--port", type=int, default=18765)
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        raise SystemExit("status API must not run as root")
    if not 1024 <= args.port <= 65535:
        raise SystemExit("unprivileged port required")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
