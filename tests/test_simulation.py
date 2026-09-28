import math
import unittest
from dataclasses import replace

from simulation.model import (Command, Experiment, Gains, Observation, PID, Plant,
                              PlantParameters,
                              SCENARIOS, Settings, Supervisor, cpu_class_caps)
from simulation.tui import PARAMETERS, edit_parameter, headless
from simulation.model import RandomLoad
from simulation.model import QueueLoad


class RandomLoadTests(unittest.TestCase):
    def test_live_cpu_edit_preserves_run_and_does_not_rearm_gpu(self):
        settings = Settings(random_load=RandomLoad(100, 100, 20, 20, hold_s=60))
        run = Experiment("random", settings)
        for _ in range(24):
            run.step()
        before = (run.time, run.plant.cpu_c, run.plant.gpu_c,
                  run.control.cpu.integral, run.control.gpu.integral, run.control.cap)
        updated = replace(settings, random_load=replace(settings.random_load, cpu_min_pct=80, cpu_max_pct=80))
        run.update_parameters(updated, run.plant.p)
        self.assertEqual(before, (run.time, run.plant.cpu_c, run.plant.gpu_c,
                                 run.control.cpu.integral, run.control.gpu.integral, run.control.cap))
        row = run.step()
        self.assertEqual(row["cpu_util_pct"], 80)
        self.assertFalse(row["prefill_arrival"])
        self.assertEqual(row["t_s"], before[0])

    def test_live_gpu_edit_rearms_without_resetting_temperature_or_time(self):
        run = Experiment("random", Settings(random_load=RandomLoad(100, 100, 20, 20, hold_s=60),
                                            prefill_rearm=True))
        for _ in range(24):
            run.step()
        before = run.time, run.plant.gpu_c
        updated = replace(run.control.s, random_load=RandomLoad(80, 80, 20, 20, hold_s=60))
        run.update_parameters(updated, run.plant.p)
        row = run.step()
        self.assertEqual((row["t_s"], row["gpu_c"]), before)
        self.assertEqual(row["gpu_util_pct"], 80)
        self.assertTrue(row["prefill_arrival"])
        self.assertLessEqual(row["gpu_cap_mhz"], 1200)

    def test_independent_ranges_and_repeatability(self):
        loads = RandomLoad(70, 100, 10, 30, hold_s=2, seed=19)
        samples = [loads.sample(t * 2)[:2] for t in range(100)]
        self.assertTrue(all(0.7 <= g <= 1 and 0.1 <= c <= 0.3 for g, c in samples))
        self.assertGreater(len(set(samples)), 50)
        self.assertEqual(samples, [loads.sample(t * 2)[:2] for t in range(100)])
        self.assertNotEqual(samples, [replace(loads, seed=20).sample(t * 2)[:2] for t in range(100)])

    def test_hold_and_fixed_load(self):
        loads = RandomLoad(75, 75, 20, 20, hold_s=5)
        self.assertEqual(loads.sample(0), loads.sample(4.75))
        self.assertEqual(loads.sample(5), (0.75, 0.2, 1))

    def test_bad_ranges_rejected(self):
        for changes in ({"gpu_min_pct": 101}, {"cpu_max_pct": -1},
                        {"cpu_min_pct": 80, "cpu_max_pct": 20},
                        {"hold_s": math.nan}, {"seed": 1.5}):
            with self.assertRaises(ValueError):
                RandomLoad(**changes)

    def test_random_experiment_restarts_identically_and_rearms(self):
        settings = Settings(random_load=RandomLoad(95, 100, 30, 60, hold_s=2), prefill_rearm=True)
        a, b = Experiment("random", settings), Experiment("random", settings)
        for _ in range(80):
            row = a.step()
            self.assertEqual(row, b.step())
            self.assertTrue(30 <= row["cpu_util_pct"] <= 60)
            if row["t_s"] % 2 == 0:
                self.assertTrue(row["prefill_arrival"])
                self.assertLessEqual(row["gpu_cap_mhz"], 1200)


