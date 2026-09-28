from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic
import unittest
from contextlib import contextmanager
from functools import partial
from multiprocessing import get_context

from energy_control.gpu_command import LoggedGpuClockSetter
from energy_control.gpu_evidence import GpuOwnershipReading
from energy_control.gpu_recorder_channel import GpuRecorderClient, serve_gpu_recorder
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_gpu_command import FakeProcess
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID
from energy_control.gpu_owner_process import serve_gpu_owner
from energy_control.gpu_evidence_channel import GpuEvidenceReader


@contextmanager
def remote_logged_setter(channel, run_id, abort_event):
    client = GpuRecorderClient(channel, run_id=run_id, boot_id=BOOT_ID)
    try:
        yield LoggedGpuClockSetter(client, driver_epoch="driver-a", owner_epoch="owner-a",
            normal_fence=abort_event, process_factory=lambda *a, **k: FakeProcess([0]),
            read_ownership=lambda: GpuOwnershipReading(BOOT_ID, "driver-a", "owner-a",
                                                       run_id, monotonic(), True))
    finally:
        client.close()


class GpuRecorderChannelTests(unittest.TestCase):
    def test_spawned_owner_uses_supervisor_run_for_normal_and_emergency_commands(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                client_socket, server_socket = socket.socketpair()
                evidence_read, evidence_write = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                worker = Thread(target=serve_gpu_recorder, args=(server_socket, recorder), daemon=True)
                worker.start()
                context = get_context("spawn")
                parent, child = context.Pipe()
                abort = context.Event()
                owner = context.Process(target=serve_gpu_owner, args=(child, parent,
                    partial(remote_logged_setter, client_socket, recorder.run_id), abort, evidence_write))
                owner.start()
                child.close()
                client_socket.close()
                evidence_write.close()
                reader = GpuEvidenceReader(evidence_read,
                    (BOOT_ID, "driver-a", "owner-a", recorder.run_id))
                try:
                    self.assertTrue(parent.poll(2))
                    self.assertEqual(parent.recv_bytes(2), b"RD")
                    parent.send_bytes(b"C" + (1200).to_bytes(2, "big"))
                    self.assertTrue(parent.poll(2))
                    self.assertEqual(parent.recv_bytes(2), b"OK")
                    proof = reader.read()
                    self.assertIsNotNone(proof)
                    self.assertEqual(proof.run_id, recorder.run_id)
                    abort.set()
                    owner.join(3)
                    self.assertEqual(owner.exitcode, 0)
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    paths = list(Path(directory).glob("*/events.jsonl"))
                    self.assertEqual(len(paths), 1)
                    rows = inspect_run(paths[0])["records"]
                    intents = [row for row in rows if row["kind"] == "gpu_setter_intent"]
                    self.assertEqual([row["maximum_mhz"] for row in intents], [1200, 500])
                    self.assertTrue(all(row["run_id"] == recorder.run_id for row in rows))
                    self.assertEqual(inspect_run(paths[0])["pending_intents"], {})
                finally:
                    abort.set()
                    parent.close()
                    owner.join(3)
                    if owner.is_alive():
                        owner.terminate()
                        owner.join(2)
                    evidence_read.close()
                    worker.join(2)

    def test_gpu_setter_logs_in_supervisor_run(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                client_socket, server_socket = socket.socketpair()
                worker = Thread(target=serve_gpu_recorder, args=(server_socket, recorder), daemon=True)
                worker.start()
                client = GpuRecorderClient(client_socket, run_id=recorder.run_id, boot_id=BOOT_ID)
                try:
                    path = Path(directory) / recorder.run_id / "events.jsonl"
                    def command(*args, **kwargs):
                        self.assertTrue(inspect_run(path)["pending_intents"])
                        return FakeProcess([0])
                    setter = LoggedGpuClockSetter(client, driver_epoch="driver-a", owner_epoch="owner-a",
                        process_factory=command, read_ownership=lambda: GpuOwnershipReading(
                            BOOT_ID, "driver-a", "owner-a", recorder.run_id, monotonic(), True))
                    self.assertEqual(setter.apply(minimum_mhz=200, maximum_mhz=1200).status, "success")
                    proof = setter.read()
                    self.assertEqual(proof.run_id, recorder.run_id)
                    self.assertEqual(inspect_run(path)["pending_intents"], {})
                    self.assertEqual(len(list(Path(directory).glob("*/events.jsonl"))), 1)
                finally:
                    client.close()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())

    def test_wrong_run_binding_is_rejected(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                client_socket, server_socket = socket.socketpair()
                worker = Thread(target=serve_gpu_recorder, args=(server_socket, recorder), daemon=True)
                worker.start()
                try:
                    with self.assertRaises(ValueError):
                        GpuRecorderClient(client_socket, run_id="ff" * 16, boot_id=BOOT_ID)
                finally:
                    client_socket.close()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
