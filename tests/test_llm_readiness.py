from dataclasses import replace
import unittest
from energy_control.llm_container import ContainerIdentity
from energy_control.llm_readiness import ModelLoadingState, LlmReadinessObservation, observe_readiness

IDENTITY = ContainerIdentity("ab" * 32, "running", 123, "start", "no", 0, True)


class ReadinessTests(unittest.TestCase):
    def test_consumer_delay_does_not_count_as_observed_ready_dwell(self):
        state = ModelLoadingState(IDENTITY, started_s=0)
        for completed, consumed in ((.5, .5), (1, 1), (1.5, 2.6)):
            self.assertTrue(state.observe(
                LlmReadinessObservation(IDENTITY, completed - .1, completed, True, True),
                now_s=consumed))

    def test_slow_boot_is_loading_then_requires_stable_health(self):
        state = ModelLoadingState(IDENTITY, started_s=0)
        for now in range(1, 121):
            self.assertTrue(state.observe(LlmReadinessObservation(IDENTITY, now - .1, now, False),
                                          now_s=now))
        for now in (121, 122, 123):
            loading = state.observe(LlmReadinessObservation(IDENTITY, now - .1, now, True, True),
                                    now_s=now)
        self.assertFalse(loading)
        with self.assertRaises(RuntimeError):
            state.observe(LlmReadinessObservation(IDENTITY, 123.9, 124, False),
                          now_s=124)
        self.assertTrue(state.faulted)

    def test_replacement_during_probe_is_not_ready(self):
        values = iter((IDENTITY, replace(IDENTITY, pid=456)))
        times = iter((1, 1.1))
        with self.assertRaises(RuntimeError):
            observe_readiness(IDENTITY, inspect=lambda _: next(values), health=lambda: True,
                              route=lambda _: ((123, 1),), clock=lambda: next(times))

    def test_listener_replacement_and_late_readiness_do_not_release_hold(self):
        routes = iter((((123, 1),), ((456, 2),)))
        times = iter((1., 1.1))
        with self.assertRaises(RuntimeError):
            observe_readiness(IDENTITY, inspect=lambda _: IDENTITY, health=lambda: True,
                              route=lambda _: next(routes), clock=lambda: next(times))
        state = ModelLoadingState(IDENTITY, started_s=0, deadline_s=3)
        state.observe(LlmReadinessObservation(IDENTITY, .9, 1, True, True), now_s=1)
        state.observe(LlmReadinessObservation(IDENTITY, 1.9, 2, True, True), now_s=2)
        with self.assertRaises(RuntimeError):
            state.observe(LlmReadinessObservation(IDENTITY, 2.9, 3, True, True), now_s=3)

    def test_stale_or_unverified_route_latches_without_retry(self):
        for route, now in ((False, 1), (True, 4)):
            state = ModelLoadingState(IDENTITY, started_s=0)
            with self.assertRaises(RuntimeError):
                state.observe(LlmReadinessObservation(IDENTITY, .9, 1, True, route),
                              now_s=now)
            self.assertTrue(state.loading)
            self.assertTrue(state.faulted)
