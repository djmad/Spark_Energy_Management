import unittest

from energy_control.broker import Config
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.safety import Temperature
from test_safety import good_snapshot


class ShadowPolicyTests(unittest.TestCase):
    def test_missing_loading_signal_latches_even_across_configuration_edit(self):
        policy = ShadowPolicy(Config())
        loading = policy.step(PolicyInput(good_snapshot(), 0, 0.5,
                                         cpu_demand_active=True, model_loading=True))
        self.assertEqual(loading.mode, "STARTUP")
        missing = policy.step(PolicyInput(good_snapshot(monotonic_s=1.5), 0, 0.5,
                                         cpu_demand_active=True))
        self.assertTrue(missing.abort_owned_loads)
        self.assertIn("model lifecycle signal lost", missing.reasons)
        policy.update_config(Config())
        recovered_signal = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 0.5,
                                         cpu_demand_active=True, model_loading=False))
        self.assertTrue(recovered_signal.abort_owned_loads)

    def test_minutes_long_model_load_holds_both_caps_then_recovers(self):
        from simulation.model import cpu_class_caps
        config = Config(cpu_entry_ratio=0.4)
        policy = ShadowPolicy(config)
        fast, slow = cpu_class_caps(config.cpu_entry_ratio)
        for tick in range(240):
            result = policy.step(PolicyInput(good_snapshot(monotonic_s=1 + tick * 0.5),
                                 100, 0.5, cpu_demand_active=True, model_loading=True))
            self.assertFalse(result.abort_owned_loads)
            self.assertLessEqual(result.cpu_fast_max_mhz, fast)
            self.assertLessEqual(result.cpu_slow_max_mhz, slow)
            self.assertLessEqual(result.gpu_max_mhz, config.gpu_entry_mhz)
            self.assertEqual(result.mode, "STARTUP")
            self.assertEqual(result.fan_min_state, 12)
        loaded_fast = result.cpu_fast_max_mhz
        ready = policy.step(PolicyInput(good_snapshot(monotonic_s=121), 100, 0.5,
                                        cpu_demand_active=True, model_loading=False))
        self.assertFalse(ready.abort_owned_loads)
        self.assertLessEqual(ready.cpu_fast_max_mhz - loaded_fast,
                             (3900 - 1378) * config.cpu_recovery_ratio_s * 0.5 + 1)
        self.assertEqual(ready.gpu_max_mhz, config.gpu_entry_mhz)

    def test_loading_cannot_hide_missing_cpu_demand_or_thermal_abort(self):
        for changes in ({"model_loading": 1}, {"model_loading": True}):
            policy = ShadowPolicy(Config())
            self.assertTrue(policy.step(PolicyInput(good_snapshot(), 0, 0.5,
                                                   **changes)).abort_owned_loads)
        policy = ShadowPolicy(Config())
        snapshot = good_snapshot(temperatures=(Temperature("cpu", 93, 0, 0),
                                               Temperature("gpu", 35, 0, 0)))
        result = policy.step(PolicyInput(snapshot, 0, 0.5,
                                         cpu_demand_active=True, model_loading=True))
        self.assertTrue(result.abort_owned_loads)

    def test_cpu_antiwindup_tracks_final_class_limits_once(self):
        from simulation.model import PID
        for fast, slow in ((2000, 1000), (3900, 2808), (3900, 500), (1500, 2808)):
            policy = ShadowPolicy(Config(cpu_fast_max_mhz=fast, cpu_slow_max_mhz=slow))
            reference = PID(policy.supervisor.cpu.gains)
            reference.propose(70, policy.config.cpu_target_c, 0.5)
            applied = min((fast - 1378) / (3900 - 1378),
                          1 if slow == 2808 else 0.75 * (slow - 338) / (2808 - 338))
            reference.track(applied, 0.5)
            result = policy.step(PolicyInput(good_snapshot(), 0, 0.5))
            self.assertFalse(result.abort_owned_loads)
            self.assertAlmostEqual(policy.supervisor.cpu.integral, reference.integral)

    def test_raising_cpu_maxima_cannot_bypass_output_ramp(self):
        from dataclasses import replace
        config = Config(cpu_fast_max_mhz=2000, cpu_slow_max_mhz=1000,
                        cpu_recovery_ratio_s=0.01)
        policy = ShadowPolicy(config)
        initial = policy.step(PolicyInput(good_snapshot(), 0, 0.5))
        self.assertEqual((initial.cpu_fast_max_mhz, initial.cpu_slow_max_mhz), (2000, 1000))
        self.assertEqual(policy.supervisor.cpu_cap, 1)
        policy.update_config(replace(config, cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808))
        raised = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 1))
        # The ramp limits the raise (fractional state +25/+33 MHz after 1 s); the
        # command waits until a raise is worth 50 MHz (25 MHz steps, doc/51).
        self.assertLessEqual(raised.cpu_fast_max_mhz, 2025)
        self.assertLessEqual(raised.cpu_slow_max_mhz, 1033)
        internal_slow, internal_fast = policy._cluster_output_caps[0], policy._cluster_output_caps[1]
        self.assertAlmostEqual(internal_fast, 2000 + (3900 - 1378) * 0.01, places=3)
        self.assertAlmostEqual(internal_slow, 1000 + (2808 - 338) * 0.01 / 0.75, places=3)
        policy.update_config(replace(config, cpu_fast_max_mhz=1500, cpu_slow_max_mhz=500))
        reduced = policy.step(PolicyInput(good_snapshot(monotonic_s=3), 0, 1))
        self.assertEqual((reduced.cpu_fast_max_mhz, reduced.cpu_slow_max_mhz), (1500, 500))

    def test_fractional_output_ramp_accumulates_without_stalling(self):
        from dataclasses import replace
        config = Config(cpu_fast_max_mhz=2000, cpu_slow_max_mhz=1000,
                        cpu_recovery_ratio_s=0.001)
        policy = ShadowPolicy(config)
        policy.step(PolicyInput(good_snapshot(), 0, 0.1))
        policy.update_config(replace(config, cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808))
        for tick in range(1, 11):
            result = policy.step(PolicyInput(good_snapshot(monotonic_s=1 + tick * 0.1), 0, 0.1))
            self.assertFalse(result.abort_owned_loads)
        # Internal fractional state accumulates (+2.5 / +3.3 MHz), commands hold
        # until a 50 MHz raise is due (25 MHz steps, doc/51).
        self.assertEqual((result.cpu_fast_max_mhz, result.cpu_slow_max_mhz), (2000, 1000))
        self.assertAlmostEqual(policy._cluster_output_caps[1], 2000 + 2522 * 0.001, places=3)
        self.assertAlmostEqual(policy._cluster_output_caps[0], 1000 + 2470 * 0.001 / 0.75, places=3)

    def test_cpu_ramp_config_edit_preserves_pid_and_admission_state(self):
        from dataclasses import replace
        config = Config(cpu_entry_ratio=0.4, cpu_recovery_ratio_s=0.02)
        policy = ShadowPolicy(config)
        first = policy.step(PolicyInput(good_snapshot(), 0, 0.5,
                                       cpu_demand_active=True, cpu_work_arrival=True))
        self.assertFalse(first.abort_owned_loads)
        self.assertAlmostEqual(policy.supervisor.cpu_cap, 0.4)
        pid = policy.supervisor.cpu
        integral = pid.integral
        policy.update_config(replace(config, cpu_entry_ratio=0.6,
                                     cpu_recovery_ratio_s=0.01, cpu_idle_down_ratio_s=0.05))
        self.assertIs(policy.supervisor.cpu, pid)
        self.assertEqual(pid.integral, integral)
        self.assertTrue(policy.supervisor.cpu_signal_seen)
        self.assertAlmostEqual(policy.supervisor.cpu_cap, 0.4)
        second = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 1,
                                        cpu_demand_active=True))
        self.assertFalse(second.abort_owned_loads)
        self.assertAlmostEqual(policy.supervisor.cpu_cap, 0.41)
        lost = policy.step(PolicyInput(good_snapshot(monotonic_s=3), 0, 1))
        self.assertTrue(lost.abort_owned_loads)
        policy.update_config(config)
        self.assertTrue(policy.step(PolicyInput(good_snapshot(monotonic_s=4), 0, 1,
                                               cpu_demand_active=True)).abort_owned_loads)

    def test_first_prefill_and_busy_ramp_stay_bounded(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=1800, gpu_entry_mhz=1200,
                                     cpu_fast_max_mhz=3200, cpu_slow_max_mhz=2300))
        first = policy.step(PolicyInput(good_snapshot(), 0, 0.5,
                                        prefill_arrival=True))
        self.assertFalse(first.abort_owned_loads)
        self.assertEqual(first.gpu_max_mhz, 1200)
        self.assertLessEqual(first.cpu_fast_max_mhz, 3200)
        self.assertLessEqual(first.cpu_slow_max_mhz, 2300)
        self.assertFalse(first.hardware_qualified)
        busy = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 100, 1.0))
        self.assertFalse(busy.abort_owned_loads)
        self.assertGreater(busy.gpu_max_mhz, first.gpu_max_mhz)
        self.assertLessEqual(busy.gpu_max_mhz, 1800)

    def test_missing_accepted_lock_and_predicted_heat_latch_abort(self):
        for change in ({"gpu_accepted_max_mhz": None},
                       {"temperatures": (Temperature("acpi0", 89, 0, 2),
                                         Temperature("gpu", 65, 0))}):
            with self.subTest(change=change):
                policy = ShadowPolicy(Config())
                result = policy.step(PolicyInput(good_snapshot(**change), 100, 0.5))
                self.assertTrue(result.abort_owned_loads)
                self.assertEqual((result.gpu_max_mhz, result.fan_min_state), (500, 12))
                again = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 1))
                self.assertTrue(again.abort_owned_loads)

    def test_bad_interval_latches_even_when_guard_is_healthy(self):
        policy = ShadowPolicy(Config())
        bad = policy.step(PolicyInput(good_snapshot(), 50, 0))
        self.assertTrue(bad.abort_owned_loads)
        self.assertIn("invalid control interval", bad.reasons)
        self.assertTrue(policy.step(PolicyInput(good_snapshot(monotonic_s=2), 50, 1))
                        .abort_owned_loads)

    def test_invalid_active_request_count_latches_abort(self):
        policy = ShadowPolicy(Config())
        result = policy.step(PolicyInput(good_snapshot(), 50, 0.5, active_jobs=21))
        self.assertTrue(result.abort_owned_loads)
        self.assertIn("invalid workload signal", result.reasons)

    def test_telemetry_interval_mismatch_aborts(self):
        policy = ShadowPolicy(Config())
        policy.step(PolicyInput(good_snapshot(), 0, 0.5))
        result = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 0.5))
        self.assertTrue(result.abort_owned_loads)
        self.assertIn("control interval differs from telemetry time", result.reasons)

    def test_telemetry_gap_up_to_three_seconds_integrates_as_one(self):
        # Live 27 September 2026: slow frames under 12 jobs gave 1.0-1.4 s gaps.
        policy = ShadowPolicy(Config())
        policy.step(PolicyInput(good_snapshot(), 0, 0.5))
        ok = policy.step(PolicyInput(good_snapshot(monotonic_s=3.0), 0, 1.0))
        self.assertFalse(ok.abort_owned_loads)
        late = policy.step(PolicyInput(good_snapshot(monotonic_s=7.5), 0, 1.0))
        self.assertTrue(late.abort_owned_loads)

    def test_policy_accepts_every_maximum_up_to_the_hard_envelope(self):
        # Live 27 September 2026: max 2000 MHz passed Config validation but the
        # policy's own Settings still capped at 1800 and the service aborted.
        from energy_control.limits import GPU_HARD_MAX_MHZ
        for maximum in (1800, 2000, GPU_HARD_MAX_MHZ):
            policy = ShadowPolicy(Config(gpu_max_mhz=maximum, gpu_entry_mhz=1700))
            self.assertEqual(policy.supervisor.s.maximum_mhz, maximum)

    def test_unrepresentable_floor_only_config_refused(self):
        with self.assertRaises(ValueError):
            ShadowPolicy(Config(gpu_max_mhz=500, gpu_entry_mhz=500))

    def test_competing_higher_cap_aborts_within_hard_envelope(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=1200))
        result = policy.step(PolicyInput(good_snapshot(gpu_accepted_max_mhz=1500), 0, 0.5))
        self.assertTrue(result.abort_owned_loads)
        self.assertIn("observed GPU limit exceeds committed policy", result.reasons)

    def test_gain_edit_preserves_state_and_lower_cap_applies_immediately(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=1800, gpu_entry_mhz=1200,
                                     fan_min_state=0))
        first = policy.step(PolicyInput(good_snapshot(), 100, 0.5))
        cpu_pid = policy.supervisor.cpu
        gpu_pid = policy.supervisor.gpu
        previous_time = policy._last_time
        previous_cap = policy.supervisor.cap
        policy.update_config(Config(gpu_max_mhz=1800, gpu_entry_mhz=1200,
                                    fan_min_state=0, cpu_kp=0.08, gpu_kd=0.09,
                                    gpu_ramp_up_mhz_s=50))
        self.assertIs(policy.supervisor.cpu, cpu_pid)
        self.assertIs(policy.supervisor.gpu, gpu_pid)
        self.assertEqual(policy._last_time, previous_time)
        self.assertEqual(policy.supervisor.cap, previous_cap)
        self.assertEqual(policy.supervisor.cpu.gains.kp, 0.08)
        self.assertEqual(policy.supervisor.gpu.gains.kd, 0.09)
        next_result = policy.step(PolicyInput(good_snapshot(monotonic_s=2), 100, 1))
        self.assertFalse(next_result.abort_owned_loads)
        self.assertLessEqual(next_result.gpu_max_mhz, first.gpu_max_mhz + 50)
        policy.update_config(Config(gpu_max_mhz=1100, gpu_entry_mhz=1000,
                                    fan_min_state=0))
        self.assertLessEqual(policy.supervisor.cap, 1000)

    def test_policy_change_cannot_clear_abort_latch(self):
        policy = ShadowPolicy(Config())
        self.assertTrue(policy.step(PolicyInput(good_snapshot(fan_healthy=False), 0, 0.5))
                        .abort_owned_loads)
        policy.update_config(Config(cpu_kp=0.08))
        self.assertTrue(policy.step(PolicyInput(good_snapshot(monotonic_s=2), 0, 1))
                        .abort_owned_loads)


if __name__ == "__main__":
    unittest.main()