class QueueLoadTests(unittest.TestCase):
    def test_counts_are_independent_and_bounded(self):
        with self.assertRaises(ValueError):
            QueueLoad(cpu_cores=21)
        with self.assertRaises(ValueError):
            QueueLoad(queued_jobs=-1)
        with self.assertRaises(ValueError):
            QueueLoad(active_jobs=2.5)
        run = Experiment("queue", Settings(queue_load=QueueLoad(5, 12, 4)))
        row = run.step()
        self.assertEqual((row["cpu_cores"], row["queued_jobs"], row["active_jobs"]), (5, 12, 4))
        self.assertEqual(row["cpu_util_pct"], 25)
        self.assertEqual(row["gpu_util_pct"], 100)
        self.assertLessEqual(row["gpu_cap_mhz"], 1200)

    def test_waiting_jobs_alone_do_not_heat_gpu(self):
        run = Experiment("queue", Settings(queue_load=QueueLoad(0, 20, 0)))
        row = run.step()
        self.assertEqual(row["gpu_util_pct"], 0)
        self.assertEqual(row["queued_jobs"], 20)

    def test_new_queued_work_rearms_during_active_load(self):
        run = Experiment("queue", Settings(queue_load=QueueLoad(5, 10, 4), prefill_rearm=True))
        for _ in range(40):
            run.step()
        self.assertGreater(run.control.cap, 1200)
        updated = replace(run.control.s, queue_load=QueueLoad(5, 11, 4))
        run.update_parameters(updated, run.plant.p)
        row = run.step()
        self.assertTrue(row["prefill_arrival"])
        self.assertLessEqual(row["gpu_cap_mhz"], 1200)

    def test_queue_drain_does_not_falsely_signal_new_prefill(self):
        run = Experiment("queue", Settings(queue_load=QueueLoad(5, 12, 4)))
        for _ in range(40):
            run.step()
        self.assertGreater(run.control.cap, 1200)
        updated = replace(run.control.s, queue_load=QueueLoad(5, 11, 4))
        run.update_parameters(updated, run.plant.p)
        row = run.step()
        self.assertFalse(row["prefill_arrival"])
        self.assertNotEqual(row["mode"], "REARM")

    def test_explicit_prefill_with_unchanged_counts_rearms_once(self):
        run = Experiment("queue", Settings(queue_load=QueueLoad(5, 10, 4), prefill_rearm=True))
        for _ in range(40):
            run.step()
        self.assertGreater(run.control.cap, 1200)
        run.inject_prefill_arrival()
        row = run.step()
        self.assertEqual((row["cpu_cores"], row["queued_jobs"], row["active_jobs"]),
                         (5, 10, 4))
        self.assertTrue(row["prefill_arrival"])
        self.assertLessEqual(row["gpu_cap_mhz"], 1200)
        self.assertFalse(run.step()["prefill_arrival"])
        with self.assertRaises(ValueError):
            Experiment("idle").inject_prefill_arrival()

    def test_headless_queue_prefill_injection_is_bounded_to_queue_scenario(self):
        summary = headless("queue", Settings(queue_load=QueueLoad(5, 10, 4), prefill_rearm=True),
                           12, prefill_at_s=10)
        self.assertEqual(summary["injected_prefill_at_s"], 10)
        metrics = summary["injected_prefill_metrics"]
        self.assertEqual(metrics["observed_s"], 10)
        # The 75 C GPU target may derate slightly before the injected prefill.
        self.assertGreater(metrics["previous_cap_mhz"], 1700)
        self.assertEqual(metrics["entry_cap_mhz"], 1200)
        self.assertAlmostEqual(metrics["cap_drop_mhz"], metrics["previous_cap_mhz"] - 1200)
        self.assertIsNone(metrics["recovery_to_previous_cap_s"])
        self.assertEqual(metrics["observed_window_s"], 1.75)
        self.assertGreaterEqual(metrics["max_gpu_rise_c"], 0)
        self.assertLessEqual(summary["maximum_gpu_cap_mhz"], 1800)
        recovered = headless("queue", Settings(queue_load=QueueLoad(5, 10, 4), prefill_rearm=True),
                             30, prefill_at_s=10)["injected_prefill_metrics"]
        # Ramp (100 MHz/s) plus busy dwell, slowed near the 75 C GPU target.
        self.assertIsNotNone(recovered["recovery_to_previous_cap_s"])
        self.assertTrue(6 <= recovered["recovery_to_previous_cap_s"] <= 15)
        with self.assertRaises(ValueError):
            headless("bursts", Settings(), 12, prefill_at_s=10)
        with self.assertRaises(ValueError):
            headless("queue", Settings(), 12, prefill_at_s=12)
        with self.assertRaises(ValueError):
            headless("queue", Settings(), 12, prefill_at_s=0)


