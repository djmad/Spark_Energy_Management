"""CPU/fan owner processes against fake sysfs trees; no hardware access."""
from contextlib import contextmanager
from functools import partial
from multiprocessing import get_context
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

from energy_control.cpu_frequency import LenovoGb10CpuMaxima
from energy_control.fan import LenovoDgxFanFloor
from energy_control.gpu_recorder_channel import GpuRecorderClient
from energy_control.limit_evidence import (LimitEvidence, LimitEvidenceReader, limit_context,
                                           publish_limit_evidence)
from energy_control.limit_owner_process import (CpuLimitActuator, FanLimitActuator,
                                                LimitOwnerSession)
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID

SLOW = set(range(5)) | set(range(10, 15))


def make_cpufreq(root):
    for number in range(20):
        path = Path(root) / f"policy{number}"
        path.mkdir(parents=True)
        low, high = (338000, 2808000) if number in SLOW else (1378000, 3900000)
        for name, value in (("cpuinfo_min_freq", low), ("cpuinfo_max_freq", high),
                            ("scaling_min_freq", low), ("scaling_max_freq", high)):
            (path / name).write_text(f"{value}\n", encoding="ascii")
        (path / "scaling_governor").write_text("conservative\n", encoding="ascii")


def make_fan(root, state=12):
    device = Path(root) / "cooling_device4"
    device.mkdir(parents=True)
    (device / "type").write_text("dgx_ec_fan_floor\n", encoding="ascii")
    (device / "cur_state").write_text(f"{state}\n", encoding="ascii")
    (device / "max_state").write_text("12\n", encoding="ascii")
    return device


def cpu_maxima(root):
    values = {}
    for number in range(20):
        khz = int((Path(root) / f"policy{number}" / "scaling_max_freq").read_text())
        values.setdefault("slow" if number in SLOW else "fast", set()).add(khz // 1000)
    return values


@contextmanager
def fake_cpu_owner(root, ceilings, channel, run_id, boot_id, abort_event):
    client = GpuRecorderClient(channel, run_id=run_id, boot_id=boot_id)
    try:
        yield CpuLimitActuator(LenovoGb10CpuMaxima(cpufreq_root=root), client,
                               slow_max_mhz=ceilings[0], fast_max_mhz=ceilings[1])
    finally:
        client.close()


@contextmanager
def fake_fan_owner(root, channel, run_id, boot_id, abort_event):
    channel.close()  # The fan owner writes no durable intents.
    yield FanLimitActuator(LenovoDgxFanFloor(thermal_root=root))


class LimitEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.receive, self.send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.receive.close)
        self.addCleanup(self.send.close)
        self.context = limit_context(BOOT_ID, "cpu", "owner-a", "ab" * 16)
        self.reader = LimitEvidenceReader(self.receive, self.context)

    def proof(self, seq=1, requested=(2000, 2500, 2000, 2500), readback=None, age=0.0):
        now = monotonic() - age
        return LimitEvidence("cpu", requested, requested if readback is None else readback,
                             seq, now, now, BOOT_ID, "owner-a", "ab" * 16)

    def test_fresh_readback_is_accepted(self):
        publish_limit_evidence(self.send, self.proof())
        self.assertEqual(self.reader.read().requested, (2000, 2500, 2000, 2500))

    def test_readback_mismatch_faults(self):
        publish_limit_evidence(self.send, self.proof(readback=(2000, 2600)))
        self.assertIsNone(self.reader.read())
        self.assertTrue(self.reader.faulted)

    def test_stale_and_reordered_evidence_fault(self):
        from energy_control.limit_evidence import MAX_AGE_S
        publish_limit_evidence(self.send, self.proof(age=MAX_AGE_S["cpu"] + 0.5))
        self.assertIsNone(self.reader.read())
        reader = LimitEvidenceReader(self.receive, self.context)
        publish_limit_evidence(self.send, self.proof(seq=2))
        publish_limit_evidence(self.send, self.proof(seq=1))
        self.assertIsNone(reader.read())

    def test_guard_mode_holds_readback_through_a_command_but_expires(self):
        guard = LimitEvidenceReader(self.receive, self.context, hold_through_transition=True)
        publish_limit_evidence(self.send, self.proof())
        self.assertIsNotNone(guard.read())
        publish_limit_evidence(self.send, None)  # Command in flight.
        self.assertIsNotNone(guard.read())
        from energy_control.limit_evidence import MAX_AGE_S
        with patch("energy_control.limit_evidence.monotonic",
                   return_value=monotonic() + MAX_AGE_S["cpu"] + 0.1):
            self.assertIsNone(guard.read())  # A hung command still expires.
        self.assertFalse(self.reader._hold)  # Policy readers default to strict.

    def test_old_queued_frames_do_not_fault_when_the_newest_is_fresh(self):
        guard = LimitEvidenceReader(self.receive, self.context, max_backlog=64)
        completed = monotonic() - 3.0
        for observed in (monotonic() - 2.0, monotonic()):
            publish_limit_evidence(self.send, LimitEvidence(
                "cpu", (2000, 2500, 2000, 2500), (2000, 2500, 2000, 2500), 0, completed, observed,
                BOOT_ID, "owner-a", "ab" * 16))
        proof = guard.read()
        self.assertIsNotNone(proof)
        self.assertLess(monotonic() - proof.observed_monotonic_s, 0.5)

    def test_future_frame_faults(self):
        now = monotonic()
        publish_limit_evidence(self.send, LimitEvidence(
            "cpu", (2000, 2500, 2000, 2500), (2000, 2500, 2000, 2500), 1, now, now + 5.0, BOOT_ID, "owner-a", "ab" * 16))
        self.assertIsNone(self.reader.read())
        self.assertTrue(self.reader.faulted)

    def test_wrong_kind_or_run_faults(self):
        fan = LimitEvidence("fan", (6,), (6,), 1, monotonic(), monotonic(),
                            BOOT_ID, "owner-a", "ab" * 16)
        publish_limit_evidence(self.send, fan)
        self.assertIsNone(self.reader.read())


class LimitOwnerSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        (base / "runs").mkdir()
        self.cpufreq, self.thermal = base / "cpufreq", base / "thermal"
        make_cpufreq(self.cpufreq)
        self.fan = make_fan(self.thermal)
        self.recorder = CommissioningRecorder(base / "runs", boot_id=BOOT_ID)
        self.recorder.__enter__()
        self.recorder.write_trial_plan(plan())
        self.abort = get_context("spawn").Event()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            self.assertTrue(session.close())
            self.assertTrue(session.released)
        self.recorder.__exit__(None, None, None)

    def cpu(self, ceilings=(2808, 3900), **kwargs):
        session = LimitOwnerSession("cpu", self.recorder,
                                    partial(fake_cpu_owner, str(self.cpufreq), ceilings),
                                    self.abort, owner_epoch="cpu-owner-a", **kwargs)
        self.sessions.append(session)
        return session

    def fan_session(self, **kwargs):
        session = LimitOwnerSession("fan", self.recorder, partial(fake_fan_owner, str(self.thermal)),
                                    self.abort, owner_epoch="fan-owner-a", **kwargs)
        self.sessions.append(session)
        return session

    def wait_for(self, predicate, timeout=2.0):
        deadline = monotonic() + timeout
        while not predicate() and monotonic() < deadline:
            sleep(.02)
        return predicate()

    def records(self):
        path = next((Path(self.directory.name) / "runs").glob("*/events.jsonl"))
        return inspect_run(path)

    def test_cpu_commands_log_raise_intents_and_emergency_reaches_minimum(self):
        session = self.cpu()
        session.start()
        self.assertTrue(self.wait_for(lambda: session.read() is not None))
        self.assertEqual(session.read().requested, (2808, 3900, 2808, 3900))
        proof = session.apply((2000, 2500, 2000, 2500))  # Reduction: no intent needed.
        self.assertEqual(proof.readback, (2000, 2500, 2000, 2500))
        self.assertEqual(cpu_maxima(self.cpufreq), {"slow": {2000}, "fast": {2500}})
        session.apply((2400, 3000, 2400, 3000))  # Increase: synced intents first.
        run = self.records()
        intents = [r for r in run["records"] if r["kind"] == "intent"]
        self.assertEqual([(r["action"], r["requested_mhz"]) for r in intents],
                         [("raise_cpu_slow_cap", 2400), ("raise_cpu_fast_cap", 3000)])
        self.assertEqual(run["pending_intents"], {})
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)
        self.assertEqual(cpu_maxima(self.cpufreq), {"slow": {338}, "fast": {1378}})

    def test_cpu_clusters_are_capped_independently(self):
        # doc/48 §0, D7: P0 and P1 (and E0/E1) take separate caps.
        session = self.cpu()
        session.start()
        proof = session.apply((2000, 3000, 2400, 2500))
        self.assertEqual(proof.readback, (2000, 3000, 2400, 2500))
        values = {int(p.name[6:]): int((p / "scaling_max_freq").read_text()) // 1000
                  for p in self.cpufreq.iterdir()}
        self.assertEqual({values[i] for i in range(0, 5)}, {2000})
        self.assertEqual({values[i] for i in range(5, 10)}, {3000})
        self.assertEqual({values[i] for i in range(10, 15)}, {2400})
        self.assertEqual({values[i] for i in range(15, 20)}, {2500})
        session.apply((2000, 3000, 2400, 3100))   # raising P1 alone: one fast intent
        intents = [r for r in self.records()["records"] if r["kind"] == "intent"]
        self.assertEqual([(r["action"], r["requested_mhz"]) for r in intents][-1:],
                         [("raise_cpu_fast_cap", 3100)])
        self.assertTrue(session.close())

    def test_competing_cpu_writer_trips_abort_and_emergency(self):
        session = self.cpu()
        session.start()
        session.apply((2000, 2500, 2000, 2500))
        (self.cpufreq / "policy7" / "scaling_max_freq").write_text("3900000\n")
        self.assertTrue(self.wait_for(self.abort.is_set))
        self.assertIsNone(session.read())
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)
        self.assertEqual(cpu_maxima(self.cpufreq), {"slow": {338}, "fast": {1378}})

    def test_cpu_above_trial_ceiling_fails_closed(self):
        session = self.cpu(ceilings=(2000, 3000))
        session.start()
        with self.assertRaises(RuntimeError):
            session.apply((2400, 2500, 2400, 2500))
        self.assertTrue(self.abort.is_set())
        self.assertTrue(session.close())
        self.assertEqual(cpu_maxima(self.cpufreq), {"slow": {338}, "fast": {1378}})

    def test_fan_floor_command_and_emergency_floor_12(self):
        session = self.fan_session()
        session.start()
        self.assertEqual(session.apply((6,)).readback, (6,))
        self.assertEqual((self.fan / "cur_state").read_text(), "6\n")
        self.assertTrue(session.close())
        self.assertEqual(session.exitcode, 0)
        self.assertEqual((self.fan / "cur_state").read_text(), "12\n")

    def test_lost_supervisor_channel_triggers_emergency(self):
        cpu, fan = self.cpu(), self.fan_session()
        cpu.start()
        fan.start()
        cpu.apply((2000, 2500, 2000, 2500))
        fan.apply((6,))
        cpu._channel.close()  # Supervisor side vanished.
        self.assertTrue(self.wait_for(lambda: cpu._process.exitcode is not None, 4))
        self.assertTrue(self.wait_for(lambda: fan._process.exitcode is not None, 4))
        self.assertEqual((cpu.exitcode, fan.exitcode), (0, 0))  # Shared abort reached both.
        self.assertEqual(cpu_maxima(self.cpufreq), {"slow": {338}, "fast": {1378}})
        self.assertEqual((self.fan / "cur_state").read_text(), "12\n")

    def test_guard_receives_separate_cpu_feed(self):
        guard_read, guard_write = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(guard_read.close)
        session = self.cpu(guard_evidence=guard_write)
        guard = LimitEvidenceReader(guard_read, session.context)
        session.start()
        session.apply((2000, 2500, 2000, 2500))
        self.assertTrue(self.wait_for(lambda: (guard.read() or LimitEvidence(
            "cpu", (338, 1378, 338, 1378), (338, 1378, 338, 1378), 0, 0, 0, BOOT_ID, "x", "ab" * 16)).requested
            == (2000, 2500, 2000, 2500)))
        self.assertFalse(guard.faulted)

    def test_invalid_values_are_refused_before_send(self):
        session = self.cpu()
        session.start()
        for values in ((300, 2500), (2000, 4000), (2000,), (2000.0, 2500)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                session.apply(values)
        self.assertFalse(self.abort.is_set())


if __name__ == "__main__":
    unittest.main()
