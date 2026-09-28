from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.admission import AdmissionClosed, OwnedRequestGate
from energy_control.lifecycle import CommissioningLifecycle, GpuLimitReading
from energy_control.owned_process import LocalOwnedWorkloads
from energy_control.request_gateway import OwnedRequestDispatcher
from energy_control.recorder import CommissioningRecorder
from energy_control.run_catalog import CommissioningRunCatalog, RunCatalogResult
from energy_control.run_session import CommissioningRunSession
from energy_control.safety import Temperature
from test_request_gateway import FakeHandle, FakeRecorder, FakeTransport
from test_safety import good_snapshot
from test_recorder import mark_terminal


RUN_ID = "ab" * 16


class FakeGpuLimitReader:
    def __init__(self):
        self.reading = GpuLimitReading(1200, 1.0, "boot-a", "driver-a", "owner-a")

    def read(self):
        return self.reading


class FakeIndependentGuard:
    def __init__(self, run_id=RUN_ID):
        self.alive = True
        self.run_id = run_id

    def heartbeat(self, run_id):
        return self.alive and run_id == self.run_id

    def verify_trial(self, run_id, proposal):
        self.checked_trial = (run_id, proposal)
        return self.heartbeat(run_id)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.gate = OwnedRequestGate()
        control = LocalOwnedWorkloads(
            [], close_admission=self.gate.close_admission,
            cancel_owned_requests=self.gate.cancel_owned_requests,
            verify_admission_and_requests=self.gate.verify_admission_and_requests)
        self.now = [1.1]
        self.reader = FakeGpuLimitReader()
        self.independent_guard = FakeIndependentGuard()
        self.lifecycle = CommissioningLifecycle(
            self.gate, AbortCoordinator(control), clock=lambda: self.now[0],
            gpu_limit_reader=self.reader, independent_guard=self.independent_guard)

    def arm(self, snapshot=None, **changes):
        inputs = {"boot_id": "boot-a", "driver_epoch": "driver-a",
                  "owner_epoch": "owner-a",
                  "run_id": RUN_ID,
                  "previous_run": RunCatalogResult("none", 0, (), ()),
                  "durable_log_ready": True,
                  "sensor_latency_qualified": True}
        inputs.update(changes)
        return self.lifecycle.arm(snapshot or good_snapshot(), **inputs)

    def test_boot_closed_then_qualified_one_shot_arm(self):
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)
        self.reader.reading = None
        self.assertFalse(self.arm().armed)
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)
        self.reader.reading = GpuLimitReading(1200, 1.0, "boot-a", "driver-a", "owner-a")
        result = self.arm()
        self.assertTrue(result.armed)
        request = self.gate.register(lambda: None)
        self.assertFalse(self.arm().armed)
        self.gate.mark_terminal(request)

    def test_no_independent_guard_cannot_arm_and_loss_trips(self):
        self.independent_guard.alive = False
        self.assertEqual(self.arm().reasons, ("independent guard unavailable",))
        self.independent_guard.alive = True
        self.assertTrue(self.arm().armed)
        self.independent_guard.alive = False
        result = self.lifecycle.observe(good_snapshot(monotonic_s=1.1),
                                        boot_id="boot-a", driver_epoch="driver-a",
                                        owner_epoch="owner-a")
        self.assertEqual(result.state, "FAULT")
        self.assertIn("independent guard heartbeat lost", result.reasons)

    def test_unclean_prior_run_requires_manual_new_run(self):
        result = self.arm(previous_run=RunCatalogResult("unclean", 1, ("a" * 32,),
                                                       ("unverified or incomplete run",)))
        self.assertEqual(result.state, "FAULT")
        self.assertFalse(result.armed)
        self.assertFalse(self.arm().armed)
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)

    def test_stale_or_unsafe_bootstrap_can_be_rechecked_without_dispatch(self):
        self.now[0] = 1.7
        self.assertEqual(self.arm().state, "BOOTSTRAP")
        self.now[0] = 1.1
        unsafe = good_snapshot(gpu_accepted_max_mhz=None)
        self.assertEqual(self.arm(unsafe).state, "BOOTSTRAP")
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)
        self.assertTrue(self.arm().armed)

    def test_pinned_sensor_loss_blocks_arm_and_trips_running_guard(self):
        missing = good_snapshot(temperatures=tuple(
            value for value in good_snapshot().temperatures
            if value.name != "acpi_tgpu"))
        self.assertEqual(self.arm(missing).state, "BOOTSTRAP")
        self.assertTrue(self.arm().armed)
        self.now[0] = 1.3
        result = self.lifecycle.observe(replace(missing, monotonic_s=1.2),
                                        boot_id="boot-a", driver_epoch="driver-a",
                                        owner_epoch="owner-a")
        self.assertEqual(result.state, "FAULT")
        self.assertTrue(result.abort.verified_quiescent)

    def test_numeric_reader_must_match_limit_epoch_and_freshness(self):
        self.reader.reading = GpuLimitReading(1801, 1.0, "boot-a", "driver-a", "owner-a")
        self.assertFalse(self.arm().armed)
        self.reader.reading = GpuLimitReading(1200, 1.0, "boot-a", "driver-b", "owner-a")
        self.assertFalse(self.arm().armed)
        self.reader.reading = GpuLimitReading(1700, 1.0, "boot-a", "driver-a", "owner-a")
        self.assertFalse(self.arm().armed)
        self.reader.reading = GpuLimitReading(1200, 0.1, "boot-a", "driver-a", "owner-a")
        self.assertFalse(self.arm().armed)
        self.reader.reading = GpuLimitReading(1200, 1.0, "boot-a", "driver-a", "owner-a")
        self.assertTrue(self.arm().armed)
        self.reader.reading = None
        self.now[0] = 1.2
        result = self.lifecycle.observe(replace(good_snapshot(), monotonic_s=1.2),
                                        boot_id="boot-a", driver_epoch="driver-a",
                                        owner_epoch="owner-a")
        self.assertEqual(result.state, "FAULT")
        self.assertIn("numeric GPU limit", result.reasons[0])

    def test_no_numeric_reader_cannot_arm(self):
        self.lifecycle.gpu_limit_reader = None
        result = self.arm()
        self.assertEqual(result.state, "BOOTSTRAP")
        self.assertFalse(result.armed)
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)

    def test_unknown_sensor_internal_latency_cannot_arm(self):
        result = self.arm(sensor_latency_qualified=False)
        self.assertEqual(result.state, "BOOTSTRAP")
        self.assertFalse(result.armed)
        self.assertIn("sensor latency", result.reasons[0])
        self.assertTrue(self.arm().armed)

    def test_driver_change_or_resume_latches_abort_and_closes_admission(self):
        self.assertTrue(self.arm().armed)
        holder = []
        holder.append(self.gate.register(lambda: self.gate.mark_terminal(holder[0])))
        self.now[0] = 1.3
        sample = replace(good_snapshot(), monotonic_s=1.2)
        result = self.lifecycle.observe(sample, boot_id="boot-a",
                                        driver_epoch="driver-b", owner_epoch="owner-a")
        self.assertEqual(result.state, "FAULT")
        self.assertTrue(result.abort.verified_quiescent)
        with self.assertRaises(AdmissionClosed):
            self.gate.register(lambda: None)
        self.assertEqual(self.lifecycle.resume_or_reset().state, "FAULT")
        self.assertFalse(self.arm().armed)

    def test_temperature_breach_and_timeline_stall_abort(self):
        self.assertTrue(self.arm().armed)
        self.now[0] = 1.3
        hot = replace(good_snapshot(), monotonic_s=1.2, temperatures=tuple(
            Temperature(value.name, 93 if value.name == "gpu" else value.celsius, 0)
            for value in good_snapshot().temperatures))
        result = self.lifecycle.observe(hot, boot_id="boot-a",
                                        driver_epoch="driver-a", owner_epoch="owner-a")
        self.assertEqual(result.state, "FAULT")
        self.assertTrue(result.abort.decision.abort)

        other = LifecycleTests()
        other.setUp()
        self.assertTrue(other.arm().armed)
        other.now[0] = 2.0
        result = other.lifecycle.observe(replace(good_snapshot(), monotonic_s=1.2),
                                         boot_id="boot-a", driver_epoch="driver-a",
                                         owner_epoch="owner-a")
        self.assertIn("timeline stale", result.reasons[0])

    def test_lifecycle_arms_fake_dispatcher_then_reset_cancels_it(self):
        handle = FakeHandle()
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            terminal_timeout_s=0.5)
        control = LocalOwnedWorkloads(
            [], close_admission=dispatcher.close_admission,
            cancel_owned_requests=dispatcher.cancel_owned_requests,
            verify_admission_and_requests=dispatcher.verify_admission_and_requests)
        lifecycle = CommissioningLifecycle(dispatcher, AbortCoordinator(control),
                                           clock=lambda: 1.1,
                                           gpu_limit_reader=FakeGpuLimitReader(),
                                           independent_guard=FakeIndependentGuard())
        with self.assertRaises(AdmissionClosed):
            dispatcher.submit(object())
        result = lifecycle.arm(good_snapshot(), boot_id="boot-a", driver_epoch="driver-a",
                               owner_epoch="owner-a", run_id=RUN_ID,
                               previous_run=RunCatalogResult("none", 0, (), ()),
                               durable_log_ready=True, sensor_latency_qualified=True)
        self.assertTrue(result.armed)
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        aborted = lifecycle.resume_or_reset()
        self.assertEqual(aborted.state, "FAULT")
        self.assertTrue(aborted.abort.verified_quiescent)
        self.assertTrue(handle.cancelled.is_set())
        self.assertTrue(dispatcher.join_workers(0.5))

    @unittest.skipUnless(os.geteuid() == 0, "catalog requires root-owned test evidence")
    def test_catalog_result_feeds_preflight_and_unclean_next_run_blocks(self):
        with TemporaryDirectory() as directory:
            parent = Path(directory)
            with CommissioningRecorder(parent,
                    boot_id="00000000-0000-0000-0000-000000000001") as recorder:
                mark_terminal(recorder)
                recorder.close(clean=True)
            from energy_control.run_review_receipt import record_clean_review
            record_clean_review(parent, recorder.run_id, reviewer="test-operator")
            catalog = CommissioningRunCatalog(parent)
            with CommissioningRunSession(
                    parent, boot_id="00000000-0000-0000-0000-000000000001") as session:
                self.assertEqual(session.prior.previous_run, "clean")
                self.assertTrue(self.arm(previous_run=session.prior).armed)
            self.assertEqual(catalog.inspect().previous_run, "unclean")


if __name__ == "__main__":
    unittest.main()