class PIDTests(unittest.TestCase):
    def test_derivative_has_no_setpoint_kick(self):
        pid = PID(Gains(kp=0, ki=0, kd=1))
        pid.propose(60, 70, 0.25)
        pid.propose(60, 90, 0.25)
        self.assertEqual(pid.d, 0)

    def test_derivative_opposes_heating(self):
        pid = PID(Gains())
        pid.propose(60, 80, 0.25)
        pid.propose(62, 80, 0.25)
        self.assertLess(pid.d, 0)

    def test_downstream_performance_limit_freezes_integrator(self):
        # Entry ceiling / ramp below the request must not drain the headroom
        # integral (live TH run 10, 27 September 2026). Tracking integrator.
        pid = PID(Gains(kp=0.01, ki=0.1, kd=0, integrator="tracking"))
        pid.integral = 0.9
        for _ in range(100):
            pid.propose(79, 80, 0.25)   # raw = 0.01 + 0.9 < 1: requesting 0.91
            pid.track(0.2, 0.25)        # downstream limit holds the actuator at 0.2
        self.assertAlmostEqual(pid.integral, 0.9, places=9)

    def test_saturation_is_pulled_back_to_one(self):
        pid = PID(Gains(kp=0.06, ki=0.0, kd=0, integrator="tracking"))
        pid.integral = 1.0
        for _ in range(40):
            pid.propose(60, 80, 0.25)   # p = 1.2: raw far above 1
            pid.track(0.5, 0.25)        # ramp-limited actuator
        self.assertLess(pid.integral, 0.05)  # raw pulled toward 1, not toward 0.5
        self.assertAlmostEqual(pid.p + pid.integral, 1.2, places=6)

    def test_actuator_held_above_request_pulls_integrator_up(self):
        pid = PID(Gains(kp=0.0, ki=0.0, kd=0, integrator="tracking"))
        pid.integral = 0.2
        for _ in range(40):
            pid.propose(80, 80, 0.25)
            pid.track(0.8, 0.25)        # e.g. a floor keeps the actuator higher
        self.assertGreater(pid.integral, 0.7)

    def test_integration_still_works_in_both_directions(self):
        pid = PID(Gains(kp=0.0, ki=0.1, kd=0))
        pid.integral = 0.5
        for _ in range(20):
            out = pid.propose(85, 80, 0.25)   # above target: integral falls
            pid.track(out, 0.25)
        low = pid.integral
        self.assertLess(low, 0.5)
        for _ in range(20):
            out = pid.propose(75, 80, 0.25)   # below target: integral recovers
            pid.track(out, 0.25)
        self.assertGreater(pid.integral, low)

    def test_conditional_integral_stays_full_while_cool(self):
        # Retired guard semantics (doc/48 §0, D2): saturation below target
        # neither drains nor tracks; downstream limits are ignored.
        pid = PID(Gains(kd=0))          # no derivative: isolate the integrator
        for _ in range(200):
            pid.propose(60, 90, 0.25)   # p = 2.25: saturated far below target
            pid.track(0.5, 0.25)        # entry ratio / ramp holds the actuator
        self.assertEqual(pid.integral, 1.0)
        for temperature in (80.0, 85.0, 88.0, 89.9):
            self.assertEqual(pid.propose(temperature, 90, 0.25), 1.0)  # no early relief
        self.assertEqual(pid.integral, 1.0)

    def test_conditional_integrates_down_above_target_and_recovers(self):
        pid = PID(Gains(kd=0))
        for _ in range(40):
            pid.propose(92, 90, 0.25)
        low = pid.integral
        self.assertLess(low, 1.0)
        for _ in range(40):
            out = pid.propose(89.5, 90, 0.25)
        self.assertGreater(pid.integral, low)  # inside 0..1 below target: recovers
        self.assertGreater(out, 0.0)

    def test_conditional_holds_near_target_under_ripple_slew_and_quantisation(self):
        # Defect 27: the tracking integrator settled ~12 C below target on this
        # synthetic P-cluster plant; the conditional one within ~2 C.
        import random
        def settle(integrator):
            pid, rng = PID(Gains(integrator=integrator)), random.Random(1)
            temperature, cap, tail = 60.0, 1.0, []
            for k in range(1200):  # 300 s at 4 Hz
                sensed = temperature + rng.uniform(-1.5, 1.5)
                out = pid.propose(sensed, 90.0, 0.25)
                if sensed < 90.0 - 12.0:
                    out = 1.0
                cap = min(out, cap + 0.03 * 0.25)          # production recovery slew
                pid.track((int(1378 + cap * 2522) - 1378) / 2522, 0.25)  # int-MHz floor
                temperature += (52 + 44 * cap ** 1.7 - temperature) * (1 - math.exp(-0.25 / 6.0))
                if k >= 720:
                    tail.append(temperature)
            return sum(tail) / len(tail)
        self.assertLess(settle("tracking"), 80.0)
        self.assertGreater(settle("conditional"), 87.5)

    def test_unknown_integrator_rejected(self):
        with self.assertRaises(ValueError):
            Gains(integrator="bang-bang")

    def test_bad_inputs_rejected(self):
        with self.assertRaises(ValueError):
            PID(Gains()).propose(math.nan, 80, 0.25)
        with self.assertRaises(ValueError):
            Gains(ki=-1)


