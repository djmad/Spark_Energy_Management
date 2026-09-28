from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import get_context
from threading import Event, Thread
import unittest
from time import monotonic

from energy_control.process_http_transport import ProcessHttpLlmTransport, GuardHttpCancellation
from energy_control.guard_ownership_process import GuardOwnershipProcess
from test_safety import good_snapshot


def sample_safe():
    return good_snapshot(monotonic_s=monotonic())


def never_verified(identifier):
    return False


class ProcessHttpTests(unittest.TestCase):
    def test_http_guard_reuses_slots_but_cannot_disarm_with_server_uncertainty(self):
        latch = get_context("spawn").Event()
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), never_verified,
            sample_safe, deadline_s=1, completion_mode="http_observed")
        guard.start()
        try:
            for number in range(21):
                identifier = f"{number:032x}"
                self.assertTrue(guard.register(identifier))
                self.assertTrue(guard.authorize_start(identifier))
                self.assertTrue(guard.client_done(identifier))
            self.assertFalse(guard.disarm())
            guard.join(2)
            self.assertEqual(guard.exitcode, 2)
            self.assertTrue(latch.is_set())
        finally:
            guard.close()
            guard.join(2)

    def test_strict_guard_rejects_http_completion(self):
        latch = get_context("spawn").Event()
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), never_verified,
                                      sample_safe, deadline_s=1)
        guard.start()
        try:
            self.assertTrue(guard.register("ab" * 16))
            self.assertFalse(guard.client_done("ab" * 16))
            guard.join(2)
            self.assertEqual(guard.exitcode, 2)
        finally:
            guard.close()
            guard.join(2)

    def test_guard_heartbeat_loss_cancels_socket_without_parent_cancel_call(self):
        entered, disconnected = Event(), Event()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(3)
                entered.set()
                if self.connection.recv(1) == b"":
                    disconnected.set()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        context = get_context("spawn")
        latch = context.Event()
        request = ProcessHttpLlmTransport(
            guard_cancelled=latch,
            connection_factory=partial(HTTPConnection, "127.0.0.1", server.server_port,
                                       timeout=.5)).prepare({"stream": True}, workload_id="ab" * 16)
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), never_verified,
                                      sample_safe, deadline_s=1, start_method="spawn")
        try:
            guard.start()
            self.assertTrue(guard.register("ab" * 16))
            self.assertTrue(guard.authorize_start("ab" * 16))
            request.start()
            self.assertTrue(entered.wait(3))
            # Deliberately stop heartbeats. No request.cancel(), wait/poll,
            # or policy work drives cancellation.
            self.assertTrue(disconnected.wait(3))
            guard.join(2)
            self.assertEqual(guard.exitcode, 2)  # Aborted; server drain unverified.
            self.assertTrue(latch.is_set())
            self.assertTrue(request.wait_local_done(2))
            self.assertFalse(request.wait_terminal(0))
        finally:
            latch.set()
            request.cancel()
            request.wait_local_done(2)
            guard.close()
            guard.join(2)
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_cancel_before_start_sends_nothing(self):
        request = ProcessHttpLlmTransport(
            guard_cancelled=get_context("spawn").Event(), enable_live=True
        ).prepare({"stream": True}, workload_id="ab" * 16)
        request.cancel()
        # The dispatcher intentionally skips start() for canceled queued work.
        self.assertTrue(request.wait_local_done(0))
        self.assertTrue(request.wait_terminal(0))
        request.start()
        self.assertTrue(request.wait_terminal(0))

    def test_guard_latch_blocks_later_prepared_requests(self):
        latch = get_context("spawn").Event()
        transport = ProcessHttpLlmTransport(guard_cancelled=latch, enable_live=True)
        latch.set()
        request = transport.prepare({"stream": True}, workload_id="cd" * 16)
        request.start()
        self.assertTrue(request.wait_terminal(0))
        self.assertIsNone(request._process.pid)
