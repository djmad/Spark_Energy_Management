import unittest
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from multiprocessing import get_context
from contextlib import contextmanager
from threading import Event, Thread
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from energy_control.http_llm_transport import HttpLlmTransport
from energy_control.request_gateway import OwnedRequestDispatcher
import test_http_llm_transport as http_fixtures
import test_request_gateway as fixtures
from energy_control.resident_abort import stop_test_loads
from energy_control.process_http_transport import ProcessHttpLlmTransport, GuardHttpCancellation
from energy_control.guard_ownership_process import GuardOwnershipProcess
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_process_http_transport import sample_safe, never_verified
from test_recorder import BOOT_ID


def forbidden_connection():
    raise AssertionError("canceled entry must not open a connection")


class HttpGatewayTests(unittest.TestCase):
    def test_cancel_at_entry_does_not_start_http_or_fault_strict_receipt_check(self):
        latch = get_context("spawn").Event()
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), never_verified,
            sample_safe, deadline_s=2, completion_mode="http_observed")
        guard.start()
        faults = []
        @contextmanager
        def cancel_at_entry(identifier):
            stop_test_loads(dispatcher.gate)
            yield True
        dispatcher = OwnedRequestDispatcher(ProcessHttpLlmTransport(
            guard_cancelled=latch, connection_factory=forbidden_connection), fixtures.FakeRecorder(),
            guard_ownership=guard, completion_mode="http_observed",
            prepare_entry=cancel_at_entry, on_fault=faults.append)
        try:
            dispatcher.arm_admission()
            dispatcher.submit({"stream": True})
            self.assertTrue(dispatcher.join_workers(2))
            self.assertEqual(faults, [])
            self.assertFalse(latch.is_set())
            self.assertEqual(dispatcher.gate.accounting_counts(), (0, 0, 1))
        finally:
            dispatcher.cancel_owned_requests()
            dispatcher.join_workers(2)
            guard.close()
            guard.join(2)

    def test_isolated_gateway_guard_and_durable_normal_stop(self):
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
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        latch = get_context("spawn").Event()
        guard = GuardOwnershipProcess(GuardHttpCancellation(latch), never_verified,
            sample_safe, deadline_s=2, completion_mode="http_observed")
        guard.start()
        dispatcher = None
        try:
            with TemporaryDirectory() as directory:
                with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                    path = Path(directory) / recorder.run_id / "events.jsonl"
                    faults = []
                    dispatcher = OwnedRequestDispatcher(ProcessHttpLlmTransport(
                        guard_cancelled=latch, connection_factory=partial(
                            HTTPConnection, "127.0.0.1", server.server_port, timeout=.5)),
                        recorder, completion_mode="http_observed", guard_ownership=guard,
                        on_fault=faults.append)
                    dispatcher.arm_admission()
                    dispatcher.submit({"stream": True})
                    self.assertTrue(entered.wait(2))
                    before = inspect_run(path)
                    self.assertTrue(before["pending_intents"])
                    self.assertTrue(any(row["kind"] == "dispatch_intent"
                                        for row in before["records"]))
                    stop_test_loads(dispatcher.gate)
                    self.assertTrue(disconnected.wait(2))
                    self.assertTrue(dispatcher.join_workers(2))
                    self.assertEqual(faults, [])
                    self.assertFalse(latch.is_set(), "normal stop triggered guard abort")
                    self.assertEqual(dispatcher.gate.accounting_counts(), (0, 0, 1))
                    after = inspect_run(path)
                    outcomes = [row for row in after["records"] if row["kind"] == "outcome"]
                    self.assertEqual(len(outcomes), 1)
                    self.assertFalse(outcomes[0]["verified"])
                    self.assertFalse(after["clean_end"])
        finally:
            if dispatcher is not None:
                dispatcher.close_admission()
                dispatcher.cancel_owned_requests()
                dispatcher.join_workers(2)
            guard.close()
            guard.join(2)
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_connection_failure_is_not_reported_as_successful_request(self):
        def unavailable():
            raise ConnectionRefusedError()
        faults = []
        dispatcher = OwnedRequestDispatcher(HttpLlmTransport(connection_factory=unavailable),
            fixtures.FakeRecorder(), completion_mode="http_observed", on_fault=faults.append)
        dispatcher.arm_admission()
        dispatcher.submit({"stream": True})
        self.assertTrue(dispatcher.join_workers(1))
        self.assertTrue(faults)

    def test_normal_stop_closes_http_request_without_emergency_fault(self):
        entered, disconnected = Event(), Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(2)
                entered.set()
                if self.connection.recv(1) == b"":
                    disconnected.set()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        faults = []
        dispatcher = OwnedRequestDispatcher(HttpLlmTransport(connection_factory=lambda:
            HTTPConnection("127.0.0.1", server.server_port, timeout=.5)),
            fixtures.FakeRecorder(), completion_mode="http_observed", on_fault=faults.append)
        try:
            dispatcher.arm_admission()
            dispatcher.submit({"stream": True})
            self.assertTrue(entered.wait(1))
            stopped = stop_test_loads(dispatcher.gate)
            self.assertTrue(stopped.cancellation_issued)
            self.assertTrue(disconnected.wait(1))
            self.assertTrue(dispatcher.join_workers(1))
            self.assertEqual(faults, [])
            self.assertEqual(dispatcher.gate.counts(), (0, 0))
            self.assertEqual(dispatcher.gate.unverified_client_completions(), 1)
            self.assertFalse(dispatcher.verify_admission_and_requests())
        finally:
            dispatcher.close_admission()
            dispatcher.cancel_owned_requests()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_response_completion_releases_client_slot_but_retains_uncertainty(self):
        def connect():
            connection = http_fixtures.Connection()
            connection.release.set()
            return connection
        recorder = fixtures.FakeRecorder()
        dispatcher = OwnedRequestDispatcher(HttpLlmTransport(connection_factory=connect),
            recorder, max_requests=1, completion_mode="http_observed")
        dispatcher.arm_admission()
        for _ in range(2):
            dispatcher.submit({"stream": True})
            self.assertTrue(dispatcher.join_workers(1))
            self.assertEqual(dispatcher.gate.counts(), (0, 0))
        self.assertEqual(dispatcher.gate.unverified_client_completions(), 2)
        dispatcher.close_admission()
        self.assertFalse(dispatcher.verify_admission_and_requests())
        self.assertEqual(recorder.events.count(("outcome", 7, False)), 2)

    def test_other_transport_cannot_silently_relax_terminal_contract(self):
        with self.assertRaises(ValueError):
            OwnedRequestDispatcher(fixtures.FakeTransport(fixtures.FakeHandle()),
                                   fixtures.FakeRecorder(), completion_mode="http_observed")