class ControlTests(unittest.TestCase):
    def test_baseline_only_profile_reports_cooldown_on_idle(self):
        controller = Supervisor(Settings(maximum_mhz=1200))
        command = controller.step(Observation(60, 50, 0), 0.25)
        self.assertEqual(command.mode, "COOLDOWN")

    def test_busy_ramp_bounded_for_variable_dt(self):
        controller = Supervisor()
        last = controller.cap
        for dt in [0.1, 0.5, 0.25, 0.75] * 20:
            command = controller.step(Observation(60, 50, 1), dt)
            self.assertLessEqual(command.gpu_cap_mhz - last, 100 * dt + 1e-8)
            self.assertLessEqual(command.gpu_cap_mhz, 1800)
            last = command.gpu_cap_mhz
        self.assertEqual(last, 1800)

    def test_idle_and_new_prefill_rearm(self):
        for observation in [Observation(60, 50, 0), Observation(60, 50, 1, prefill_arrival=True),
                            Observation(60, 50, 0.02, workload_done=True)]:
            controller = Supervisor(Settings(prefill_rearm=True))
            for _ in range(40):
                controller.step(Observation(60, 50, 1), 0.25)
            self.assertEqual(controller.cap, 1800)
            command = controller.step(observation, 0.25)
            if observation.prefill_arrival:
                self.assertEqual(command.gpu_cap_mhz, 1200)
            else:
                self.assertEqual(command.gpu_cap_mhz, 1800 - 150 * 0.25)
            self.assertEqual(controller.busy_s, 0)

    def test_gb10_background_utilisation_counts_as_idle(self):
        # doc/42 defect 36: GB10 reads 8-9 % with nothing running; the cap must
        # still cool down to the entry ceiling without an LLM prefill.
        controller = Supervisor()
        for _ in range(40):
            controller.step(Observation(60, 50, 1), 0.25)
        self.assertEqual(controller.cap, 1800)
        for _ in range(4 * 10):
            command = controller.step(Observation(60, 50, 0.085), 0.25)
        self.assertEqual(command.mode, "COOLDOWN")
        self.assertEqual(command.gpu_cap_mhz, 1200)
        busy = Supervisor(replace(Settings(), idle_util_threshold=0.05))
        for _ in range(40):
            busy.step(Observation(60, 50, 1), 0.25)
        self.assertNotEqual(busy.step(Observation(60, 50, 0.085), 0.25).mode, "COOLDOWN")
        with self.assertRaises(ValueError):
            Settings(idle_util_threshold=0.0)          # must stay above zero

    def test_prefill_during_busy_load_keeps_the_cap_by_default(self):
        # Operator, 28 September 2026: load is detected by GPU utilisation only.
        # A prompt joining a running decode must not drop the cap to the entry
        # ceiling; a cold start still begins at the entry ceiling (idle cooldown).
        controller = Supervisor()
        for _ in range(40):
            controller.step(Observation(60, 50, 1), 0.25)
        self.assertEqual(controller.cap, 1800)
        command = controller.step(Observation(60, 50, 1, prefill_arrival=True, active_jobs=3), 0.25)
        self.assertEqual(command.gpu_cap_mhz, 1800)
        self.assertNotEqual(command.mode, "REARM")
        for _ in range(4 * 10):
            controller.step(Observation(60, 50, 0.05), 0.25)       # idle: back to the entry ceiling
        self.assertEqual(controller.cap, 1200)
        command = controller.step(Observation(60, 50, 1, prefill_arrival=True), 0.25)
        self.assertEqual(command.gpu_cap_mhz, 1200)                 # cold start ramps from the entry ceiling

    def test_one_completed_request_does_not_cool_down_other_busy_work(self):
        controller = Supervisor()
        for _ in range(40):
            controller.step(Observation(60, 50, 1), 0.25)
        self.assertEqual(controller.cap, 1800)
        command = controller.step(Observation(60, 50, 0.85, workload_done=True), 0.25)
        self.assertEqual(command.gpu_cap_mhz, 1800)
        self.assertNotEqual(command.mode, "COOLDOWN")

    def test_owned_active_work_prevents_completion_cooldown_in_brief_util_gap(self):
        controller = Supervisor()
        for _ in range(40):
            controller.step(Observation(60, 50, 1), 0.25)
        command = controller.step(
            Observation(60, 50, 0.02, workload_done=True, active_jobs=2), 0.25)
        self.assertEqual(command.gpu_cap_mhz, 1800)
        self.assertNotEqual(command.mode, "COOLDOWN")
        self.assertEqual(Supervisor().step(
            Observation(60, 50, 0.1, active_jobs=21), 0.25).mode, "FAULT")

    def test_rapid_bursts_cannot_accumulate_busy_dwell(self):
        controller = Supervisor()
        for _ in range(40):
            controller.step(Observation(60, 50, 1), 0.25)
            command = controller.step(Observation(60, 50, 0), 0.25)
            self.assertLessEqual(command.gpu_cap_mhz, 1200)

    def test_zero_load_never_raises_a_derated_cap(self):
        controller = Supervisor()
        controller.cap = 800
        self.assertEqual(controller.step(Observation(60, 50, 0), 0.25).gpu_cap_mhz, 800)

    def test_all_faults_reduce_without_waiting_for_ramp(self):
        samples = [Observation(60, 50, math.nan), Observation(60, 50, 1, valid=False),
                   Observation(60, 50, 1, fan_ok=False), Observation(60, 50, 1, memory_ok=False),
                   Observation(96, 50, 1), Observation(60, 93, 1)]
        for observation in samples:
            controller = Supervisor()
            command = controller.step(observation, 0.25)
            self.assertEqual((command.gpu_cap_mhz, command.cpu_ratio, command.fan_state), (500, 0, 12))
            self.assertEqual(command.mode, "FAULT")
        self.assertEqual(Supervisor().step(Observation(60, 50, 1), 2).mode, "FAULT")

    def test_hot_gpu_derates_even_at_full_load(self):
        controller = Supervisor()
        output = controller.step(Observation(60, 84, 1), 0.25)  # Below the 85 C GPU abort.
        self.assertLess(output.gpu_cap_mhz, 1200)
        self.assertEqual(output.mode, "DERATED")

    def test_class_endpoints(self):
        self.assertEqual(cpu_class_caps(1), (3900, 2808))
        self.assertEqual(cpu_class_caps(0), (1378, 338))
        self.assertEqual(cpu_class_caps(0.75)[1], 2808)

    def test_settings_reject_nonfinite_and_wrong_envelope(self):
        from energy_control.limits import GPU_HARD_MAX_MHZ
        for change in ({"maximum_mhz": GPU_HARD_MAX_MHZ + 1}, {"ramp_mhz_s": math.inf},
                       {"gpu_target_c": 95}, {"cpu_target_c": 96}):
            with self.assertRaises(ValueError):
                Settings(**change)


