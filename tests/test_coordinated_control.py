"""Goal v2 coordinated controller: synthetic plant only, no hardware claims."""
from dataclasses import replace
import unittest

from simulation.model import (Experiment, Observation, PlantParameters, QueueLoad, Settings,
                              Supervisor, balance_reductions, steady_resistance)


R = steady_resistance(PlantParameters(), 0.5)


def run_queue(settings, seconds, plant=None):
    run = Experiment("queue", settings, plant)
    return run, [run.step(0.25) for _ in range(int(seconds / 0.25))]


class BalanceAllocatorTests(unittest.TestCase):
    def test_no_need_no_cut(self):
        self.assertEqual(balance_reductions(0, 0, R, 0.6, 1.0, 20, 50), (0.0, 0.0))

    def test_cpu_heat_is_cut_at_the_cpu_when_cheaper(self):
        (r_cc, _), _ = R
        x_c, x_g = balance_reductions(10, 0, R, 0.6, 1.0, 20, 50)
        self.assertAlmostEqual(x_c, 10 / r_cc)
        self.assertEqual(x_g, 0)

    def test_cpu_bound_spills_to_gpu(self):
        (r_cc, r_cg), _ = R
        x_c, x_g = balance_reductions(20, 0, R, 0.6, 1.0, 5, 50)  # Reservation bound.
        self.assertAlmostEqual(x_c, 5)
        self.assertAlmostEqual(r_cc * x_c + r_cg * x_g, 20)

    def test_shared_heat_is_not_cut_twice(self):
        (r_cc, r_cg), (r_gc, r_gg) = R
        need_c, need_g = 6.0, 8.0
        independent = 0.6 * need_c / r_cc + 1.0 * need_g / r_gg
        x_c, x_g = balance_reductions(need_c, need_g, R, 0.6, 1.0, 40, 60)
        self.assertGreaterEqual(r_cc * x_c + r_cg * x_g, need_c - 1e-6)
        self.assertGreaterEqual(r_gc * x_c + r_gg * x_g, need_g - 1e-6)
        self.assertLess(0.6 * x_c + x_g, independent)

    def test_infeasible_returns_both_maxima(self):
        self.assertEqual(balance_reductions(500, 500, R, 0.6, 1.0, 3, 4), (3.0, 4.0))


