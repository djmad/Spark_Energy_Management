from contextlib import contextmanager
from functools import partial
from multiprocessing import get_context
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
import socket
from energy_control.gpu_evidence_channel import GpuEvidenceReader

from energy_control.gpu_command import LoggedGpuClockSetter
from energy_control.gpu_evidence import GpuOwnershipReading
from energy_control.gpu_owner_process import serve_gpu_owner
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_gpu_command import FakeProcess
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID


@contextmanager
def fake_setter(directory, commands, abort_event, *, emergency_release=None):
    with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
        recorder.write_trial_plan(plan())
        def factory(argv, **kwargs):
            commands.put(argv[-1])
            if argv[-1] == "--lock-gpu-clocks=200,500" and emergency_release is not None:
                if not emergency_release.wait(5):
                    raise TimeoutError("fake emergency command remained blocked")
            return FakeProcess([0])
        yield LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
            process_factory=factory, normal_fence=abort_event,
            read_ownership=lambda: GpuOwnershipReading(BOOT_ID, "driver-a", "owner-a",
                                                      recorder.run_id, monotonic(), True))


class GpuOwnerTests(unittest.TestCase):
    def test_spawned_owner_publishes_run_bound_evidence_then_invalidates_on_abort(self):
        with TemporaryDirectory() as directory:
            context = get_context("spawn")
            parent, child = context.Pipe()
            receive, send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            abort = context.Event()
            commands = context.Queue()
            process = context.Process(target=serve_gpu_owner, args=(child, parent,
                partial(fake_setter, directory, commands), abort, send))
            process.start()
            child.close()
            send.close()
            try:
                self.assertTrue(parent.poll(2))
                self.assertEqual(parent.recv_bytes(2), b"RD")
                runs = list(Path(directory).glob("*/events.jsonl"))
                self.assertEqual(len(runs), 1)
                reader = GpuEvidenceReader(receive,
                    (BOOT_ID, "driver-a", "owner-a", runs[0].parent.name))
                self.assertIsNone(reader.read())
                parent.send_bytes(b"C" + (1200).to_bytes(2, "big"))
                self.assertTrue(parent.poll(2))
                self.assertEqual(parent.recv_bytes(2), b"OK")
                proof = reader.read()
                self.assertIsNotNone(proof)
                self.assertEqual(proof.requested_max_mhz, 1200)
                self.assertEqual(proof.exit_code, 0)
                abort.set()
                process.join(2)
                self.assertEqual(process.exitcode, 0)
                self.assertIsNone(reader.read())
            finally:
                abort.set()
                parent.close()
                process.join(2)
                receive.close()
                if process.is_alive():
                    process.terminate()
                    process.join(2)
                commands.close()
                commands.join_thread()

    def test_shared_abort_and_policy_channel_loss_use_same_writer(self):
        for trigger in ("event", "eof", "invalid"):
            with self.subTest(trigger=trigger), TemporaryDirectory() as directory:
                context = get_context("spawn")
                parent, child = context.Pipe()
                abort = context.Event()
                commands = context.Queue()
                process = context.Process(target=serve_gpu_owner, args=(child, parent,
                    partial(fake_setter, directory, commands), abort))
                process.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(2))
                    self.assertEqual(parent.recv_bytes(2), b"RD")
                    parent.send_bytes(b"C" + (1200).to_bytes(2, "big"))
                    self.assertTrue(parent.poll(2))
                    self.assertEqual(parent.recv_bytes(2), b"OK")
                    if trigger == "event":
                        abort.set()
                    elif trigger == "eof":
                        parent.close()
                    else:
                        parent.send_bytes(b"C" + (2000).to_bytes(2, "big"))
                    process.join(2)
                    self.assertEqual(process.exitcode, 0)
                    self.assertEqual(commands.get(timeout=1), "--lock-gpu-clocks=200,1200")
                    self.assertEqual(commands.get(timeout=1), "--lock-gpu-clocks=200,500")
                    paths = list(Path(directory).glob("*/events.jsonl"))
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(inspect_run(paths[0])["clean_end"])
                finally:
                    abort.set()
                    parent.close()
                    process.join(2)
                    if process.is_alive():
                        process.terminate()
                        process.join(2)
                    commands.close()
                    commands.join_thread()
