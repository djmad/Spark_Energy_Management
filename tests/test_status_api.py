"""Read-only status API: serves only the status file, never mutates."""
from http.client import HTTPConnection
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from http.server import ThreadingHTTPServer

from energy_control import status_api


class StatusApiTests(unittest.TestCase):
    def serve(self, path):
        handler = type("H", (status_api.Handler,), {"status_path": path})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def get(self, port, route, method="GET"):
        connection = HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(method, route)
        response = connection.getresponse()
        return response.status, json.loads(response.read())

    def test_fresh_status_limits_and_health(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps({"utc_ns": time.time_ns(), "mode": "RUN",
                                        "gpu": {"cap_mhz": 1800}, "cpu": {"caps_mhz": {"slow": 2808}},
                                        "fan": {"floor": 6}, "limits": {"gpu_max_mhz": 1800}}))
            port = self.serve(path)
            self.assertEqual(self.get(port, "/healthz")[0], 200)
            code, body = self.get(port, "/v1/limits")
            self.assertEqual((code, body["applied"]["gpu_cap_mhz"], body["stale"]), (200, 1800, False))
            self.assertEqual(self.get(port, "/v1/status")[1]["status"]["mode"], "RUN")
            self.assertEqual(self.get(port, "/v1/status", "POST")[0], 405)
            self.assertEqual(self.get(port, "/etc/passwd")[0], 404)

    def test_stale_or_missing_status_is_unhealthy(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            port = self.serve(path)
            self.assertEqual(self.get(port, "/healthz")[0], 503)
            path.write_text(json.dumps({"utc_ns": time.time_ns() - 60 * 10**9, "mode": "RUN"}))
            code, body = self.get(port, "/healthz")
            self.assertEqual((code, body["ok"]), (503, False))


if __name__ == "__main__":
    unittest.main()
