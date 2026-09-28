from dataclasses import replace
from multiprocessing import get_context
from time import monotonic
import unittest

from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.guard_ownership_process import GuardOwnershipProcess
from functools import partial
GuardOwnershipProcess = partial(GuardOwnershipProcess, start_method="fork")  # legacy fake closures
from energy_control.safety import CommissioningGuard
from test_safety import good_snapshot
from test_guard_ownership_process import FakeAbort
import test_lifecycle


CONTEXT = ("boot-a", "driver-a", "owner-a", "ab" * 16)


def evidence(now=1.0):
    return GpuSetterEvidence(200, 1200, 2, 0, 0.5, now, *CONTEXT)


def snapshot(now=1.0, **changes):
    return good_snapshot(monotonic_s=now, gpu_accepted_max_mhz=None,
                         gpu_limit_age_s=None, gpu_setter_evidence=evidence(now), **changes)


def guard():
    return CommissioningGuard(gpu_evidence_mode="setter_monitor", gpu_setter_context=CONTEXT)


class SetterEvidenceTests(unittest.TestCase):
    def test_explicit_mode_preserves_unknown_readback_and_clone(self):
        self.assertFalse(guard().evaluate(snapshot()).abort)
        self.assertFalse(guard().clone_unlatched().evaluate(snapshot()).abort)
        self.assertTrue(CommissioningGuard().evaluate(snapshot()).abort)
        self.assertTrue(guard().evaluate(replace(snapshot(), gpu_accepted_max_mhz=1200)).abort)

    def test_failed_stale_mismatched_or_forged_numeric_fields_abort(self):
        for change in ({"exit_code": 1}, {"exit_code": False}, {"intent_seq": 0},
                       {"requested_max_mhz": 2000}, {"requested_max_mhz": 1100},
                       {"requested_min_mhz": -1}, {"owner_epoch": "other"},
                       {"driver_epoch": "reset"}, {"boot_id": "new-boot"},
                       {"run_id": "cd" * 16}, {"ownership_checked_monotonic_s": 0.4},
                       {"ownership_checked_monotonic_s": 1.1},
                       {"completed_monotonic_s": 1.1}):
            with self.subTest(change=change):
                self.assertTrue(guard().evaluate(replace(
                    snapshot(), gpu_setter_evidence=replace(evidence(), **change))).abort)

    def test_measured_violation_staleness_temperature_and_fault_latch_remain(self):
        for change in ({"gpu_measured_mhz": 1201}, {"gpu_measured_mhz": 1801},
                       {"gpu_clock_age_s": 1.1}, {"fan_healthy": False},
                       {"available_memory_bytes": 1}, {"gpu_setter_evidence": None}):
            controller = guard()
            # Measured-clock violations count once the clock sample is 5 s past
            # completion (at 0.5 s); the other faults are immediate.
            self.assertTrue(controller.evaluate(replace(snapshot(5.8), **change)).abort)
            self.assertTrue(controller.evaluate(snapshot(6.0)).abort)

    def test_old_setter_completion_needs_fresh_ownership_not_repeated_writes(self):
        self.assertFalse(guard().evaluate(snapshot(100)).abort)

    def test_child_process_uses_explicit_mode_without_numeric_readback(self):
        callback = FakeAbort(get_context("fork"))
        process = GuardOwnershipProcess(callback, lambda _: True,
            lambda: snapshot(monotonic()), run_id=CONTEXT[3],
            gpu_evidence_mode="setter_monitor", gpu_setter_context=CONTEXT)
        process.start()
        self.assertTrue(process.heartbeat(CONTEXT[3]))
        self.assertTrue(process.disarm())
        process.join(timeout_s=1)
        self.assertEqual(process.exitcode, 0)


class SetterLifecycleTests(unittest.TestCase):
    def test_setter_evidence_arms_and_identity_loss_faults(self):
        # Reuse the fake-only fixture, not its test methods.
        fixture = test_lifecycle.LifecycleTests()
        fixture.setUp()
        fixture.lifecycle.abort.guard = guard()
        fixture.reader.reading = evidence()
        self.assertTrue(fixture.arm(snapshot()).armed)
        fixture.now[0] = 2
        fixture.reader.reading = evidence(2)
        self.assertTrue(fixture.lifecycle.observe(snapshot(2), boot_id=CONTEXT[0],
                        driver_epoch=CONTEXT[1], owner_epoch=CONTEXT[2]).armed)
        fixture.now[0] = 3
        changed = fixture.lifecycle.observe(snapshot(3), boot_id=CONTEXT[0],
                        driver_epoch="reset", owner_epoch=CONTEXT[2])
        self.assertEqual(changed.state, "FAULT")
