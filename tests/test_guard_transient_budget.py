"""Periodic guard acquisition tolerance: count-bounded near limits, time-bounded far away."""
from dataclasses import replace
from time import monotonic
import unittest

from energy_control.guard_ownership_process import (ACQUISITION_FAILED, TRANSIENT_GRACE_S,
                                                    _TransientBudget, _sample_fault)
from energy_control.safety import CommissioningGuard
from test_guard_ownership_process import good_snapshot


def hot(snapshot, celsius):
    return replace(snapshot, temperatures=tuple(replace(t, celsius=celsius)
                                                for t in snapshot.temperatures))


class TransientBudgetTests(unittest.TestCase):
    def test_far_from_limits_tolerates_a_gap_up_to_the_grace(self):
        budget = _TransientBudget()
        budget.good(hot(good_snapshot(monotonic_s=0), 60.0))
        t = 100.0
        for i in range(8):  # 0.7 s of 0.1 s checks
            self.assertFalse(budget.exhausted(t + 0.1 * i))
        self.assertTrue(budget.exhausted(t + TRANSIENT_GRACE_S))

    def test_near_a_limit_three_failures_still_abort(self):
        budget = _TransientBudget()
        budget.good(hot(good_snapshot(monotonic_s=0), 75.0))  # GPU 10 C below 85, ACPI 18 below
        budget.good(hot(good_snapshot(monotonic_s=0), 84.0))
        self.assertFalse(budget.exhausted(1.0))
        self.assertFalse(budget.exhausted(1.1))
        self.assertTrue(budget.exhausted(1.2))

    def test_no_good_sample_yet_uses_the_count_rule(self):
        budget = _TransientBudget()
        self.assertFalse(budget.exhausted(0.0))
        self.assertFalse(budget.exhausted(0.1))
        self.assertTrue(budget.exhausted(0.2))

    def test_a_good_sample_resets_the_gap(self):
        budget = _TransientBudget()
        budget.good(hot(good_snapshot(monotonic_s=0), 60.0))
        for i in range(5):
            budget.exhausted(0.1 * i)
        budget.good(hot(good_snapshot(monotonic_s=0), 60.0))
        self.assertEqual((budget.failures, budget.first_s), (0, None))

    def test_acquisition_fault_names_its_cause(self):
        def broken():
            raise RuntimeError("isolated snapshot delivery stale")
        fault = _sample_fault(CommissioningGuard(), broken, 0.1)
        self.assertTrue(fault.startswith(ACQUISITION_FAILED))
        self.assertIn("RuntimeError: isolated snapshot delivery stale", fault)

    def test_good_sample_is_reported(self):
        seen = []
        fault = _sample_fault(CommissioningGuard(),
                              lambda: good_snapshot(monotonic_s=monotonic()), 0.1,
                              on_good=seen.append)
        self.assertIsNone(fault)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()


class NearLimitGraceTests(unittest.TestCase):
    """Defect 29: 5-10 C from the nearest abort a 0.6 s grace applies."""

    def test_operating_point_tolerates_one_late_frame(self):
        from energy_control.guard_ownership_process import TRANSIENT_NEAR_GRACE_S
        budget = _TransientBudget()
        budget.good(hot(good_snapshot(monotonic_s=0), 78.0))   # GPU 7 C below 85, ACPI 15 below
        for i in range(6):                                     # 0.5 s of 0.1 s checks
            self.assertFalse(budget.exhausted(10.0 + 0.1 * i))
        self.assertTrue(budget.exhausted(10.0 + TRANSIENT_NEAR_GRACE_S + 0.01))

    def test_within_five_degrees_there_is_no_grace(self):
        budget = _TransientBudget()
        budget.good(hot(good_snapshot(monotonic_s=0), 80.5))   # GPU 4.5 C below 85
        self.assertFalse(budget.exhausted(1.0))
        self.assertFalse(budget.exhausted(1.1))
        self.assertTrue(budget.exhausted(1.2))
