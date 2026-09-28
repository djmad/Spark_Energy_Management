import unittest
from dataclasses import replace

from energy_control.abort import AbortCoordinator, cpu_emergency_action, fan_emergency_action
from energy_control.fan import FanFloorStatus
from test_safety import good_snapshot


class FakeControl:
    def __init__(self, *, failure=None, quiet=True):
        self.calls = []
        self.failure = failure
        self.quiet = quiet

    def _call(self, name):
        self.calls.append(name)
        if name == self.failure:
            raise RuntimeError("injected")

    def close_admission(self):
        self._call("close_admission")

    def cancel_owned_requests(self):
        self._call("cancel_owned_requests")

    def terminate_owned_processes(self):
        self._call("terminate_owned_processes")

    def verify_quiescent(self, timeout_s):
        self._call("verify_quiescent")
        return self.quiet


class AbortTests(unittest.TestCase):
    def test_emergency_ceiling_and_cancellation_have_separate_evidence(self):
        for outcome in (True, False, 1, RuntimeError("setter failed")):
            with self.subTest(outcome=outcome):
                control = FakeControl()
                def emergency(maximum):
                    self.assertEqual(maximum, 500)
                    control.calls.append("gpu_500")
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome
                coordinator = AbortCoordinator(control, emergency_gpu=emergency)
                self.assertIsNone(coordinator.evaluate(good_snapshot()).emergency_gpu_verified)
                self.assertEqual(control.calls, [])
                result = coordinator.trip("thermal abort")
                self.assertEqual(control.calls, ["close_admission", "gpu_500",
                    "cancel_owned_requests", "terminate_owned_processes", "verify_quiescent"])
                self.assertEqual(result.emergency_gpu_verified, outcome is True)
                self.assertEqual(result.verified_quiescent, outcome is True)

    def test_cap_success_does_not_prove_prompt_termination(self):
        result = AbortCoordinator(FakeControl(quiet=False), emergency_gpu=lambda _: True).trip("hot")
        self.assertTrue(result.emergency_gpu_verified)
        self.assertFalse(result.verified_quiescent)

    def test_safe_does_not_touch_workload(self):
        control = FakeControl()
        result = AbortCoordinator(control).evaluate(good_snapshot())
        self.assertFalse(result.decision.abort)
        self.assertEqual(control.calls, [])

    def test_abort_orders_all_actions_and_verifies(self):
        control = FakeControl()
        result = AbortCoordinator(control).evaluate(good_snapshot(fan_healthy=False))
        self.assertTrue(result.decision.abort)
        self.assertTrue(result.verified_quiescent)
        self.assertEqual(control.calls, ["close_admission", "cancel_owned_requests",
                                         "terminate_owned_processes", "verify_quiescent"])

    def test_action_failure_does_not_skip_later_protection(self):
        control = FakeControl(failure="cancel_owned_requests")
        result = AbortCoordinator(control).evaluate(good_snapshot(fan_healthy=False))
        self.assertFalse(result.verified_quiescent)
        self.assertIn("cancel_owned_requests: RuntimeError", result.action_errors)
        self.assertEqual(control.calls[-2:], ["terminate_owned_processes", "verify_quiescent"])

    def test_unverified_termination_is_not_success(self):
        control = FakeControl(quiet=False)
        result = AbortCoordinator(control).evaluate(good_snapshot(fan_healthy=False))
        self.assertFalse(result.verified_quiescent)
        self.assertIn("owned workloads not verified quiescent", result.action_errors)

    def test_external_actuator_fault_latches_and_aborts(self):
        control = FakeControl()
        coordinator = AbortCoordinator(control)
        result = coordinator.trip("CPU actuator readback failed")
        self.assertTrue(result.verified_quiescent)
        self.assertEqual(result.decision.reasons, ("CPU actuator readback failed",))
        self.assertEqual(control.calls, ["close_admission", "cancel_owned_requests",
                                         "terminate_owned_processes", "verify_quiescent"])
        self.assertTrue(coordinator.evaluate(good_snapshot()).decision.abort)


class GoalV2AbortActionTests(unittest.TestCase):
    def test_lowest_mhz_and_fan_floor_order(self):
        control = FakeControl()
        def gpu(maximum):
            control.calls.append(f"gpu_{maximum}")
            return True
        def cpu():
            control.calls.append("cpu_min")
            return True
        def fan():
            control.calls.append("fan_12")
            return True
        result = AbortCoordinator(control, emergency_gpu=gpu, emergency_cpu=cpu,
                                  emergency_fan=fan).trip("GPU at 85 C")
        self.assertEqual(control.calls, ["close_admission", "gpu_500", "cpu_min",
            "cancel_owned_requests", "terminate_owned_processes", "fan_12", "verify_quiescent"])
        self.assertEqual((result.emergency_gpu_verified, result.emergency_cpu_verified,
                          result.emergency_fan_verified), (True, True, True))
        self.assertTrue(result.verified_quiescent)

    def test_failed_actuator_actions_do_not_stop_the_rest(self):
        control = FakeControl()
        def cpu():
            raise RuntimeError("sysfs write failed")
        result = AbortCoordinator(control, emergency_cpu=cpu,
                                  emergency_fan=lambda: False).trip("ACPI at 93 C")
        self.assertEqual(control.calls, ["close_admission", "cancel_owned_requests",
                                         "terminate_owned_processes", "verify_quiescent"])
        self.assertFalse(result.emergency_cpu_verified)
        self.assertFalse(result.emergency_fan_verified)
        self.assertIsNone(result.emergency_gpu_verified)
        self.assertFalse(result.verified_quiescent)
        self.assertIn("emergency_cpu: RuntimeError", result.action_errors)
        self.assertIn("emergency fan floor not verified", result.action_errors)

    def test_bound_adapters(self):
        class Cpu:
            def set_emergency_minimum(self):
                return True
        class Fan:
            def __init__(self):
                self.states = []
            def set_minimum(self, state):
                self.states.append(state)
                return FanFloorStatus("fake", state, 12)
        fan = Fan()
        self.assertTrue(cpu_emergency_action(Cpu())())
        self.assertTrue(fan_emergency_action(fan)())
        self.assertEqual(fan.states, [12])

if __name__ == "__main__":
    unittest.main()
