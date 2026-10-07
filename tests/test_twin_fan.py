"""Twin fan (fan_policy "twin", 6 October 2026): the level against the new
cooler's twin for a smoothed power, target TGPU 70 C, up only as far as needed,
down one level per fan_release_step_s with hysteresis, a slow bias from the
measured TGPU; feedback in a narrow band and 12 near an abort. Synthetic only."""
from dataclasses import replace
import unittest

from energy_control.broker import FAN_POLICIES, TUNABLES, normalize_tuning
from simulation.model import Observation, Settings, Supervisor


def settings(**changes):
    base = Settings(maximum_mhz=2500, baseline_mhz=1700, fan_policy="twin", fan_min_state=2)
    return replace(base, **changes)


def run(controller, seconds, *, gpu_util=0.9, gpu_w=29.0, cpu_w=4.0, tgpu=None, cool=45.0,
        zone_projection=None):
    command = None
    for _ in range(int(seconds * 4)):
        t = tgpu if tgpu is not None else cool
        command = controller.step(Observation(
            cool, cool - 5, gpu_util, gpu_w=gpu_w, cpu_w=cpu_w,
            gpu_zone=(t, zone_projection if zone_projection is not None else t)), 0.25)
    return command


def changes(levels):
    return sum(1 for a, b in zip(levels, levels[1:]) if a != b)


class TwinFanTests(unittest.TestCase):
    def test_llm_load_settles_at_the_floor_from_12(self):
        # 29 W of LLM decode (live average, 5-6 October): the new cooler holds
        # TGPU near 57 C at fan 2, well under 70 C.
        controller = Supervisor(settings())
        controller.fan_state = 12
        levels = [run(controller, 60, tgpu=50.0).fan_state for _ in range(12)]
        self.assertEqual(levels[0], 11)                 # one level per minute
        self.assertEqual(levels[-1], 2)
        self.assertTrue(all(a - b in (0, 1) for a, b in zip(levels, levels[1:])))

    def test_bursts_do_not_bounce_the_fan(self):
        # LLM bursts: 25 W base, 60 W for 30 s every 3 min (the predictive fan
        # peak-held them and jumped back to 12).
        controller = Supervisor(settings())
        controller.fan_state = 2
        run(controller, 150, gpu_w=25.0, tgpu=55.0)     # settled before the first burst
        levels = []
        for _ in range(10):
            levels.append(run(controller, 30, gpu_w=60.0, tgpu=62.0).fan_state)
            levels.append(run(controller, 150, gpu_w=25.0, tgpu=55.0).fan_state)
        self.assertLessEqual(max(levels), 4)
        self.assertLessEqual(changes(levels), 4)

    def test_heavy_load_raises_the_fan_only_as_far_as_needed(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        moderate = run(controller, 180, gpu_w=55.0, tgpu=68.0).fan_state
        self.assertGreater(moderate, 2)
        self.assertLess(moderate, 12)
        heavy = run(controller, 180, gpu_w=81.0, tgpu=72.0).fan_state
        self.assertEqual(heavy, 12)                     # burn-in at 2.5 GHz needs full fan

    def test_hysteresis_holds_the_level_near_the_target(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        run(controller, 300, gpu_w=55.0, tgpu=68.0)
        level = controller.fan_state
        self.assertGreater(level, 2)
        # A slightly lower load must not step it down: the next lower level's
        # steady TGPU is not 3 K below the target.
        self.assertEqual(run(controller, 600, gpu_w=52.0, tgpu=67.0).fan_state, level)

    def test_bias_learns_a_warmer_room_and_raises_the_level(self):
        cold, warm = Supervisor(settings()), Supervisor(settings())
        cold.fan_state = warm.fan_state = 2
        run(cold, 0.25, gpu_w=40.0)
        run(warm, 0.25, gpu_w=40.0)
        for _ in range(120):   # 30 min: TGPU equal to the twin, and 6 K warmer than it
            run(cold, 15, gpu_w=40.0, tgpu=cold.fan_twin_plate + 0.06 * 40.0)
            run(warm, 15, gpu_w=40.0, tgpu=warm.fan_twin_plate + 0.06 * 40.0 + 6.0)
        self.assertAlmostEqual(cold.fan_twin_bias, 0.0, delta=0.5)
        self.assertGreater(warm.fan_twin_bias, 4.0)
        self.assertLessEqual(warm.fan_twin_bias, warm.s.fan_twin_bias_max_c)
        self.assertGreaterEqual(warm.fan_state, cold.fan_state)

    def test_feedback_near_a_setpoint_adds_fan(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        # TGPU zone projection 2 K below its setpoint: inside the 6 K band.
        setpoint = controller.gpu_zone_loop.setpoint
        command = run(controller, 2, gpu_w=29.0, tgpu=55.0, zone_projection=setpoint - 2.0)
        self.assertGreater(command.fan_state, 2)
        self.assertGreater(controller.fan_info["feedback"], 0)

    def test_near_abort_goes_to_12(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        command = run(controller, 1, cool=94.5, tgpu=60.0)
        self.assertEqual(command.fan_state, 12)

    def test_operator_floor_is_a_hard_minimum(self):
        controller = Supervisor(settings(fan_min_state=5))
        controller.fan_state = 12
        self.assertEqual(run(controller, 900, gpu_w=10.0, tgpu=45.0).fan_state, 5)

    def test_fan_info_reports_the_twin(self):
        controller = Supervisor(settings())
        run(controller, 5)
        info = controller.fan_info
        for key in ("steady_tgpu_c", "target_c", "bias_c", "twin_plate_c", "feed", "feedback", "target"):
            self.assertIn(key, info)
        self.assertEqual(info["target_c"], 70.0)

    def test_settings_validation(self):
        with self.assertRaises(ValueError):
            settings(fan_temp_target_c=95.0)
        with self.assertRaises(ValueError):
            settings(fan_twin_rise_s=300.0, fan_twin_fall_s=60.0)
        with self.assertRaises(ValueError):
            settings(fan_policy="nonsense")

    def test_broker_accepts_the_policy_and_its_tunables(self):
        self.assertIn("twin", FAN_POLICIES)
        tuning = dict(normalize_tuning({"fan_temp_target_c": 68.0, "fan_twin_down_margin_c": 4.0}))
        self.assertEqual(tuning["fan_temp_target_c"], 68.0)
        for name in ("fan_temp_target_c", "fan_twin_neck_w_k", "fan_twin_bias_tau_s", "fan_twin_fb_band_c"):
            self.assertIn(name, TUNABLES)


if __name__ == "__main__":
    unittest.main()
