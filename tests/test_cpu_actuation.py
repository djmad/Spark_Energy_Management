from dataclasses import replace
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.cpu_actuation import GuardedCpuStep
from energy_control.cpu_frequency import CpuPolicy
from energy_control.trial_plan import TrialProposal
from test_safety import good_snapshot


def policies(slow=1800, fast=2500):
    result = []
    for index in range(20):
        cpu_class = "slow" if index < 10 else "fast"
        low, high = ((338000, 2808000) if cpu_class == "slow"
                     else (1378000, 3900000))
        result.append(CpuPolicy(f"policy{index}", cpu_class, low, high, low,
                                (slow if cpu_class == "slow" else fast) * 1000,
                                "conservative"))
    return tuple(result)


class FakeAdapter:
    def __init__(self, calls, *, fail=False, wrong_readback=False,
                 false_report=False, topology_drift=False):
        self.calls = calls
        self.fail = fail
        self.wrong_readback = wrong_readback
        self.false_report = false_report
        self.topology_drift = topology_drift
        self.current = policies()

    def readback(self):
        self.calls.append("readback")
        return self.current

    def set_maxima(self, *, slow_mhz, fast_mhz):
        self.calls.append("write")
        if self.fail:
            raise OSError("partial write")
        self.current = policies(slow_mhz, fast_mhz - int(self.wrong_readback))
        if self.topology_drift:
            self.current = tuple(
                replace(p, name="policy10" if p.name == "policy0" else "policy0")
                if p.name in {"policy0", "policy10"} else p for p in self.current
            )
        return policies(slow_mhz, fast_mhz) if self.false_report else self.current


class FakeRecorder:
    def __init__(self, calls, *, fail_intent=False, proposal=None):
        self.calls = calls
        self.fail_intent = fail_intent
        self.trial_proposal = proposal
        self.plan_written = proposal is not None

    def write_intent(self, kind, *, requested_mhz=None, workload_id=None):
        self.calls.append(f"intent:{kind}")
        if self.fail_intent:
            raise OSError("disk sync failed")
        return len(self.calls)

    def write_outcome(self, intent_seq, *, accepted_mhz=None, measured_mhz=None,
                      verified=False):
        self.calls.append("outcome")

    def write_event(self, kind):
        self.calls.append(f"event:{kind}")
        return len(self.calls)


class FakeControl:
    def __init__(self, calls):
        self.calls = calls

    def close_admission(self):
        self.calls.append("close_admission")

    def cancel_owned_requests(self):
        self.calls.append("cancel_owned_requests")

    def terminate_owned_processes(self):
        self.calls.append("terminate_owned_processes")

    def verify_quiescent(self, timeout_s):
        self.calls.append("verify_quiescent")
        return True


