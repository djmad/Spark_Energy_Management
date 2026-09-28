import json
import socket
import unittest

from energy_control.broker import BrokerCore
from energy_control.broker_socket import (
    API_OPERATOR_LABEL, CLI_OPERATOR_LABEL, OPERATOR_SOCKET_NAME,
    _read_request, decode_request, dispatch, peer_credentials,
)


class BrokerSocketTests(unittest.TestCase):
    def test_operator_label_is_derived_from_socket_peer_not_request_text(self):
        broker = BrokerCore(object(), object(), object(), api_uid=1000,
                            operator_uid=1001)
        for peer_uid, wrong in ((1000, CLI_OPERATOR_LABEL),
                                (1001, API_OPERATOR_LABEL)):
            for operation in ("authorize", "commit"):
                with self.subTest(peer_uid=peer_uid, op=operation), self.assertRaises(PermissionError):
                    dispatch(broker, {"op": operation, "operator": wrong}, peer_uid=peer_uid)

    def test_total_deadline_rejects_trickled_request(self):
        class Clock:
            now = 0.0

            def __call__(self):
                return self.now

        class TrickleSocket:
            def __init__(self, clock, chunks):
                self.clock = clock
                self.chunks = iter(chunks)
                self.timeouts = []

            def settimeout(self, value):
                self.timeouts.append(value)

            def recv(self, _size):
                self.clock.now += 0.75
                return next(self.chunks)

        clock = Clock()
        slow = TrickleSocket(clock, [b"{", b'"', b"o", b"p", b'":"status"}\n'])
        with self.assertRaises(TimeoutError):
            _read_request(slow, clock=clock)
        self.assertEqual(len(slow.timeouts), 3)
        self.assertLess(slow.timeouts[-1], slow.timeouts[0])

    def test_one_complete_broker_frame(self):
        class CompleteSocket:
            def settimeout(self, _value):
                pass

            def recv(self, _size):
                return b'{"op":"status"}\n'

        self.assertEqual(_read_request(CompleteSocket()), b'{"op":"status"}')

    def test_distinct_operator_socket_name(self):
        self.assertNotEqual(OPERATOR_SOCKET_NAME, "energy-control-broker.sock")
    def test_strict_decoder(self):
        request = decode_request(b'{"op":"status"}')
        self.assertEqual(request, {"op": "status"})
        for payload in (b'{"op":"status","peer_uid":1000}',
                        b'{"op":"status","op":"commit"}',
                        b'{"op":"exec","command":"id"}',
                        b'{"op":"propose","changes":{"gpu_max_mhz":NaN},"base_revision":0}',
                        b'{}', b'[' * 10000):
            with self.subTest(payload=payload[:50]), self.assertRaises(ValueError):
                decode_request(payload)

    def test_kernel_credentials_on_socketpair(self):
        first, second = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pid, uid, gid = peer_credentials(first)
            self.assertGreater(pid, 0)
            self.assertGreaterEqual(uid, 0)
            self.assertGreaterEqual(gid, 0)
        finally:
            first.close()
            second.close()


if __name__ == "__main__":
    unittest.main()
