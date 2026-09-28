import json
from pathlib import Path
import unittest
from unittest.mock import patch

from energy_control.parameter_api import BrokerClient, BrokerUnavailable, MutationRouter


class FakeBrokerClient:
    def __init__(self):
        self.calls = []
        self.reply = {"ok": True, "result": {"id": "proposal"}}
        self.fail = False

    def call(self, request):
        self.calls.append(request)
        if self.fail:
            raise BrokerUnavailable("injected")
        return self.reply


class ParameterApiTests(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBrokerClient()
        self.router = MutationRouter(self.broker)
        self.proposal_id = "abcdefghijklmnop1234567890"

    def test_exact_proposal_and_authorization_routes(self):
        status, _ = self.router.handle("/api/v1/changes",
            json.dumps({"changes": {"gpu_max_mhz": 1800}, "base_revision": 0}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(self.broker.calls[-1]["op"], "propose")
        status, _ = self.router.handle("/api/v1/changes",
            json.dumps({"changes": {"cpu_kp": 0.08, "gpu_kd": 0.09},
                        "base_revision": 0}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(self.broker.calls[-1]["changes"],
                         {"cpu_kp": 0.08, "gpu_kd": 0.09})
        changes = {"cpu_entry_ratio": 0.4, "cpu_recovery_ratio_s": 0.02,
                   "cpu_idle_down_ratio_s": 0.05}
        status, _ = self.router.handle("/api/v1/changes",
            json.dumps({"changes": changes, "base_revision": 0}).encode())
        self.assertEqual(status, 201)
        self.assertEqual(self.broker.calls[-1]["changes"], changes)
        status, _ = self.router.handle(f"/api/v1/changes/{self.proposal_id}/authorize",
            b'{"password":"test-only-password","session":"cli"}')
        self.assertEqual(status, 200)
        self.assertEqual(self.broker.calls[-1]["operator"], "api-password-operator")
        self.assertEqual(self.broker.calls[-1]["password"], "test-only-password")
        status, _ = self.router.handle(f"/api/v1/changes/{self.proposal_id}/commit",
            b'{"authorization":"one-use-token","session":"cli"}')
        self.assertEqual(status, 200)
        self.assertEqual(self.broker.calls[-1]["token"], "one-use-token")

    def test_unknown_fields_paths_and_nonfinite_refused(self):
        for path, body in (("/api/v1/exec", b'{"command":"id"}'),
                           ("/api/v1/changes", b'{"changes":{},"base_revision":0,"path":"/etc"}'),
                           ("/api/v1/changes", b'{"changes":{},"base_revision":NaN}'),
                           ("/api/v1/changes", b'{"changes":{},"changes":{},"base_revision":0}'),
                           ("/api/v1/changes", b"x" * 4097)):
            with self.subTest(path=path, body=body[:30]):
                self.assertNotIn(self.router.handle(path, body)[0], (200, 201))
        self.assertEqual(self.broker.calls, [])

    def test_broker_failure_and_denial_mapping(self):
        self.broker.fail = True
        status, _ = self.router.handle("/api/v1/changes",
            b'{"changes":{"gpu_max_mhz":1800},"base_revision":0}')
        self.assertEqual(status, 503)
        self.broker.fail = False
        self.broker.reply = {"ok": False, "error": "forbidden"}
        status, _ = self.router.handle(f"/api/v1/changes/{self.proposal_id}/authorize",
            b'{"password":"wrong","session":"cli"}')
        self.assertEqual(status, 403)

    def test_broker_client_never_sends_password_to_nonroot_peer(self):
        class FakeSocket:
            sent = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _seconds):
                pass

            def connect(self, _path):
                pass

            def sendall(self, _payload):
                self.sent = True

        connection = FakeSocket()
        path = Path("/run/energy-control/energy-control-broker.sock")
        with (patch("energy_control.parameter_api.socket.socket", return_value=connection),
              patch("energy_control.parameter_api.peer_credentials",
                    return_value=(1234, 1000, 1000))):
            with self.assertRaises(BrokerUnavailable):
                BrokerClient(path).call({"op": "authorize", "proposal_id": "p",
                                         "password": "test-only-password",
                                         "operator": "test", "session": "test"})
        self.assertFalse(connection.sent)

    def test_broker_reply_total_deadline_rejects_slow_trickle(self):
        now = [0.0]
        class SlowSocket:
            reads = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _seconds):
                pass

            def connect(self, _path):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                self.reads += 1
                now[0] += 0.9
                return b"x"

        connection = SlowSocket()
        with (patch("energy_control.parameter_api.socket.socket", return_value=connection),
              patch("energy_control.parameter_api.peer_credentials",
                    return_value=(1234, 0, 0)),
              patch("energy_control.parameter_api.monotonic", side_effect=lambda: now[0])):
            with self.assertRaises(BrokerUnavailable):
                BrokerClient().call({"op": "status"})
        self.assertLessEqual(connection.reads, 3)


if __name__ == "__main__":
    unittest.main()