class CpuActuationTests(unittest.TestCase):
    def make_step(self, *, proposal=None, verify_ownership=lambda: True, **adapter_options):
        calls = []
        adapter = FakeAdapter(calls, **adapter_options)
        if proposal is None:
            proposal = TrialProposal(2, 1, 30, 4, 0, 0, 1800, 1200, 12,
                                     cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                     config_digest="0" * 64)
        recorder = FakeRecorder(calls, proposal=proposal)
        step = GuardedCpuStep(adapter, recorder,
                              AbortCoordinator(FakeControl(calls)), verify_ownership=verify_ownership)
        return step, calls, recorder

    def test_trial_plan_is_required_and_its_cpu_ceilings_are_enforced(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "durable trial plan"):
            GuardedCpuStep(FakeAdapter(calls), FakeRecorder(calls),
                           AbortCoordinator(FakeControl(calls)), verify_ownership=lambda: True)

    def test_ownership_loss_before_write_after_sync_and_after_write(self):
        for lose_at in (1, 2, 3, 4):
            with self.subTest(lose_at=lose_at):
                checks = []
                def verify():
                    checks.append(1)
                    return len(checks) < lose_at
                step, calls, _ = self.make_step(verify_ownership=verify)
                result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
                self.assertFalse(result.applied)
                self.assertTrue(result.abort.verified_quiescent)
                self.assertTrue(step.faulted)
                if lose_at <= 2:
                    self.assertNotIn("write", calls)
                if lose_at <= 3:
                    self.assertNotIn("outcome", calls)

    def test_plan_ceilings_and_mutation_are_enforced(self):
        plan = TrialProposal(2, 1, 30, 4, 0, 0, 1800, 1200, 12,
                             cpu_fast_max_mhz=2550, cpu_slow_max_mhz=1950,
                             config_digest="0" * 64)
        step, calls, recorder = self.make_step(proposal=plan)
        result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2500)
        self.assertFalse(result.applied)
        self.assertNotIn("write", calls)
        self.assertFalse(any(call.startswith("intent:") for call in calls))
        self.assertTrue(result.abort.verified_quiescent)
        fresh, calls, recorder = self.make_step(proposal=plan)
        recorder.trial_proposal = None
        result = fresh.apply(good_snapshot(), slow_mhz=1900, fast_mhz=2500)
        self.assertFalse(result.applied)
        self.assertNotIn("write", calls)

    def test_synced_intents_precede_upward_write(self):
        step, calls, _ = self.make_step()
        result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
        self.assertTrue(result.applied)
        self.assertEqual(calls, ["readback", "intent:raise_cpu_slow_cap",
                                 "intent:raise_cpu_fast_cap", "write", "readback",
                                 "outcome", "outcome"])

    def test_downward_change_needs_no_raise_intent(self):
        step, calls, _ = self.make_step()
        result = step.apply(good_snapshot(), slow_mhz=1500, fast_mhz=2200)
        self.assertTrue(result.applied)
        self.assertEqual(calls, ["readback", "write", "readback"])

    def test_intent_failure_prevents_write_and_aborts(self):
        step, calls, recorder = self.make_step()
        recorder.fail_intent = True
        result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
        self.assertFalse(result.applied)
        self.assertNotIn("write", calls)
        self.assertTrue(result.abort.verified_quiescent)
        self.assertIn("close_admission", calls)
        self.assertTrue(step.faulted)

    def test_partial_write_or_false_readback_aborts_and_poison_step(self):
        for options in ({"fail": True}, {"wrong_readback": True},
                        {"wrong_readback": True, "false_report": True}):
            with self.subTest(options=options):
                step, calls, _ = self.make_step(**options)
                result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
                self.assertFalse(result.applied)
                self.assertTrue(result.abort.verified_quiescent)
                self.assertIn("terminate_owned_processes", calls)
                self.assertTrue(step.faulted)

    def test_bad_topology_or_target_fails_before_intent_and_write(self):
        for malformed in (
            policies()[:-1],
            tuple(replace(p, name="policy20") if p.name == "policy19" else p
                  for p in policies()),
            tuple(replace(p, hardware_max_khz=4000000) if p.name == "policy19" else p
                  for p in policies()),
        ):
            with self.subTest(malformed=malformed):
                step, calls, _ = self.make_step()
                step.adapter.current = malformed
                result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
                self.assertFalse(result.applied)
                self.assertNotIn("write", calls)
                self.assertFalse(any(call.startswith("intent:") for call in calls))
        step, calls, _ = self.make_step()
        result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=3901)
        self.assertFalse(result.applied)
        self.assertNotIn("readback", calls)
        self.assertNotIn("write", calls)

    def test_identity_drift_between_write_and_readback_aborts(self):
        step, calls, _ = self.make_step(topology_drift=True, false_report=True)
        result = step.apply(good_snapshot(), slow_mhz=2000, fast_mhz=2600)
        self.assertFalse(result.applied)
        self.assertEqual(calls.count("readback"), 2)
        self.assertTrue(step.faulted)
        self.assertTrue(result.abort.verified_quiescent)

    def test_guard_blocks_write_and_intent_before_temperature_boundary(self):
        from dataclasses import replace
        from energy_control.safety import Temperature

        step, calls, _ = self.make_step()
        unsafe = replace(good_snapshot(), temperatures=tuple(
            Temperature(value.name, 95.5 if value.name == "acpi_tsoc" else value.celsius,
                        0, 0.5 if value.name == "acpi_tsoc" else 0)
            for value in good_snapshot().temperatures))
        result = step.apply(unsafe, slow_mhz=2000, fast_mhz=2600)
        self.assertFalse(result.applied)
        self.assertNotIn("readback", calls)
        self.assertNotIn("write", calls)
        self.assertIn("close_admission", calls)


if __name__ == "__main__":
    unittest.main()
