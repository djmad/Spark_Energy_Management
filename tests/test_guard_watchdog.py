from dataclasses import replace
from time import sleep
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.admission import AdmissionClosed, OwnedRequestGate
from energy_control.guard_watchdog import GuardFrame, GuardWatchdog
from energy_control.lifecycle import CommissioningLifecycle
from energy_control.owned_process import LocalOwnedWorkloads
from energy_control.run_catalog import RunCatalogResult
from test_lifecycle import FakeGpuLimitReader, FakeIndependentGuard, RUN_ID
from test_safety import good_snapshot


class GuardWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.now = [1.1]
        self.gate = OwnedRequestGate()
        control = LocalOwnedWorkloads(
            [], close_admission=self.gate.close_admission,
            cancel_owned_requests=self.gate.cancel_owned_requests,
            verify_admission_and_requests=self.gate.verify_admission_and_requests)
        self.lifecycle = CommissioningLifecycle(
            self.gate, AbortCoordinator(control), clock=lambda: self.now[0],
            gpu_limit_reader=FakeGpuLimitReader(),
            independent_guard=FakeIndependentGuard())
        self.assertTrue(self.lifecycle.arm(
            good_snapshot(), boot_id="boot-a", driver_epoch="driver-a",
            owner_epoch="owner-a", run_id=RUN_ID,
            previous_run=RunCatalogResult("none", 0, (), ()),
            durable_log_ready=True, sensor_latency_qualified=True).armed)
        self.initial = GuardFrame(good_snapshot(), "boot-a", "driver-a", "owner-a")

    def wait_for(self, state):
        for _ in range(100):
            if self.lifecycle.state == state:
                return
            sleep(0.005)
        self.fail(f"lifecycle did not reach {state}")

    def test_missing_new_sample_trips_abort_without_policy_step(self):
        watchdog = GuardWatchdog(self.lifecycle, self.initial, poll_s=0.01)
        watchdog.start()
        self.now[0] = 1.6
        self.wait_for("FAULT")
        watchdog.stop()
        self.assertIn("timeline stale", watchdog.result.reasons[0])
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)

    def test_mismatched_initial_identity_fails_closed(self):
        watchdog = GuardWatchdog(self.lifecycle,
                                 replace(self.initial, driver_epoch="driver-b"))
        with self.assertRaises(RuntimeError):
            watchdog.start()
        self.assertEqual(self.lifecycle.state, "FAULT")

    def test_fresh_frame_can_be_processed_but_stopping_guard_trips(self):
        watchdog = GuardWatchdog(self.lifecycle, self.initial, poll_s=0.01)
        watchdog.start()
        self.now[0] = 1.3
        watchdog.publish(GuardFrame(replace(good_snapshot(), monotonic_s=1.2),
                                    "boot-a", "driver-a", "owner-a"))
        for _ in range(100):
            if watchdog.result is not None:
                break
            sleep(0.005)
        self.assertEqual(watchdog.result.state, "ARMED")
        watchdog.stop()
        self.assertEqual(self.lifecycle.state, "FAULT")


if __name__ == "__main__":
    unittest.main()
