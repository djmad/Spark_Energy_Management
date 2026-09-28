from io import BytesIO
from threading import Event, Thread
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
import socket
from energy_control.http_llm_transport import HttpLlmTransport


class Connection:
    def __init__(self):
        self.sock = None
        self.connected = Event()
        self.release = Event()
        self.sent = False
    def connect(self):
        self.connected.set()
        if not self.release.wait(1):
            raise TimeoutError()
    def request(self, *args, **kwargs):
        self.sent = True
    def getresponse(self):
        response = BytesIO(b"data: [DONE]\n\n")
        response.status = 200
        return response
    def close(self):
        pass


class HttpTransportTests(unittest.TestCase):
    def test_cancel_interrupts_blocked_socket_send_without_reconnecting(self):
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        entered = Event()

        class BlockedSend(Connection):
            def __init__(self):
                super().__init__()
                self.sock = sender
                self.connect_count = 0

            def connect(self):
                self.connect_count += 1

            def request(self, *args, **kwargs):
                entered.set()
                # Synthetic bytes only; the peer deliberately never reads.
                self.sock.sendall(b"x" * 1048576)
                raise AssertionError("blocked send unexpectedly completed")

            def close(self):
                sender.close()

        connection = BlockedSend()
        request = HttpLlmTransport(connection_factory=lambda: connection).prepare(
            {"stream": True}, workload_id="ab" * 16)
        try:
            request.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(request.wait_local_done(.05))
            canceller = Thread(target=request.cancel, daemon=True)
            canceller.start()
            canceller.join(1)
            self.assertFalse(canceller.is_alive(), "cancel blocked behind send")
            self.assertTrue(request.wait_local_done(1))
            self.assertFalse(request.wait_terminal(0))
            self.assertFalse(request.response_completed)
            self.assertEqual(connection.connect_count, 1)
            self.assertEqual(connection.auto_open, 0)
        finally:
            request.cancel()
            receiver.close()
            sender.close()

    def test_cancel_closes_real_socket_while_server_has_not_generated_tokens(self):
        entered, disconnected = Event(), Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
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
        request = None
        try:
            transport = HttpLlmTransport(connection_factory=lambda: HTTPConnection(
                "127.0.0.1", server.server_port, timeout=.5))
            request = transport.prepare({"stream": True}, workload_id="ab" * 16)
            request.start()
            self.assertTrue(entered.wait(1))
            # Longer than the connect timeout, without response tokens.
            self.assertFalse(request.wait_local_done(.6))
            request.cancel()
            self.assertTrue(disconnected.wait(1))
            self.assertTrue(request.wait_local_done(1))
            self.assertFalse(request.wait_terminal(.1))
        finally:
            if request is not None:
                request.cancel()
            server.shutdown()
            server.server_close()
            worker.join(2)

    def test_cancel_before_start_and_during_connect_prevents_send(self):
        for before in (True, False):
            connection = Connection()
            request = HttpLlmTransport(connection_factory=lambda: connection).prepare(
                {"stream": True}, workload_id="ab" * 16)
            if before:
                request.cancel()
                request.start()
            else:
                request.start()
                self.assertTrue(connection.connected.wait(1))
                request.cancel()
                connection.release.set()
            self.assertTrue(request.wait_local_done(1))
            self.assertFalse(connection.sent)
            self.assertTrue(request.wait_terminal(.1))

    def test_response_completion_is_not_engine_drain(self):
        connection = Connection()
        connection.release.set()
        request = HttpLlmTransport(connection_factory=lambda: connection).prepare(
            {"stream": True}, workload_id="ab" * 16)
        request.start()
        self.assertTrue(request.wait_local_done(1))
        self.assertTrue(request.response_completed)
        self.assertFalse(request.wait_terminal(.1))
        self.assertEqual(connection.auto_open, 0)

    def test_live_disabled_by_default(self):
        with self.assertRaises(PermissionError):
            HttpLlmTransport()