class CoordinatedSupervisorTests(unittest.TestCase):
    def test_fast_core_reservation_during_llm_work(self):
        controller = Supervisor()
        controller.cpu_cap = 0.5
        for step in range(40):
            command = controller.step(Observation(89.5 + step * 0.01, 60, 1, active_jobs=4,
                                                  cpu_demand_active=True, cpu_util=0.5), 0.25)
        self.assertGreaterEqual(command.cpu_ratio, Settings().cpu_reservation_ratio - 1e-9)
        self.assertEqual(controller.balance_info["reserve"], Settings().cpu_reservation_ratio)

    def test_reservation_yields_when_cpu_runs_hot(self):
        controller = Supervisor()
        controller.step(Observation(91.6, 60, 1, active_jobs=4, cpu_demand_active=True,
                                    cpu_util=0.5), 0.25)
        self.assertEqual(controller.balance_info["reserve"], 0.0)

    def test_balance_shifts_cpu_relief_to_gpu_at_the_reservation(self):
        caps = {}
        for balance in (True, False):
            # Mechanism test at an explicit reservation (default is 0.10, doc/45).
            controller = Supervisor(Settings(balance=balance, cpu_reservation_ratio=0.35))
            controller.cap = 1500
            controller.cpu_cap = 0.35
            controller.cpu.integral = 0.1  # Sustained CPU derate already built up.
            for _ in range(4):
                command = controller.step(Observation(91.0, 60, 1, active_jobs=4,
                                                      cpu_demand_active=True, cpu_util=0.5), 0.25)
            caps[balance] = command.gpu_cap_mhz
            self.assertGreaterEqual(command.cpu_ratio, 0.35 - 1e-9)
        self.assertLess(caps[True], caps[False])

    def test_low_utilisation_never_collapses_the_gpu_cap(self):
        controller = Supervisor()
        controller.gpu.integral = 0.9  # A small GPU derate request.
        for _ in range(8):
            command = controller.step(Observation(60, 76, 0.05, active_jobs=1,
                                                  cpu_demand_active=True, cpu_util=0.1), 0.25)
        self.assertGreater(command.gpu_cap_mhz, 1000)

    def test_cpu_hot_spot_cuts_cpu_not_the_gpu(self):
        from simulation.model import GB10_FIT
        controller = Supervisor(twin=GB10_FIT)
        controller.cap, controller.cpu_cap, controller.busy_s = 1800, 1.0, 5
        controller.cpu.integral = 0.05  # Sustained CPU derate demand.
        for step in range(6):  # P-cluster near its abort band, rising.
            command = controller.step(Observation(89.5 + 0.3 * step, 50, 1, active_jobs=4,
                                                  cpu_demand_active=True, cpu_util=0.5), 0.25)
        self.assertLess(command.cpu_ratio, 0.6)
        span = 1800 - 500
        self.assertGreaterEqual(command.gpu_cap_mhz, 1800 - 0.3 * span)  # Spill bound.

    def test_cpu_spike_far_below_target_cuts_nothing(self):
        # Live 27 September 2026 (fan floor 2): P-cluster spikes 60 -> 75 C
        # within a second cut the GPU to its spill bound through the PID
        # derivative while the CPU was 15 C below target.
        from simulation.model import GB10_FIT
        controller = Supervisor(twin=GB10_FIT)
        controller.cap, controller.cpu_cap, controller.busy_s = 1800, 1.0, 5
        for _ in range(40):  # Settle at full headroom (integral unwinds).
            before = controller.step(Observation(60, 48, 1, active_jobs=4,
                                                 cpu_demand_active=True, cpu_util=0.2), 0.25)
        for step in range(5):
            command = controller.step(Observation(63 + 3 * step, 48, 1, active_jobs=4,
                                                  cpu_demand_active=True, cpu_util=0.2), 0.25)
            self.assertEqual(command.gpu_cap_mhz, 1800)
            # No relief: the CPU cap keeps its normal recovery ramp.
            self.assertGreaterEqual(command.cpu_ratio, before.cpu_ratio)

    def test_cpu_below_target_never_spills_to_the_gpu(self):
        # Inside the PID band (77-79 C, live floor-2 spikes) the CPU needs
        # relief, but below its target it must come from the CPU caps only.
        from simulation.model import GB10_FIT
        controller = Supervisor(twin=GB10_FIT)
        controller.cap, controller.cpu_cap, controller.busy_s = 1800, 1.0, 5
        for _ in range(40):
            controller.step(Observation(70, 48, 1, active_jobs=4,
                                        cpu_demand_active=True, cpu_util=0.2), 0.25)
        for step in range(8):
            command = controller.step(Observation(79 + 1.0 * (step % 2), 48, 1, active_jobs=4,
                                                  cpu_demand_active=True, cpu_util=0.2), 0.25)
            self.assertEqual(command.gpu_cap_mhz, 1800)

    def test_pid_relief_resumes_inside_the_band(self):
        controller = Supervisor()
        controller.cap, controller.cpu_cap, controller.busy_s = 1800, 1.0, 5
        for step in range(20):  # Steady rise into the band towards the target.
            command = controller.step(Observation(80 + 0.5 * step, 50, 1, active_jobs=4,
                                                  cpu_demand_active=True, cpu_util=0.5), 0.25)
        self.assertLess(command.cpu_ratio, 1.0)

    def test_gpu_abort_latches_until_cooling_dwell(self):
        settings = Settings()
        controller = Supervisor(settings)
        self.assertEqual(controller.step(Observation(60, 85, 1), 0.25).mode, "FAULT")
        hot = controller.step(Observation(60, 81, 1), 0.25)  # Below abort, above hysteresis.
        self.assertEqual((hot.mode, hot.gpu_cap_mhz, hot.fan_state), ("FAULT", 500, 12))
        for _ in range(int(settings.emergency_recovery_s / 0.25) - 1):
            self.assertEqual(controller.step(Observation(60, 70, 1), 0.25).mode, "FAULT")
        resumed = controller.step(Observation(60, 70, 1), 0.25)
        self.assertNotEqual(resumed.mode, "FAULT")
        self.assertLessEqual(resumed.gpu_cap_mhz, settings.baseline_mhz)
        # Resumes from the entry ratio; one tick of the normal recovery ramp.
        self.assertLessEqual(resumed.cpu_ratio,
                             settings.cpu_entry_ratio + settings.cpu_recovery_s * 0.25 + 1e-9)

    def test_predicted_breach_must_persist(self):
        controller = Supervisor()
        controller.step(Observation(83.0, 60, 1, active_jobs=4), 0.25)   # abort 96 C
        spike = controller.step(Observation(89.0, 60, 1, active_jobs=4), 0.25)  # +24 C/s
        self.assertNotEqual(spike.mode, "FAULT")
        back = controller.step(Observation(84.0, 60, 1, active_jobs=4), 0.25)
        self.assertNotEqual(back.mode, "FAULT")
        modes = [controller.step(Observation(87.0 + 0.9 * k, 60, 1, active_jobs=4), 0.25).mode
                 for k in range(8)]  # Sustained rise within the band.
        self.assertIn("FAULT", modes)

    def _gpu_near_target(self, signal, seconds=120):
        """Policy settings (targets 50/72) fed the 2 s trend of an integer GPU sensor."""
        from energy_control.broker import Config
        from energy_control.policy import ShadowPolicy
        from simulation.model import GB10_FIT
        settings = ShadowPolicy._settings(Config(gpu_max_mhz=1800, gpu_entry_mhz=1700,
                                                 gpu_target_c=50.0, cpu_target_c=72.0))
        controller = Supervisor(settings, twin=GB10_FIT)
        controller.cap = 1700
        history, caps = [], []
        for i in range(int(seconds / 0.25)):
            t = i * 0.25
            history = [p for p in history + [(t, signal(t, i))] if t - p[0] <= 2.0]
            n = len(history)
            if n < 3:
                value = history[-1][1]
            else:
                mt = sum(x for x, _ in history) / n
                mv = sum(y for _, y in history) / n
                b = (sum((x - mt) * (y - mv) for x, y in history)
                     / sum((x - mt) ** 2 for x, _ in history))
                value = mv + b * (t - mt)
            command = controller.step(Observation(58.0, value, 0.96, active_jobs=12,
                                                  prefill_arrival=(i == 0),
                                                  cpu_demand_active=True, cpu_util=0.3), 0.25)
            caps.append((t, command.gpu_cap_mhz))
        return caps

    def test_load_start_kick_does_not_drain_gpu_headroom(self):
        # Live TH run 10 (targets 50/72): a 39 -> 45 C step at load start plus the
        # entry ceiling drained the GPU integral; the cap then cycled near
        # 800-1100 MHz while the (integer) GPU sensor read 43-45 C.
        noise = lambda t, i: 39.0 if t < 1 else (45.0 if t < 2 else
                                                 44.0 + (1.0 if (i // 4) % 2 else -1.0))
        steady = [c for t, c in self._gpu_near_target(noise) if t >= 30]
        self.assertGreaterEqual(min(steady), 1650)
        self.assertGreaterEqual(sum(steady) / len(steady), 1740)

    def test_real_gpu_rise_is_cut_at_the_target(self):
        # The conditional integrator (doc/48 §0, D2) relieves at the target
        # with derivative anticipation, not ~13 C below it (defect 27); the
        # production GPU target (75 C) sits 10 C below its 85 C abort.
        rise = lambda t, i: 44.0 if t < 30 else min(58.0, 44.0 + 2.0 * (t - 30))
        caps = self._gpu_near_target(rise, seconds=40)
        first_cut = next(t for t, c in caps if t >= 30 and c < 1700)
        self.assertLessEqual(first_cut - 30, 3.5)  # The GPU reaches 50 C at 3 s.
        self.assertLess(min(c for t, c in caps if t <= 36), 1500)  # and keeps cutting

    def test_acpi_abort_is_the_limits_value(self):
        from energy_control.limits import ACPI_ABORT_C   # 96 C since 27 September 2026
        self.assertNotEqual(Supervisor().step(Observation(ACPI_ABORT_C - 5, 60, 1), 0.25).mode,
                            "FAULT")
        self.assertEqual(Supervisor().step(Observation(ACPI_ABORT_C, 60, 1), 0.25).mode, "FAULT")

    def test_entry_fallback_can_be_disabled_once_qualified(self):
        for fallback, expected in ((True, "REARM"), (False, "RUN")):
            controller = Supervisor(Settings(entry_fallback=fallback))
            controller.cap, controller.busy_s = 1800, 5
            command = controller.step(Observation(60, 50, 1, prefill_arrival=True), 0.25)
            self.assertEqual(command.mode, expected)
            self.assertEqual(command.gpu_cap_mhz, 1200 if fallback else 1800)

    def test_fan_feedforward_to_preferred_on_load_entry(self):
        controller = Supervisor(Settings(fan_policy="staging", fan_preferred_state=7))
        controller.fan_state = 2
        command = controller.step(Observation(50, 40, 1, prefill_arrival=True), 0.25)
        self.assertEqual(command.fan_state, 7)

    def test_load_fan_policy_holds_load_level_and_idles_after_delay(self):
        # doc/48 §0, D1: level 12 under any load; minimum only after a long idle.
        settings = Settings(fan_min_state=2, fan_idle_delay_s=60.0)
        controller = Supervisor(settings)
        controller.fan_state = 2
        loaded = controller.step(Observation(50, 40, 1, prefill_arrival=True), 0.25)
        self.assertEqual(loaded.fan_state, 12)
        cpu_only = Supervisor(settings)
        cpu_only.fan_state = 2
        self.assertEqual(cpu_only.step(Observation(50, 40, 0, cpu_demand_active=True,
                                                   cpu_util=0.4), 0.25).fan_state, 12)
        states = [controller.step(Observation(45, 38, 0, cpu_demand_active=False,
                                              cpu_util=0.02), 0.25).fan_state
                  for _ in range(int(60 / 0.25) + 2)]
        self.assertEqual(states[0], 12)           # still inside the idle delay
        self.assertEqual(states[-1], 2)           # idle delay passed: minimum
        self.assertEqual(Supervisor(Settings(fan_load_state=9)).step(
            Observation(50, 40, 1, active_jobs=2), 0.25).fan_state, 9)

    def test_guard_margin_backs_the_cpu_setpoint_off_and_recovers(self):
        # doc/42 defect 28: a projection within guard_margin_c of the 93 C abort
        # lowers the effective setpoint; calm operation recovers it slowly. It
        # starts at the ceiling below the guard's no-confirmation zone (87 C).
        from simulation.model import cpu_setpoint_ceiling
        settings = Settings()
        ceiling = cpu_setpoint_ceiling(settings)
        self.assertEqual(ceiling, 89.0)   # 96 - 3 (immediate zone) - 4 (trend margin)
        controller = Supervisor(settings)
        self.assertEqual(controller.cpu_setpoint, ceiling)
        for _ in range(8):                        # 2 s of near-misses (projection 95)
            controller.step(Observation(89.0, 60, 1, active_jobs=2, cpu_projected_c=95.0), 0.25)
        self.assertAlmostEqual(controller.cpu_setpoint, ceiling - 2.0 * settings.setpoint_backoff_c_s)
        self.assertAlmostEqual(controller.near_miss_s, 2.0)
        for _ in range(400):                      # far more than the back-off bound
            controller.step(Observation(89.0, 60, 1, active_jobs=2, cpu_projected_c=95.5), 0.25)
        self.assertAlmostEqual(controller.cpu_setpoint, ceiling - settings.setpoint_backoff_max_c)
        for _ in range(40):                       # 10 s calm: slow recovery only
            controller.step(Observation(78.0, 60, 1, active_jobs=2, cpu_projected_c=79.0), 0.25)
        self.assertAlmostEqual(controller.cpu_setpoint,
                               ceiling - settings.setpoint_backoff_max_c
                               + 10.0 * settings.setpoint_recovery_c_s, places=6)
        unknown = Supervisor(settings)             # no projection: ceiling unchanged
        unknown.step(Observation(89.0, 60, 1, active_jobs=2), 0.25)
        self.assertEqual(unknown.cpu_setpoint, ceiling)
        lower = Supervisor(Settings(cpu_target_c=84.0))  # a lower target is the ceiling
        self.assertEqual(lower.cpu_setpoint, 84.0)

    def test_recovery_tapers_toward_the_setpoint(self):
        # Live sawtooth, 27 September: full-rate ramps into the limit. Far below
        # the setpoint the cap recovers at full rate, at the setpoint at 10 %.
        settings = Settings()
        far, near = Supervisor(settings), Supervisor(settings)
        for controller, temperature in ((far, 60.0), (near, 89.9)):
            controller.cpu_cap = 0.5
            controller.previous_cpu_demand = True   # demand already running: no entry clamp
            controller.step(Observation(temperature, 50, 0, cpu_demand_active=True,
                                        cpu_projected_c=temperature,
                                        cpu_projected_basis_c=temperature), 0.25)
        self.assertAlmostEqual(far.cpu_cap, 0.5 + settings.cpu_recovery_s * 0.25)
        self.assertAlmostEqual(near.cpu_cap,
                               0.5 + settings.cpu_recovery_s * settings.recovery_taper_min * 0.25)

    def test_derate_band_is_a_last_resort(self):
        # 1 C band: a smooth projection of 94.5 C leaves the cap alone, 95.5 C halves it.
        from simulation.model import Gains
        for projected, bound in ((94.5, 1.0), (95.5, 0.5)):
            controller = Supervisor(Settings(cpu_gains=Gains(kp=0, ki=0, kd=0)))  # band only
            controller.step(Observation(projected, 60, 1, active_jobs=2), 0.25)
            self.assertLessEqual(controller.cpu_cap, bound + 1e-9)
            if bound == 1.0:
                self.assertGreater(controller.cpu_cap, 0.9)

    def test_settings_reject_invalid_fan_policy_and_guard_margin(self):
        for changes in ({"fan_policy": "loud"}, {"fan_load_state": 13}, {"guard_margin_c": 0.5},
                        {"setpoint_recovery_c_s": 2.0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(**changes)

    def test_fan_goes_to_12_near_an_abort(self):
        controller = Supervisor()
        controller.fan_state = 6
        self.assertEqual(controller.step(Observation(91.0, 60, 1), 0.25).fan_state, 12)

    def test_settings_reject_unsafe_v2_values(self):
        for changes in ({"gpu_emergency_c": 86}, {"gpu_target_c": 85}, {"cpu_emergency_c": 97},
                        {"cpu_reservation_ratio": 0.6}, {"fan_boost_headroom": 0.8},
                        {"fan_preferred_state": 13}, {"balance": 1}, {"entry_fallback": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Settings(**changes)


class TypicalLoadScenarioTests(unittest.TestCase):
    """Goal v2 typical load: GPU 100 %, CPU about 50 %, 10+ queued requests."""

    def test_load_fan_policy_holds_twelve_without_changes(self):
        _, rows = run_queue(Settings(queue_load=QueueLoad(10, 12, 4)), 600)
        self.assertFalse(any(row["test_aborted"] for row in rows))
        self.assertTrue(all(row["fan_state"] == 12 for row in rows[4:]))
        self.assertLessEqual(max(r["cpu_c"] for r in rows), 91.0)
        self.assertLessEqual(max(r["gpu_c"] for r in rows[-480:]), 76.0)

    def test_holds_targets_at_preferred_fan_without_cycling(self):
        run, rows = run_queue(Settings(fan_policy="staging", queue_load=QueueLoad(10, 12, 4)), 600)
        tail = rows[-480:]
        self.assertFalse(any(row["test_aborted"] for row in rows))
        self.assertLess(abs(sum(r["gpu_c"] for r in tail) / len(tail) - 75), 1.0)
        self.assertLessEqual(max(r["cpu_c"] for r in rows), 90.5)
        self.assertEqual(rows[-1]["fan_state"], 6)
        changes = sum(a["fan_state"] != b["fan_state"] for a, b in zip(rows, rows[1:]))
        self.assertLessEqual(changes, 10)

    def test_cpu_heavy_hot_ambient_holds_cpu_target(self):
        run, rows = run_queue(Settings(queue_load=QueueLoad(20, 12, 4)), 600,
                              PlantParameters(ambient_c=35.0, cpu_reference_w=45.0))
        tail = rows[-480:]
        self.assertFalse(any(row["test_aborted"] for row in rows))
        self.assertLess(max(r["cpu_c"] for r in rows), 93.0)   # below the immediate zone (96-3)
        # The load-onset projection backs the setpoint off (guard margin); it
        # recovers slowly toward the ceiling (89 C) and the plant tracks it.
        setpoint = run.control.cpu_setpoint
        self.assertTrue(83.0 <= setpoint <= 89.0)
        self.assertLess(abs(sum(r["cpu_c"] for r in tail) / len(tail) - setpoint), 0.6)
        self.assertLessEqual(max(r["gpu_c"] for r in tail), 75.5)


class PolicyWiringTests(unittest.TestCase):
    def test_config_fan_levels_and_cpu_util_reach_the_controller(self):
        from energy_control.broker import Config
        from energy_control.policy import PolicyInput, ShadowPolicy
        from test_safety import good_snapshot
        policy = ShadowPolicy(Config(fan_min_state=2, fan_preferred_state=7))
        self.assertEqual((policy.supervisor.s.fan_min_state,
                          policy.supervisor.s.fan_preferred_state), (2, 7))
        result = policy.step(PolicyInput(good_snapshot(), 100, 0.5, active_jobs=4,
                                         cpu_util_pct=50))
        self.assertFalse(result.abort_owned_loads)
        self.assertGreaterEqual(result.fan_min_state, 7)
        invalid = ShadowPolicy(Config()).step(PolicyInput(good_snapshot(), 100, 0.5,
                                                          cpu_util_pct=101))
        self.assertTrue(invalid.abort_owned_loads)


class GpuCommandQuantizationTests(unittest.TestCase):
    def policy(self):
        from energy_control.broker import Config
        from energy_control.policy import ShadowPolicy
        return ShadowPolicy(Config(gpu_max_mhz=1800, gpu_entry_mhz=1200))

    def test_floor_to_step_never_above_controller_cap(self):
        policy = self.policy()
        for cap in (1200.0, 1212.4, 1249.9, 1263.0, 1401.2):
            out = policy._quantize_gpu(cap)
            self.assertLessEqual(out, cap)
            self.assertEqual(out % 25, 0)

    def test_hysteresis_suppresses_boundary_flapping(self):
        policy = self.policy()
        outputs = {policy._quantize_gpu(cap) for cap in
                   (1375.2, 1374.9, 1375.1, 1376.0, 1399.0, 1375.4) * 10}
        self.assertLessEqual(len(outputs), 2)
        self.assertEqual(policy._quantize_gpu(1300.0), 1300)  # Reductions are immediate.

    def test_full_ramp_fits_the_owner_command_budget(self):
        from energy_control.gpu_owner_session import MAX_NORMAL_COMMANDS
        policy = self.policy()
        outputs, cap = [], 1200.0
        while cap < 1800:
            outputs.append(policy._quantize_gpu(cap))
            cap += 100 * 0.25  # 100 MHz/s at 4 Hz.
        outputs.append(policy._quantize_gpu(1800.0))
        commands = sum(a != b for a, b in zip(outputs, outputs[1:])) + 1
        self.assertEqual(outputs[-1], 1800)
        self.assertLessEqual(commands, 30)
        self.assertLess(commands, MAX_NORMAL_COMMANDS)


class Gb10FitTests(unittest.TestCase):
    def test_measured_twin_reproduces_the_identification_runs(self):
        from dataclasses import replace
        from simulation.model import GB10_FIT, Command, Plant
        results = {}
        for state in (12, 4):
            plant = Plant(replace(GB10_FIT), cpu_c=45, gpu_c=35, sink_c=34)
            for _ in range(960):  # 240 s at 1800 MHz, full utilisation.
                plant.advance(Command(1800, 1.0, state, "RUN", ""), 1.0, 0.3, 0.25)
            results[state] = plant.gpu_c
        self.assertTrue(40 <= results[12] <= 47)  # Measured end of load ~45 C at floor 12.
        self.assertTrue(46 <= results[4] <= 53)   # Measured ~48-50 C at floors 4/2.
        self.assertGreater(results[4], results[12])

    def test_tui_headless_accepts_the_measured_plant(self):
        from simulation.tui import PLANTS, headless
        summary = headless("queue", Settings(), 20, plant=PLANTS["gb10-fit"])
        self.assertFalse(summary["test_aborted"])


if __name__ == "__main__":
    unittest.main()
