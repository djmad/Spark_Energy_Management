from contextlib import contextmanager
from multiprocessing import get_context
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from energy_control.gpu_command import LoggedGpuClockSetter
from energy_control.gpu_evidence import GpuOwnershipReading
from energy_control.gpu_evidence_channel import GpuEvidenceReader
from energy_control.gpu_owner_session import GpuOwnerSession
from energy_control.gpu_recorder_channel import GpuRecorderClient
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_gpu_command import FakeProcess
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID


@contextmanager
def fake_owner(channel, run_id, boot_id, abort_event):
    client = GpuRecorderClient(channel, run_id=run_id, boot_id=boot_id)
    try:
        yield LoggedGpuClockSetter(client, driver_epoch="driver-a", owner_epoch="owner-a",
            normal_fence=abort_event, process_factory=lambda *a, **k: FakeProcess([0]),
            read_ownership=lambda: GpuOwnershipReading(boot_id, "driver-a", "owner-a",
                                                       run_id, monotonic(), True))
    finally:
        client.close()


@contextmanager
def failing_owner(channel, run_id, boot_id, abort_event):
    channel.close()
    raise RuntimeError("fake factory failure")
    yield


@contextmanager
def hanging_owner(channel, run_id, boot_id, abort_event):
    abort_event.wait(10)
    channel.close()
    raise RuntimeError("fake factory aborted before readiness")
    yield


class GpuOwnerSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.recorder = CommissioningRecorder(Path(self.directory.name), boot_id=BOOT_ID)
        self.recorder.__enter__()
        self.recorder.write_trial_plan(plan())
        self.abort = get_context("spawn").Event()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            self.assertTrue(session.close())
            self.assertTrue(session.released)
        self.recorder.__exit__(None, None, None)
        self.directory.cleanup()

    def session(self, factory=fake_owner, **kwargs):
        session = GpuOwnerSession(self.recorder, factory, self.abort,
                                  driver_epoch="driver-a", owner_epoch="owner-a", **kwargs)
        self.sessions.append(session)
        return session

    def rows(self):
        paths = list(Path(self.directory.name).glob("*/events.jsonl"))
        self.assertEqual(len(paths), 1)
        return inspect_run(paths[0])

    def test_command_evidence_and_emergency_share_one_run(self):
        session = self.session()
        session.start()
        attempt = session.apply(minimum_mhz=200, maximum_mhz=1100)
        self.assertEqual(attempt.status, "success")
        self.assertTrue(attempt.process_reaped)
        self.assertEqual(session.read().intent_seq, attempt.intent_seq)
        second = session.apply(minimum_mhz=200, maximum_mhz=1200)
        self.assertGreater(second.intent_seq, attempt.intent_seq)
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)
        run = self.rows()
        intents = [row["maximum_mhz"] for row in run["records"] if row["kind"] == "gpu_setter_intent"]
        self.assertEqual(intents, [1100, 1200, 500])
        self.assertEqual(run["pending_intents"], {})

    def test_policy_feed_stays_fresh_without_reads(self):
        session = self.session()
        session.start()
        session.apply(minimum_mhz=200, maximum_mhz=1200)
        sleep(1.5)  # Longer than eight 100 ms frames: the old reader faulted here.
        proof = session.read()
        self.assertIsNotNone(proof)
        self.assertEqual(proof.requested_max_mhz, 1200)
        self.assertFalse(self.abort.is_set())

    def test_concurrent_reads_are_safe(self):
        session = self.session()
        session.start()
        session.apply(minimum_mhz=200, maximum_mhz=1200)
        results = []
        def reader():
            for _ in range(40):
                results.append(session.read())
                sleep(.01)
        threads = [Thread(target=reader) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(results), 160)
        self.assertTrue(all(proof is not None and proof.requested_max_mhz == 1200
                            for proof in results))

    def test_guard_receives_separate_feed(self):
        guard_read, guard_write = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        session = self.session(guard_evidence=guard_write)
        guard = GpuEvidenceReader(guard_read, session.context)
        try:
            session.start()
            self.assertEqual(guard_write.fileno(), -1)  # Session released its copy.
            attempt = session.apply(minimum_mhz=200, maximum_mhz=1150)
            deadline = monotonic() + 1
            proof = None
            while proof is None and monotonic() < deadline:
                proof = guard.read()
                sleep(.02)
            self.assertEqual(proof.intent_seq, attempt.intent_seq)
            self.assertEqual(session.read().intent_seq, attempt.intent_seq)
        finally:
            guard_read.close()

    def test_factory_failure_cleans_up_and_aborts(self):
        session = self.session(failing_owner)
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertTrue(self.abort.is_set())
        self.assertTrue(session.released)
        self.assertEqual(session.exitcode, 2)
        with self.assertRaises(RuntimeError):
            session.start()

    def test_startup_timeout_aborts_hanging_owner(self):
        session = self.session(hanging_owner, command_timeout_s=.5)
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertTrue(self.abort.is_set())
        self.assertTrue(session.released)

    def test_owner_death_faults_session(self):
        session = self.session()
        session.start()
        session.apply(minimum_mhz=200, maximum_mhz=1200)
        session._process.terminate()
        session._process.join(2)
        deadline = monotonic() + 2.5  # evidence expires after 1.5 s (defect 33b)
        while not self.abort.is_set() and monotonic() < deadline:
            sleep(.02)
        self.assertTrue(self.abort.is_set())
        self.assertIsNone(session.read())
        with self.assertRaises(RuntimeError):
            session.apply(minimum_mhz=200, maximum_mhz=1200)

    def test_refusals_do_not_trip_abort(self):
        session = self.session()
        with self.assertRaises(RuntimeError):
            session.apply(minimum_mhz=200, maximum_mhz=1200)  # Not started.
        session.start()
        for bad in (199, 1801, 1200.0):
            with self.assertRaises(ValueError):
                session.apply(minimum_mhz=200, maximum_mhz=bad)
        with self.assertRaises(ValueError):
            session.apply(minimum_mhz=300, maximum_mhz=1200)
        with patch("energy_control.gpu_owner_session.MAX_NORMAL_COMMANDS", 1):
            session.apply(minimum_mhz=200, maximum_mhz=1200)
            self.assertEqual(session.commands_remaining, 0)
            with self.assertRaises(RuntimeError):
                session.apply(minimum_mhz=200, maximum_mhz=1100)
        self.assertFalse(self.abort.is_set())
        self.assertIsNotNone(session.read())

    def test_command_above_trial_plan_is_refused_before_send(self):
        session = self.session()
        session.start()
        session.apply(minimum_mhz=200, maximum_mhz=1200)
        with self.assertRaises(ValueError):
            session.apply(minimum_mhz=200, maximum_mhz=1250)  # Plan ceiling is 1200.
        self.assertFalse(self.abort.is_set())
        self.assertEqual(session.read().requested_max_mhz, 1200)
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)
        intents = [row["maximum_mhz"] for row in self.rows()["records"]
                   if row["kind"] == "gpu_setter_intent"]
        self.assertEqual(intents, [1200, 500])

    def test_start_requires_durable_trial_plan(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                session = GpuOwnerSession(recorder, fake_owner, self.abort,
                                          driver_epoch="driver-a", owner_epoch="owner-a")
                with self.assertRaises(RuntimeError):
                    session.start()
                self.assertTrue(session.close())

    def test_abort_event_fences_session(self):
        session = self.session()
        session.start()
        session.apply(minimum_mhz=200, maximum_mhz=1200)
        self.abort.set()
        self.assertIsNone(session.read())
        with self.assertRaises(RuntimeError):
            session.apply(minimum_mhz=200, maximum_mhz=1100)
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)

    def test_constructor_rejects_unbound_inputs(self):
        with self.assertRaises(ValueError):
            GpuOwnerSession(object(), fake_owner, self.abort, driver_epoch="d", owner_epoch="o")
        with self.assertRaises(ValueError):
            GpuOwnerSession(self.recorder, fake_owner, self.abort, driver_epoch="", owner_epoch="o")
        stream_a, stream_b = socket.socketpair()
        try:
            with self.assertRaises(ValueError):
                GpuOwnerSession(self.recorder, fake_owner, self.abort, driver_epoch="d",
                                owner_epoch="o", guard_evidence=stream_a)
        finally:
            stream_a.close()
            stream_b.close()


class ServiceModeSessionTests(unittest.TestCase):
    def test_service_owner_has_no_command_budget(self):
        from test_recorder import service_plan
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID, keep_segments=4) as recorder:
                recorder.write_trial_plan(service_plan())
                abort = get_context("spawn").Event()
                session = GpuOwnerSession(recorder, fake_owner, abort, driver_epoch="driver-a",
                                          owner_epoch="owner-a", service=True)
                try:
                    session.start()
                    self.assertIsNone(session.commands_remaining)
                    for index in range(140):  # Beyond the commissioning 128-command budget.
                        session.apply(minimum_mhz=200, maximum_mhz=1100 + 25 * (index % 4))
                    self.assertFalse(abort.is_set())
                finally:
                    self.assertTrue(session.close())
                self.assertEqual(session.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
