"""Thermal control v3, Stage A (doc/48 §0): guard-margin setpoint, policy wiring,
operator surfaces and the ripple twin. Synthetic only, no hardware claims."""
from dataclasses import replace
import unittest

from energy_control.broker import Config
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.safety import LENOVO_REQUIRED_TEMPERATURES, Temperature
from simulation.model import Observation, Settings, Supervisor
from test_safety import good_snapshot


def snapshot(zones=None, *, monotonic_s=1.0, default=(70.0, 0.0, 70.0)):
    """Required sensors; ``zones`` maps name -> (raw, rise C/s, trend)."""
    zones = zones or {}
    temperatures = []
    for name in sorted(LENOVO_REQUIRED_TEMPERATURES):
        raw, rise, trend = zones.get(name, (65.0, 0.0, 65.0) if name == "gpu" else default)
        temperatures.append(Temperature(name, raw, 0.1, rise, trend))
    return good_snapshot(temperatures=tuple(temperatures), monotonic_s=monotonic_s)


class GuardProjectionWiringTests(unittest.TestCase):
    def test_projection_uses_the_guard_formula_within_its_band(self):
        projection, basis = ShadowPolicy._cpu_projection(snapshot({
            "acpi_ts1p": (88.0, 1.0, 87.5),     # in the band: 87.5 + 2 x 1.0
            "acpi_ts0p": (84.0, 3.0, 82.0),     # below the band: trend only
        }))
        self.assertEqual((projection, basis), (89.5, 87.5))

    def test_gpu_is_not_a_cpu_zone(self):
        projection, _ = ShadowPolicy._cpu_projection(snapshot({"gpu": (84.0, 5.0, 84.0)}))
        self.assertEqual(projection, 70.0)

    def test_control_state_reports_setpoint_and_last_second_ripple(self):
        policy = ShadowPolicy(Config())
        for tick, raw in enumerate((80.0, 83.0, 80.5, 80.0)):
            result = policy.step(PolicyInput(snapshot({"acpi_ts1p": (raw, 0.5, 80.0)},
                                                      monotonic_s=1.0 + 0.25 * tick), 0, 0.25))
            self.assertFalse(result.abort_owned_loads)
        state = policy.control_state()
        self.assertEqual(state["cpu_spike_max_1s_c"], 3.0)
        self.assertEqual(state["cpu_setpoint_c"], 89.0)  # ceiling below the no-confirmation zone
        self.assertEqual((state["fan_policy"], state["pid_integrator"]), ("load", "conditional"))

    def test_policy_near_miss_backs_the_setpoint_off(self):
        policy = ShadowPolicy(Config())
        for tick in range(8):  # 2 s with the projection above 96 - 2 C
            policy.step(PolicyInput(snapshot({"acpi_ts1p": (91.0, 1.5, 91.0)},
                                             monotonic_s=1.0 + 0.25 * tick), 0, 0.25))
        self.assertAlmostEqual(policy.control_state()["cpu_setpoint_c"], 87.0)  # 89 - 2 s x 1 C/s

    def test_raising_the_target_does_not_cut_the_cpu(self):
        # Bumpless on the effective setpoint (it recovers slowly toward a raised target).
        policy = ShadowPolicy(Config(cpu_target_c=86.0))
        for tick in range(4):
            policy.step(PolicyInput(snapshot({"acpi_ts1p": (85.0, 0.0, 85.0)},
                                             monotonic_s=1.0 + 0.25 * tick), 0, 0.25))
        before = policy.supervisor.cpu.propose(85.0, policy.supervisor.cpu_setpoint, 0.25)
        policy.update_config(Config(cpu_target_c=90.0))
        self.assertEqual(policy.supervisor.cpu_setpoint, 86.0)
        after = policy.supervisor.cpu.propose(85.0, policy.supervisor.cpu_setpoint, 0.25)
        self.assertGreaterEqual(after, before - 1e-9)

    def test_lowering_the_target_takes_effect_at_once(self):
        policy = ShadowPolicy(Config())
        policy.update_config(Config(cpu_target_c=84.0))
        self.assertEqual(policy.supervisor.cpu_setpoint, 84.0)


