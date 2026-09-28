from pathlib import Path
import json
from dataclasses import replace
from time import monotonic, monotonic_ns
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from energy_control.gpu_command import LoggedGpuClockSetter
from energy_control.gpu_evidence import GpuOwnershipReading
from energy_control.recorder import CommissioningRecorder, inspect_run
from energy_control.safety import CommissioningGuard
from energy_control.broker import Config
from energy_control.replay import replay_run
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID, record
from test_safety import good_snapshot
from multiprocessing import get_context
from energy_control.process_http_transport import GuardHttpCancellation


def guard_cancel(event):
    GuardHttpCancellation(event)((), "test abort")


class FakeProcess:
    def __init__(self, replies):
        self.replies = list(replies)
        self.killed = False

    def wait(self, timeout):
        value = self.replies.pop(0)
        if value == "timeout":
            raise subprocess.TimeoutExpired("fake", timeout)
        return value

    def kill(self):
        self.killed = True


def ownership_reader(recorder):
    return lambda: GpuOwnershipReading(recorder.boot_id, "driver-a", "owner-a",
                                        recorder.run_id, monotonic(), True)


class GpuCommandTests(unittest.TestCase):
    def test_emergency_uses_same_writer_after_inflight_normal_command(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                commands = []
                def factory(argv, **kwargs):
                    commands.append(argv[-1])
                    if len(commands) == 1:
                        # Simulate guard arrival during the normal command.
                        with self.assertRaises(RuntimeError):
                            setter.apply_emergency()
                        self.assertFalse(setter.normal_writer_quiescent())
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=factory, read_ownership=ownership_reader(recorder))
                with self.assertRaises(RuntimeError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertFalse(setter.faulted)
                self.assertTrue(setter.normal_writer_quiescent())
                result = setter.apply_emergency()
                self.assertEqual(result.status, "success")
                self.assertTrue(result.process_reaped)
                self.assertIsNone(setter.read())  # No normal admission proof after abort.
                with self.assertRaises(RuntimeError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1200)
                with self.assertRaises(RuntimeError):
                    setter.apply_emergency()
                self.assertEqual(commands, ["--lock-gpu-clocks=200,1200",
                                            "--lock-gpu-clocks=200,500"])

    def test_spawned_guard_fences_normal_gpu_setter(self):
        context = get_context("spawn")
        fence = context.Event()
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    normal_fence=fence, process_factory=lambda *a, **k: self.fail("command issued"),
                    read_ownership=ownership_reader(recorder))
                guard = context.Process(target=guard_cancel, args=(fence,))
                guard.start()
                guard.join(2)
                self.assertEqual(guard.exitcode, 0)
                self.assertTrue(setter.normal_writer_quiescent())
                with self.assertRaises(RuntimeError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertIsNone(setter.read())

    def test_normal_fence_prevents_rewrite_and_waits_for_inflight_completion(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                issued = []
                def factory(*args, **kwargs):
                    issued.append(args)
                    setter.fence_normal_writes()
                    self.assertFalse(setter.normal_writer_quiescent())
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=factory, read_ownership=ownership_reader(recorder))
                self.assertFalse(setter.normal_writer_quiescent())
                with self.assertRaises(RuntimeError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertTrue(setter.normal_writer_quiescent())
                self.assertIsNone(setter.read())
                with self.assertRaises(RuntimeError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1500)
                self.assertEqual(len(issued), 1)

    def test_sample_receipt_must_reference_successful_logged_command(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=lambda *a, **k: FakeProcess([0]), read_ownership=ownership_reader(recorder))
                setter.apply(minimum_mhz=200, maximum_mhz=1200)
                proof = setter.read()
                sample = record(gpu_accepted_mhz=None, gpu_limit_age_s=None, gpu_setter_evidence=proof,
                                sample_mono_ns=monotonic_ns())
                recorder.write_sample(sample)
                with self.assertRaises(ValueError):
                    recorder.write_sample(replace(sample, gpu_setter_evidence=replace(proof, intent_seq=1)))
                path = Path(directory) / recorder.run_id / "events.jsonl"
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[-1]["gpu_setter_evidence"]["intent_seq"] = 1
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            self.assertTrue(inspect_run(path)["corrupt_record"])

    def test_receipt_refresh_does_not_repeat_write_and_owner_loss_latches(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                owner = ownership_reader(recorder)
                lost = [False]
                calls, faults = [], []
                def read_owner():
                    return replace(owner(), exclusive=not lost[0])
                def factory(*args, **kwargs):
                    calls.append(args)
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=factory, read_ownership=read_owner, on_fault=faults.append)
                self.assertIsNone(setter.read())
                setter.apply(minimum_mhz=200, maximum_mhz=1200)
                first, second = setter.read(), setter.read()
                self.assertEqual(first.completed_monotonic_s, second.completed_monotonic_s)
                self.assertEqual(first.intent_seq, second.intent_seq)
                self.assertGreaterEqual(second.ownership_checked_monotonic_s,
                                        first.ownership_checked_monotonic_s)
                guard = CommissioningGuard(gpu_evidence_mode="setter_monitor",
                    gpu_setter_context=(recorder.boot_id, "driver-a", "owner-a", recorder.run_id))
                self.assertFalse(guard.evaluate(good_snapshot(monotonic_s=monotonic(),
                    gpu_accepted_max_mhz=None, gpu_limit_age_s=None, gpu_setter_evidence=second)).abort)
                recorder.write_sample(record(gpu_accepted_mhz=None, gpu_limit_age_s=None,
                    gpu_setter_evidence=second, sample_mono_ns=monotonic_ns(), gpu_clock_age_s=0.1,
                    fan_healthy=True, cpu_actuator_healthy=True, gpu_actuator_healthy=True,
                    workload_control_healthy=True))
                path = Path(directory) / recorder.run_id / "events.jsonl"
                context = (recorder.boot_id, "driver-a", "owner-a", recorder.run_id)
                replay = replay_run(path, Config(), gpu_evidence_mode="setter_monitor", gpu_setter_context=context)
                self.assertFalse(replay.corrupt_record)
                self.assertEqual(len(replay.points), 1)
                self.assertFalse(replay.points[0].limits.abort_owned_loads)
                self.assertTrue(replay_run(path, Config()).points[0].limits.abort_owned_loads)
                self.assertIsNone(inspect_run(path)["records"][-1]["gpu_accepted_mhz"])
                lost[0] = True
                self.assertIsNone(setter.read())
                lost[0] = False
                self.assertIsNone(setter.read())
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(faults), 1)

    def test_ownership_failure_before_or_after_command_refuses_evidence(self):
        for failed_check in (1, 2, 3):
            with TemporaryDirectory() as directory:
                with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                    recorder.write_trial_plan(plan())
                    owner = ownership_reader(recorder)
                    checks, calls = [], []
                    def read_owner():
                        checks.append(1)
                        return replace(owner(), owner_epoch="other") if len(checks) == failed_check else owner()
                    def factory(*args, **kwargs):
                        calls.append(args)
                        return FakeProcess([0])
                    setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                                                  process_factory=factory, read_ownership=read_owner)
                    with self.assertRaises(RuntimeError):
                        setter.apply(minimum_mhz=200, maximum_mhz=1200)
                    self.assertEqual(len(calls), int(failed_check == 3))
                    self.assertIsNone(setter.read())
                    self.assertTrue(setter.faulted)

    def test_outcome_sync_failure_cannot_publish_receipt(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=lambda *a, **k: FakeProcess([0]), read_ownership=ownership_reader(recorder))
                with patch.object(recorder, "write_gpu_setter_outcome", side_effect=OSError("fake sync failure")):
                    with self.assertRaises(OSError):
                        setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertIsNone(setter.read())
                self.assertTrue(setter.faulted)

    def test_fixed_command_follows_synced_intent(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                path = Path(directory) / recorder.run_id / "events.jsonl"
                def factory(argv, **kwargs):
                    self.assertEqual(inspect_run(path)["pending_intents"], {3: "gpu_setter"})
                    self.assertEqual(argv, ["/usr/bin/nvidia-smi", "-i", "0", "--lock-gpu-clocks=200,1200"])
                    self.assertIs(kwargs["shell"], False)
                    self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
                    self.assertEqual(kwargs["env"], {"LC_ALL": "C"})
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a",
                                              owner_epoch="owner-a", process_factory=factory,
                                              read_ownership=ownership_reader(recorder))
                result = setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertEqual(result.status, "success")
                self.assertTrue(result.process_reaped)
                self.assertFalse(setter.faulted)
                self.assertEqual(inspect_run(path)["pending_intents"], {})

    def test_failure_and_timeout_poison_runner_without_retry(self):
        for replies, status, pending in (([1], "failed", False),
                                        (["timeout", -9], "timeout", False),
                                        (["timeout", "timeout"], "timeout", True)):
            with TemporaryDirectory() as directory:
                with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                    recorder.write_trial_plan(plan())
                    process = FakeProcess(replies)
                    calls = []
                    faults = []
                    def factory(*args, **kwargs):
                        calls.append(args)
                        return process
                    setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a",
                                                  owner_epoch="owner-a", process_factory=factory,
                                                  on_fault=faults.append, read_ownership=ownership_reader(recorder))
                    result = setter.apply(minimum_mhz=200, maximum_mhz=1200)
                    self.assertEqual(result.status, status)
                    self.assertEqual(setter.process_pending, pending)
                    self.assertTrue(setter.faulted)
                    self.assertEqual(process.killed, status == "timeout")
                    with self.assertRaises(RuntimeError):
                        setter.apply(minimum_mhz=200, maximum_mhz=1200)
                    with self.assertRaises(RuntimeError):
                        setter.apply_emergency()
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(len(faults), 1)

    def test_disabled_or_invalid_request_never_spawns(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                with patch("energy_control.gpu_command.subprocess.Popen") as spawn:
                    setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a")
                    with self.assertRaises(PermissionError):
                        setter.apply(minimum_mhz=200, maximum_mhz=1200)
                    spawn.assert_not_called()
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                                              process_factory=lambda *a, **k: self.fail("spawned"),
                                              read_ownership=ownership_reader(recorder))
                with self.assertRaises(ValueError):
                    setter.apply(minimum_mhz=200, maximum_mhz=2000)

    def test_failed_intent_sync_never_spawns(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                                              process_factory=lambda *a, **k: self.fail("spawned"),
                                              read_ownership=ownership_reader(recorder))
                with patch("energy_control.recorder.os.fdatasync", side_effect=OSError("fake full disk")):
                    with self.assertRaises(OSError):
                        setter.apply(minimum_mhz=200, maximum_mhz=1200)
                self.assertTrue(setter.faulted)


