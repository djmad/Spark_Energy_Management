import unittest
from unittest.mock import patch
from dataclasses import replace
from contextlib import contextmanager
from energy_control.request_gateway import OwnedRequestDispatcher
import test_request_gateway as fixtures
import test_unified_actuation as actuators
from test_safety import good_snapshot
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.unified_actuation import UnifiedController
from energy_control.safety import CommissioningGuard


class PrefillEntryTests(unittest.TestCase):
    def test_operator_approved_setter_mode_gates_cold_dispatch(self):
        for case in ("valid", "missing", "stale", "wrong_owner", "clock_violation"):
            with self.subTest(case=case):
                cycle, _, gpu, _, _ = actuators.UnifiedActuationTests().setup_cycle()
                context = actuators.CONTEXT
                cycle.cpu.abort.guard = CommissioningGuard(
                    gpu_evidence_mode="setter_monitor", gpu_setter_context=context)
                original_sample = cycle._sample
                cycle._sample = lambda: replace(original_sample(), gpu_accepted_max_mhz=None,
                                                gpu_setter_evidence=gpu.read())
                policy = ShadowPolicy(cycle.config, gpu_evidence_mode="setter_monitor",
                                      gpu_setter_context=context)
                controller = UnifiedController(policy, cycle)
                evidence = gpu.read()
                if case == "missing":
                    evidence = None
                elif case == "stale":
                    evidence = replace(evidence, completed_monotonic_s=0,
                                       ownership_checked_monotonic_s=0)
                elif case == "wrong_owner":
                    evidence = replace(evidence, owner_epoch="different-owner")
                baseline = good_snapshot(gpu_accepted_max_mhz=None, gpu_setter_evidence=evidence,
                    gpu_measured_mhz=1300 if case == "clock_violation" else 500,
                    gpu_clock_age_s=0.0)
                sample = replace(baseline, temperatures=tuple(replace(t, celsius=30)
                                                             for t in baseline.temperatures))
                handle = fixtures.FakeHandle()
                dispatcher = OwnedRequestDispatcher(fixtures.FakeTransport(handle),
                    fixtures.FakeRecorder(), prepare_entry=lambda _: controller.prefill_entry(
                        lambda: PolicyInput(sample, 0, .5, cpu_demand_active=True,
                                            model_loading=False)))
                dispatcher.arm_admission()
                # The fake clock cannot express a completion > 1 s ago; test the
                # steady violation with the post-command settle window disabled.
                # The fake clock spans ~1 s: test staleness with the 0.5 s rule.
                with patch("energy_control.safety.CLOCK_SETTLE_S",
                           0.0 if case == "clock_violation" else 1.0), \
                        patch("energy_control.gpu_evidence.GPU_EVIDENCE_MAX_AGE_S", 0.5):
                    dispatcher.submit(object())
                    if case == "valid":
                        self.assertTrue(handle.started.wait(1))
                        dispatcher.close_admission()
                        dispatcher.cancel_owned_requests()
                    self.assertTrue(dispatcher.join_workers(1))
                self.assertEqual(handle.started.is_set(), case == "valid")
                self.assertEqual(cycle.faulted, case != "valid")

    def test_cold_low_clock_without_cap_proof_cannot_dispatch(self):
        cycle, _, _, _, _ = actuators.UnifiedActuationTests().setup_cycle()
        controller = UnifiedController(ShadowPolicy(cycle.config), cycle)
        baseline = good_snapshot(gpu_accepted_max_mhz=None, gpu_measured_mhz=500)
        cold = replace(baseline, temperatures=tuple(replace(t, celsius=30)
                                                    for t in baseline.temperatures))
        handle = fixtures.FakeHandle()
        dispatcher = OwnedRequestDispatcher(fixtures.FakeTransport(handle), fixtures.FakeRecorder(),
            prepare_entry=lambda _: controller.prefill_entry(lambda: PolicyInput(
                cold, 0, .5, cpu_demand_active=True, model_loading=False)))
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(dispatcher.join_workers(1))
        self.assertFalse(handle.started.is_set())
        self.assertTrue(cycle.faulted)
        self.assertTrue(handle.cancelled.is_set())

    def test_real_coordinator_holds_entry_envelope_during_dispatch(self):
        cycle, _, gpu, _, _ = actuators.UnifiedActuationTests().setup_cycle()
        gpu.maximum = 1700
        controller = UnifiedController(ShadowPolicy(cycle.config), cycle)
        handle = fixtures.FakeHandle()
        checks = []
        original_start = handle.start
        def start():
            checks.append((controller._lock.locked(), gpu.maximum))
            original_start()
        handle.start = start
        dispatcher = OwnedRequestDispatcher(fixtures.FakeTransport(handle), fixtures.FakeRecorder(),
            prepare_entry=lambda _: controller.prefill_entry(lambda: PolicyInput(
                good_snapshot(monotonic_s=1, gpu_requested_max_mhz=gpu.maximum,
                              gpu_accepted_max_mhz=gpu.maximum), 0, .5,
                cpu_demand_active=True, model_loading=False)))
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(1))
        dispatcher.close_admission()
        dispatcher.cancel_owned_requests()
        self.assertTrue(dispatcher.join_workers(1))
        self.assertEqual(checks, [(True, 1200)])
        self.assertFalse(controller._lock.locked())

    def test_entry_verified_before_upstream_start_and_failed_entry_never_starts(self):
        for accepted in (True, False, 1):
            with self.subTest(accepted=accepted):
                handle = fixtures.FakeHandle()
                recorder = fixtures.FakeRecorder()
                observed = []
                @contextmanager
                def entry(identifier):
                    observed.append(identifier)
                    self.assertFalse(handle.started.is_set())
                    self.assertIn(("dispatch_intent", 7), recorder.events)
                    yield accepted
                dispatcher = OwnedRequestDispatcher(fixtures.FakeTransport(handle), recorder,
                                                     prepare_entry=entry)
                dispatcher.arm_admission()
                dispatcher.submit(object())
                if accepted is True:
                    self.assertTrue(handle.started.wait(1))
                    dispatcher.close_admission()
                    dispatcher.cancel_owned_requests()
                self.assertTrue(dispatcher.join_workers(1))
                self.assertEqual(len(observed), 1)
                self.assertEqual(handle.started.is_set(), accepted is True)