class SupervisorGuardMirrorTests(unittest.TestCase):
    def test_predicted_breach_mirrors_the_guard_on_its_own_quantities(self):
        # Guard rule: immediate only when the worst zone's trend is >= 90 C.
        quick = Supervisor()
        spike = Observation(92.5, 60, 1, active_jobs=2, cpu_projected_c=96.2,
                            cpu_projected_basis_c=92.5)
        self.assertNotEqual(quick.step(spike, 0.25).mode, "FAULT")   # needs 1 s confirmation
        near = Supervisor()
        self.assertEqual(near.step(Observation(93.2, 60, 1, active_jobs=2, cpu_projected_c=96.2,
                                               cpu_projected_basis_c=93.2), 0.25).mode, "FAULT")
        sustained = Supervisor()
        modes = [sustained.step(spike, 0.25).mode for _ in range(5)]
        self.assertEqual(modes[-1], "FAULT")

    def test_filtered_slope_alone_no_longer_faults_when_guard_quantities_exist(self):
        controller = Supervisor()
        controller.step(Observation(88.0, 60, 1, active_jobs=2, cpu_projected_c=88.0,
                                    cpu_projected_basis_c=88.0), 0.25)
        # +2 C trend step: the PID's filtered slope projects past 93, the guard's does not.
        result = controller.step(Observation(90.2, 60, 1, active_jobs=2, cpu_projected_c=91.0,
                                             cpu_projected_basis_c=90.2), 0.25)
        self.assertNotEqual(result.mode, "FAULT")

    def test_basis_without_projection_fails_closed(self):
        result = Supervisor().step(Observation(80, 60, 1, cpu_projected_basis_c=80.0), 0.25)
        self.assertEqual(result.mode, "FAULT")


class OperatorSurfaceTests(unittest.TestCase):
    def test_cli_accepts_the_v3_fields(self):
        from energy_control.cli import _parser
        args = _parser().parse_args(["--fan-policy", "staging", "--pid-integrator", "tracking",
                                     "--fan-load-state", "10", "--guard-margin-c", "3",
                                     "--fan-idle-delay-s", "120"])
        self.assertEqual((args.fan_policy, args.pid_integrator, args.fan_load_state,
                          args.guard_margin_c, args.fan_idle_delay_s),
                         ("staging", "tracking", 10, 3.0, 120.0))
        import contextlib, io
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            _parser().parse_args(["--fan-policy", "loud"])

    def test_v3_fields_are_proposable(self):
        from energy_control.broker import _FIELDS
        self.assertTrue({"fan_policy", "fan_load_state", "fan_idle_delay_s", "guard_margin_c",
                         "pid_integrator"} <= _FIELDS)

    def test_config_reaches_the_controller(self):
        settings = ShadowPolicy._settings(Config(fan_policy="staging", fan_load_state=9,
                                                 fan_idle_delay_s=120, guard_margin_c=3.0,
                                                 pid_integrator="tracking"))
        self.assertEqual((settings.fan_policy, settings.fan_load_state, settings.fan_idle_delay_s,
                          settings.guard_margin_c, settings.cpu_gains.integrator,
                          settings.gpu_gains.integrator),
                         ("staging", 9, 120, 3.0, "tracking", "tracking"))


class RippleTwinTests(unittest.TestCase):
    def _run(self, legacy=False, **config):
        from simulation import cluster_twin
        settings = ShadowPolicy._settings(Config(gpu_max_mhz=2000, gpu_entry_mhz=1700,
                                                 fan_min_state=2, **config))
        if legacy:  # the goal-v2 controller: 2.5 C derate band, no taper, no ceiling
            settings = replace(settings, guard_band_c=2.5, recovery_taper_min=1.0,
                               trend_margin_c=0.001, cpu_job_util=2.0)  # and no job costs
        rows, events = cluster_twin.run(settings, seconds=600, seed=2,
                                        cluster_control=config.get("cpu_control") != "class")
        return cluster_twin.summarise(rows, events, steady_from_s=240)

    def test_defect_27_plateau_and_its_fix(self):
        old = self._run(legacy=True, pid_integrator="tracking", fan_policy="staging",
                        cpu_control="class")
        new = self._run()
        self.assertLess(old["hottest_mean_c"], 83.0)            # plateau ~9 C below 90
        self.assertEqual(new["guard_events"], 0)                # guard stays quiet at 90
        self.assertIsNone(new["fault"])
        self.assertGreater(new["hottest_mean_c"], old["hottest_mean_c"] + 3.0)
        self.assertGreater(new["throughput_mean"], old["throughput_mean"] * 1.08)
        self.assertEqual(new["fan_last"], 12)


if __name__ == "__main__":
    unittest.main()
