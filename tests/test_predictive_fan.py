"""Predictive fan (fan_policy "predictive"): feed-forward from the expected
power through the fitted cooler, feedback from the loops' projections, up at
once and down gently; the operator's floor is a hard minimum. Synthetic only."""
from dataclasses import replace
import unittest

from simulation.model import GB10_GPU_MATMUL_W, Observation, Settings, Supervisor


def settings(**changes):
    base = Settings(maximum_mhz=2200, baseline_mhz=1700, fan_policy="predictive", fan_min_state=2)
    return replace(base, **changes)


def run(controller, seconds, *, gpu_util=0.05, gpu_w=5.5, cpu_w=4.5, cool=40.0, zone=None,
        prefill=False):
    command = None
    for tick in range(int(seconds * 4)):
        command = controller.step(Observation(
            cool, cool, gpu_util, gpu_w=gpu_w, cpu_w=cpu_w, prefill_arrival=prefill and tick == 0,
            gpu_zone=zone or (cool, cool)), 0.25)
    return command


class PredictiveFanTests(unittest.TestCase):
    def test_idle_steps_down_gently_to_the_floor(self):
        controller = Supervisor(settings())
        controller.fan_state = 12
        levels = [run(controller, 60).fan_state for _ in range(12)]
        self.assertEqual(levels[0], 11)                  # one level per 60 s (fan_release_step_s)
        self.assertEqual(levels[-1], 2)
        self.assertTrue(all(a - b in (0, 1) for a, b in zip(levels, levels[1:])))

    def test_operator_floor_is_a_hard_minimum(self):
        controller = Supervisor(settings(fan_min_state=5))
        controller.fan_state = 12
        self.assertEqual(run(controller, 600).fan_state, 5)

    def test_load_start_spins_up_before_the_heat_arrives(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        run(controller, 60)                               # idle, cool
        command = run(controller, 0.5, gpu_util=1.0, gpu_w=6.0, prefill=True)   # power not yet up
        self.assertGreaterEqual(command.fan_state, 10)    # anticipation at the entry cap
        self.assertTrue(controller.fan_info["anticipating"])
        self.assertGreaterEqual(controller.fan_info["expected_w"], GB10_GPU_MATMUL_W(1700) - 1)

    def test_a_short_gpu_blip_is_no_load_start(self):
        # Live, 27 September 2026: a 22 % utilisation blip (dashboard restart)
        # raised the anticipation and held the fan at 12 for minutes.
        controller = Supervisor(settings())
        controller.fan_state = 2
        run(controller, 60)
        command = run(controller, 1, gpu_util=0.22, gpu_w=5.7)
        self.assertEqual(command.fan_state, 2)
        self.assertFalse(controller.fan_info["anticipating"])

    def test_moderate_load_gets_an_intermediate_level(self):
        controller = Supervisor(settings())
        controller.fan_state = 12
        command = run(controller, 600, gpu_util=0.9, gpu_w=30.0, cpu_w=8.0, cool=55.0)
        # 50 W with background: the steady plate estimate needs a share of ~0.45.
        self.assertTrue(4 <= command.fan_state <= 8, command.fan_state)

    def test_projection_near_a_setpoint_adds_fan_before_the_clock_loops_cut(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        setpoint = controller.gpu_zone_loop.setpoint
        command = run(controller, 2, zone=(setpoint - 4.0, setpoint - 2.0))
        self.assertEqual(command.fan_state, 12)

    def test_near_abort_forces_full_fan_and_the_old_policies_stay(self):
        controller = Supervisor(settings())
        controller.fan_state = 2
        self.assertEqual(run(controller, 1, zone=(94.0, 95.5)).fan_state, 12)
        with self.assertRaises(ValueError):
            settings(fan_fb_full_c=20.0)                  # full must lie inside the band
        self.assertEqual(Settings().fan_policy, "load")


if __name__ == "__main__":
    unittest.main()