class PlantTests(unittest.TestCase):
    def test_synthetic_sensor_lag_exposes_hidden_heating(self):
        run = Experiment("queue", plant_parameters=PlantParameters(sensor_tau_s=2.0))
        rows = [run.step() for _ in range(24)]
        self.assertGreater(rows[-1]["gpu_c"], rows[-1]["observed_gpu_c"])
        self.assertEqual(rows[0]["gpu_c"], rows[0]["observed_gpu_c"])
        with self.assertRaises(ValueError):
            PlantParameters(sensor_tau_s=-0.1)
        with self.assertRaises(ValueError):
            PlantParameters(sensor_tau_s=11)

    def test_lagged_indication_cannot_guarantee_physical_abort_boundary(self):
        # Deliberately adverse *synthetic* heat-store state: this checks a
        # limitation of sampled control, not a safe hardware test recipe.
        run = Experiment("queue", plant_parameters=PlantParameters(sensor_tau_s=5))
        run.plant.gpu_c, run.plant.sink_c = 82.0, 87.0
        run.sensed_gpu_c = 82.0
        rows = [run.step() for _ in range(80)]
        self.assertTrue(any(row["gpu_c"] >= 85 and row["observed_gpu_c"] < 85
                            and not row["test_aborted"] for row in rows))

    def test_shared_sink_can_heat_idle_cpu_and_preserves_residual_heat(self):
        # An idle CPU is not thermally isolated from GPU-heated copper.
        plant = Plant(cpu_c=35, gpu_c=45, sink_c=60, fan=1)
        dc, dg, ds = plant.derivatives(0, 0)
        self.assertGreater(dc, 0)
        self.assertGreater(dg, 0)
        self.assertLess(ds, 0)
        command = Command(1200, 0, 12, "COOLDOWN", "synthetic residual heat")
        plant.advance(command, 0, 0, 0.5)
        self.assertGreater(plant.cpu_c, 35)
        self.assertGreater(plant.sink_c, 55)
        self.assertLess(plant.sink_c, 60)

    def test_gpu_load_changes_cpu_temperature_through_shared_sink(self):
        quiet = Plant(cpu_c=25, gpu_c=25, sink_c=25, fan=1)
        loaded = Plant(cpu_c=25, gpu_c=25, sink_c=25, fan=1)
        command = Command(1200, 0.5, 12, "HOLD", "synthetic coupling check")
        for _ in range(240):
            quiet.advance(command, 0, 0, 0.5)
            loaded.advance(command, 1, 0, 0.5)
        self.assertAlmostEqual(quiet.cpu_w, loaded.cpu_w)
        self.assertGreater(loaded.sink_c, quiet.sink_c)
        self.assertGreater(loaded.cpu_c, quiet.cpu_c)

    def test_energy_balance(self):
        plant = Plant()
        dc, dg, ds = plant.derivatives(30, 70)
        p = plant.p
        stored_rate = dc * p.cpu_capacity_j_k + dg * p.gpu_capacity_j_k + ds * p.sink_capacity_j_k
        loss = (p.passive_conductance_w_k + p.fan_conductance_w_k * plant.fan) * (plant.sink_c - p.ambient_c)
        self.assertAlmostEqual(stored_rate, 100 - loss)

    def test_clock_cap_does_not_eliminate_load_power_step(self):
        plant = Plant()
        idle = plant.gpu_w
        plant.advance(Command(1800, 1, 12, "RUN", "test"), 1, 0.2, 1)
        self.assertGreater(plant.gpu_w, idle + 50)

    def test_plant_time_step_convergence(self):
        a, b = Plant(), Plant()
        command = Command(1800, 1, 12, "RUN", "test")
        for _ in range(40):
            a.advance(command, 1, 1, 0.25)
        for _ in range(100):
            b.advance(command, 1, 1, 0.1)
        self.assertLess(abs(a.gpu_c - b.gpu_c), 0.05)
        self.assertLess(abs(a.cpu_c - b.cpu_c), 0.05)

    def test_every_scenario_is_finite_and_bounded(self):
        for scenario in SCENARIOS:
            experiment = Experiment(scenario, Settings(prefill_rearm=True))
            for _ in range(720):
                row = experiment.step()
                self.assertTrue(all(math.isfinite(v) for v in row.values() if isinstance(v, float)))
                self.assertLessEqual(row["gpu_cap_mhz"], 1800)
                self.assertGreaterEqual(row["gpu_cap_mhz"], 500)
                if row["prefill_arrival"]:
                    self.assertLessEqual(row["gpu_cap_mhz"], 1200)

    def test_abort_is_latched_and_stops_synthetic_load(self):
        run = Experiment("queue", Settings(queue_load=QueueLoad(20, 20, 20)))
        run.plant.cpu_c = 96
        first = run.step()
        self.assertTrue(first["test_aborted"])
        self.assertEqual((first["cpu_util_pct"], first["gpu_util_pct"]), (0, 0))
        second = run.step()
        self.assertEqual((second["cpu_util_pct"], second["gpu_util_pct"]), (0, 0))

    def test_predicted_breach_aborts_before_observed_abort(self):
        controller = Supervisor()
        controller.step(Observation(91, 70, 1), 0.25)
        command = controller.step(Observation(95.5, 70, 1), 0.25)
        self.assertEqual(command.mode, "FAULT")

    def test_tui_edits_remain_valid_at_boundaries(self):
        settings, plant = Settings(), Plant().p
        for index in range(len(PARAMETERS)):
            for direction in (-1, 1):
                for _ in range(150):
                    settings, plant = edit_parameter(settings, plant, index, direction)
        self.assertIsInstance(settings, Settings)


if __name__ == "__main__":
    unittest.main()
