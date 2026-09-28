import unittest

from energy_control.llm_readiness import ModelLoadingState, LlmReadinessObservation
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.startup_control import StartupController
from energy_control.unified_actuation import UnifiedController
from test_llm_readiness import IDENTITY
from test_safety import good_snapshot
import test_unified_actuation as fixtures


class StartupIntegrationTests(unittest.TestCase):
    def test_delayed_health_cannot_use_old_or_future_thermal_frame(self):
        for timestamp, now in ((.5, 1), (1.1, 1), (1, 1.6)):
            with self.subTest(timestamp=timestamp, now=now):
                cycle, calls, _, _, _ = fixtures.UnifiedActuationTests().setup_cycle()
                bridge = StartupController(UnifiedController(ShadowPolicy(cycle.config), cycle),
                                           ModelLoadingState(IDENTITY, started_s=0))
                sample = PolicyInput(good_snapshot(monotonic_s=timestamp), 100, .5)
                readiness = LlmReadinessObservation(IDENTITY, .9, 1, True, True)
                with self.assertRaises(RuntimeError):
                    bridge.tick(sample, readiness, now_s=now)
                self.assertTrue(bridge.faulted)
                self.assertIn("cancel_owned_requests", calls)
                self.assertFalse(any(c in calls for c in ("write", "gpu_write", "fan_write")))

    def test_readiness_drives_hold_and_loss_aborts_without_later_writes(self):
        cycle, calls, gpu, fan, _ = fixtures.UnifiedActuationTests().setup_cycle()
        bridge = StartupController(UnifiedController(ShadowPolicy(cycle.config), cycle),
                                   ModelLoadingState(IDENTITY, started_s=0))
        for tick in range(1, 9):
            now = tick * .5
            ready = tick >= 3
            sample = PolicyInput(good_snapshot(monotonic_s=now,
                gpu_requested_max_mhz=gpu.maximum, gpu_accepted_max_mhz=gpu.maximum),
                100, .5, cpu_demand_active=False, model_loading=False)
            observation = LlmReadinessObservation(IDENTITY, now - .1, now, ready, ready)
            proposal = bridge.tick(sample, observation, now_s=now)
            self.assertFalse(proposal.abort_owned_loads)
            if tick < 7:
                self.assertEqual(proposal.mode, "STARTUP")
                self.assertLessEqual(gpu.maximum, 1200)
                self.assertEqual(fan.floor, 12)
        self.assertFalse(bridge.loading_state.loading)
        self.assertGreater(gpu.maximum, 1200)
        writes = [c for c in calls if c in ("write", "gpu_write", "fan_write")]
        with self.assertRaises(RuntimeError):
            bridge.tick(sample, None, now_s=4.5)
        self.assertTrue(bridge.faulted)
        self.assertTrue(cycle.faulted)
        self.assertIn("cancel_owned_requests", calls)
        self.assertEqual(writes, [c for c in calls if c in ("write", "gpu_write", "fan_write")])
        with self.assertRaises(RuntimeError):
            bridge.tick(sample, observation, now_s=5)
