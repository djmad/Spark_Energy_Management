from dataclasses import replace
import unittest
from energy_control.engine_receipts import ExecutionReceipt
from energy_control.prefill_hold import PrefillHold
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.unified_actuation import UnifiedController
from test_safety import good_snapshot
import test_unified_actuation as fixtures


class PrefillHoldTests(unittest.TestCase):
    def test_busy_other_work_cannot_release_waiting_request(self):
        receipts = {}
        current = [1.]
        identifier = "01" * 16
        hold = PrefillHold(receipts.get, lambda: "engine-a", run_id="ab" * 16,
                           engine_epoch="engine-a", clock=lambda: current[0])
        cycle, _, gpu, _, _ = fixtures.UnifiedActuationTests().setup_cycle()
        # REARM on prefill is off in production (28 Sep); owned trials switch it on.
        cycle.config = replace(cycle.config, tuning={"prefill_rearm": 1.0})
        controller = UnifiedController(ShadowPolicy(cycle.config), cycle, prefill_hold=hold)
        def sample(now):
            current[0] = now
            return PolicyInput(good_snapshot(monotonic_s=now,
                gpu_requested_max_mhz=gpu.maximum, gpu_accepted_max_mhz=gpu.maximum),
                100, .5, cpu_demand_active=True, model_loading=False)
        with controller.prefill_entry(lambda: sample(1), workload_id=identifier):
            pass
        for now in (1.5, 2, 2.5, 3):
            controller.tick(sample(now))
            self.assertLessEqual(gpu.maximum, 1200)
        receipts[identifier] = ExecutionReceipt("ab" * 16, identifier, "engine-a", 3.5, 1)
        for now in (3.5, 4, 4.5):
            controller.tick(sample(now))
        self.assertGreater(gpu.maximum, 1200)

    def test_foreign_stale_or_future_receipt_latches_fault(self):
        receipt = ExecutionReceipt("ab" * 16, "01" * 16, "engine-a", 1, 1)
        for bad in (replace(receipt, workload_id="02" * 16),
                    replace(receipt, observed_monotonic_s=0),
                    replace(receipt, observed_monotonic_s=2)):
            hold = PrefillHold(lambda _: bad, lambda: "engine-a", run_id="ab" * 16,
                               engine_epoch="engine-a", clock=lambda: 1.)
            hold.register("01" * 16)
            with self.assertRaises(RuntimeError):
                hold.pending(1)
            self.assertTrue(hold.faulted)

    def test_receipt_may_be_newer_than_sample_but_slow_read_cannot_release(self):
        for finish, expected in ((1.05, False), (1.2, None)):
            times = iter((1., finish))
            receipt = ExecutionReceipt("ab" * 16, "01" * 16, "engine-a", finish, 1)
            hold = PrefillHold(lambda _: receipt, lambda: "engine-a", run_id="ab" * 16,
                               engine_epoch="engine-a", clock=lambda: next(times))
            hold.register("01" * 16)
            if expected is None:
                with self.assertRaises(RuntimeError):
                    hold.pending(1)
                self.assertTrue(hold.faulted)
            else:
                self.assertIs(hold.pending(1), expected)
