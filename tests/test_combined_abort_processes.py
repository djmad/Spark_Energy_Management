"""No hardware: fake GPU command, sleeping CPU child, dummy HTTP server."""
from functools import partial
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import get_context
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest

from energy_control.cpu_workload_process import serve_cpu_workload
from energy_control.gpu_owner_process import serve_gpu_owner
from energy_control.guard_ownership_process import GuardOwnershipProcess
from energy_control.process_http_transport import ProcessHttpLlmTransport, GuardHttpCancellation
from test_cpu_workload_process import sleeping_child
from test_gpu_owner_process import fake_setter
from test_process_http_transport import sample_safe, never_verified


class CombinedAbortTests(unittest.TestCase):
    def test_guard_heartbeat_loss_reaches_all_three_independent_paths(self):
        self._combined_abort(stall_gpu=False)

    def test_stalled_gpu_command_cannot_delay_http_and_cpu_cancellation(self):
        self._combined_abort(stall_gpu=True)

    def _combined_abort(self, *, stall_gpu):
        entered, disconnected = Event(), Event()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(4)
                entered.set()
                if self.connection.recv(1) == b"":
                    disconnected.set()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        context = get_context("spawn")
        abort, cpu_started = context.Event(), context.Event()
        emergency_release = context.Event()
        if not stall_gpu:
            emergency_release.set()
        commands = context.Queue()
        gpu_parent, gpu_child = context.Pipe()
        cpu_parent, cpu_child = context.Pipe()
        guard = GuardOwnershipProcess(GuardHttpCancellation(abort), never_verified,
            sample_safe, deadline_s=1, completion_mode="http_observed")
        request = ProcessHttpLlmTransport(guard_cancelled=abort,
            connection_factory=partial(HTTPConnection, "127.0.0.1", server.server_port,
                                       timeout=.5)).prepare({"stream": True}, workload_id="ab" * 16)
        with TemporaryDirectory() as directory:
            gpu = context.Process(target=serve_gpu_owner, args=(gpu_child, gpu_parent,
                partial(fake_setter, directory, commands, emergency_release=emergency_release), abort))
            cpu = context.Process(target=serve_cpu_workload, args=(cpu_child, cpu_parent,
                partial(sleeping_child, cpu_started), abort, 10))
            gpu.start()
            cpu.start()
            gpu_child.close()
            cpu_child.close()
            guard_started = False
            try:
                for channel in (gpu_parent, cpu_parent):
                    self.assertTrue(channel.poll(2))
                    self.assertEqual(channel.recv_bytes(2), b"RD")
                gpu_parent.send_bytes(b"C" + (1200).to_bytes(2, "big"))
                self.assertTrue(gpu_parent.poll(2))
                self.assertEqual(gpu_parent.recv_bytes(2), b"OK")
                guard.start()
                guard_started = True
                self.assertTrue(guard.register("ab" * 16))
                self.assertTrue(guard.authorize_start("ab" * 16))
                cpu_parent.send_bytes(b"GO")
                self.assertTrue(cpu_parent.poll(2))
                self.assertEqual(cpu_parent.recv_bytes(2), b"ON")
                request.start()
                self.assertTrue(entered.wait(2))
                # No more heartbeats, cancellation calls or GPU commands.
                self.assertTrue(disconnected.wait(3))
                guard.join(2)
                cpu.join(2)
                self.assertTrue(request.wait_local_done(2))
                self.assertEqual((guard.exitcode, cpu.exitcode), (2, 0))
                self.assertEqual(commands.get(timeout=1), "--lock-gpu-clocks=200,1200")
                self.assertEqual(commands.get(timeout=1), "--lock-gpu-clocks=200,500")
                if stall_gpu:
                    self.assertTrue(gpu.is_alive())
                    self.assertFalse(emergency_release.is_set())
                emergency_release.set()
                gpu.join(2)
                self.assertEqual((guard.exitcode, cpu.exitcode, gpu.exitcode), (2, 0, 0))
                self.assertTrue(request.wait_local_done(2))
                self.assertFalse(request.wait_terminal(0))
            finally:
                abort.set()
                emergency_release.set()
                request.cancel()
                request.wait_local_done(2)
                if guard_started:
                    guard.close()
                    guard.join(2)
                gpu_parent.close()
                cpu_parent.close()
                cpu.join(3)
                gpu.join(3)
                server.shutdown()
                server.server_close()
                thread.join(2)
                commands.close()
                commands.join_thread()