class EmergencyWithoutDiskTests(unittest.TestCase):
    """Goal v2: intent-first guards increases; a protective reduction must not
    depend on a live recorder or supervisor."""

    def test_emergency_proceeds_when_recorder_is_unreachable(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())
                commands = []
                def factory(argv, **kwargs):
                    commands.append(argv[-1])
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=factory, read_ownership=ownership_reader(recorder))
                self.assertEqual(setter.apply(minimum_mhz=200, maximum_mhz=1200).status, "success")
                with patch.object(recorder, "write_gpu_setter_intent",
                                  side_effect=RuntimeError("supervisor gone")):
                    result = setter.apply_emergency()
                self.assertEqual((result.status, result.intent_seq, result.process_reaped),
                                 ("success", 0, True))
                self.assertEqual(commands, ["--lock-gpu-clocks=200,1200", "--lock-gpu-clocks=200,500"])

    def test_recorder_refusal_before_spawn_still_allows_one_emergency(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                recorder.write_trial_plan(plan())  # Plan ceiling 1200 MHz.
                commands = []
                def factory(argv, **kwargs):
                    commands.append(argv[-1])
                    return FakeProcess([0])
                setter = LoggedGpuClockSetter(recorder, driver_epoch="driver-a", owner_epoch="owner-a",
                    process_factory=factory, read_ownership=ownership_reader(recorder))
                with self.assertRaises(ValueError):
                    setter.apply(minimum_mhz=200, maximum_mhz=1300)
                self.assertTrue(setter.faulted)
                self.assertEqual(commands, [])  # Nothing reached the driver.
                self.assertEqual(setter.apply_emergency().status, "success")
                self.assertEqual(commands, ["--lock-gpu-clocks=200,500"])
                with self.assertRaises(RuntimeError):
                    setter.apply_emergency()  # Still exactly one emergency request.
