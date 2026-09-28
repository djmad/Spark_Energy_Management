from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest

from energy_control.gpu_command import LoggedGpuClockSetter
from energy_control.gpu_ownership import GpuOwnershipMonitor, OwnershipObservation
from energy_control.run_session import CommissioningRunSession
from test_gpu_command import FakeProcess
from test_gpu_setter_recorder import plan
from test_recorder import BOOT_ID


@unittest.skipUnless(os.geteuid() == 0, "requires root-owned temporary evidence")
class GpuOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.session = CommissioningRunSession(Path(temporary.name), boot_id=BOOT_ID)
        self.addCleanup(self.session.close)
        self.now = 10.0
        self.observation = OwnershipObservation(BOOT_ID, "driver-a", self.session.owner_epoch,
            self.session.recorder.run_id, self.now, True, True, True)

    def monitor(self):
        return GpuOwnershipMonitor(self.session, driver_epoch="driver-a",
            read_observation=lambda: self.observation, clock=lambda: self.now)

    def test_never_refreshes_timestamp_from_cached_source(self):
        monitor = self.monitor()
        self.assertEqual(monitor.read().observed_monotonic_s, 10)
        self.now = 10.6
        self.assertIsNone(monitor.read())
        self.observation = replace(self.observation, observed_monotonic_s=self.now)
        self.assertIsNone(monitor.read())
        self.assertTrue(monitor.faulted)

    def test_missing_handoff_reset_or_matching_identity_fails_closed(self):
        original = self.observation
        for field, value in (("handoff_verified", False), ("competing_writers_fenced", 1),
                             ("reset_watch_healthy", False), ("driver_epoch", "reset"),
                             ("boot_id", "other"), ("owner_epoch", "other"),
                             ("run_id", "00" * 16), ("observed_monotonic_s", 11),
                             ("observed_monotonic_s", float("nan"))):
            with self.subTest(field=field, value=value):
                monitor = self.monitor()
                self.observation = replace(original, **{field: value})
                self.assertIsNone(monitor.read())
                self.observation = original
                self.assertIsNone(monitor.read())

    def test_lease_loss_during_source_read(self):
        def source():
            (self.session.parent / "commissioning.lock").chmod(0o640)
            return self.observation
        monitor = GpuOwnershipMonitor(self.session, driver_epoch="driver-a",
                                       read_observation=source, clock=lambda: self.now)
        self.assertIsNone(monitor.read())
        self.assertTrue(monitor.faulted)

    def test_setter_faults_without_another_command_after_reset(self):
        self.session.recorder.write_trial_plan(plan())
        reset = [False]
        def source():
            return replace(self.observation, observed_monotonic_s=monotonic(),
                           reset_watch_healthy=not reset[0])
        monitor = GpuOwnershipMonitor(self.session, driver_epoch="driver-a", read_observation=source)
        calls, faults = [], []
        def factory(*args, **kwargs):
            calls.append(args)
            return FakeProcess([0])
        setter = LoggedGpuClockSetter(self.session.recorder, driver_epoch="driver-a",
            owner_epoch=self.session.owner_epoch, read_ownership=monitor.read,
            process_factory=factory, on_fault=faults.append)
        setter.apply(minimum_mhz=200, maximum_mhz=1200)
        self.assertIsNotNone(setter.read())
        reset[0] = True
        self.assertIsNone(setter.read())
        self.assertEqual(len(faults), 1)
        with self.assertRaises(RuntimeError):
            setter.apply(minimum_mhz=200, maximum_mhz=1300)
        self.assertEqual(len(calls), 1)
