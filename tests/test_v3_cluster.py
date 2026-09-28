"""Thermal control v3, Stage B (doc/48 §0, D5/D7): per-cluster CPU loops and caps,
workload priorities in the power balance. Synthetic only, no hardware claims."""
import unittest

from energy_control.broker import Config
from energy_control.policy import PolicyInput, ProposedLimits, ShadowPolicy
from simulation.model import Observation, Settings, Supervisor, cluster_caps_mhz
from test_v3_control import snapshot


def hot_p1(policy, ticks=120, p1=(88.5, 0.0, 88.5), p0=(80.0, 0.0, 80.0)):
    result = None
    for tick in range(ticks):
        result = policy.step(PolicyInput(snapshot({"acpi_ts1p": p1, "acpi_ts0p": p0,
                                                   "acpi_tsoc": p1},
                                                  monotonic_s=1.0 + 0.25 * tick), 0, 0.25,
                                         cpu_demand_active=True, cpu_util_pct=90.0))
        assert not result.abort_owned_loads, result.reasons
    return result


class ClusterControlTests(unittest.TestCase):
    def test_hot_cluster_is_capped_alone(self):
        # 100 s: the cool P0 climbs from the entry clamp through the 15 C taper band.
        result = hot_p1(ShadowPolicy(Config()), ticks=400)
        e0, p0, e1, p1 = result.cpu_clusters()
        self.assertLess(p1, p0)                      # only the hot P1 is cut
        self.assertEqual(p0, 3900)                   # the cool P0 keeps full clock
        self.assertEqual((result.cpu_fast_max_mhz, result.cpu_slow_max_mhz), (p0, max(e0, e1)))
        self.assertEqual(result.cpu_cluster_max_mhz, (e0, p0, e1, p1))

    def test_class_control_keeps_uniform_caps(self):
        result = hot_p1(ShadowPolicy(Config(cpu_control="class")))
        self.assertIsNone(result.cpu_cluster_max_mhz)
        e0, p0, e1, p1 = result.cpu_clusters()
        self.assertEqual((e0, p0), (e1, p1))

    def test_e_cluster_follows_its_p_neighbour_below_75_percent(self):
        controller = Supervisor(Settings())
        controller.previous_cpu_demand = True
        hot = ((60.0, 60.0), (95.0, 95.0), (60.0, 60.0), (60.0, 60.0))  # P0 far above
        command = None
        for _ in range(40):
            command = controller.step(Observation(89.0, 50, 0, cpu_demand_active=True,
                                                  cpu_zones=hot), 0.25)
        e0, p0, e1, p1 = command.cluster_ratios
        self.assertLess(p0, 0.75)
        self.assertLessEqual(e0, p0 / 0.75 + 1e-9)   # fast-first per side
        self.assertEqual((e1, p1), (1.0, 1.0))       # the other side is untouched

    def test_cluster_zones_are_validated(self):
        bad = ((60.0, 59.0), (60.0, 60.0), (60.0, 60.0), (60.0, 60.0))  # projection < trend
        self.assertEqual(Supervisor().step(Observation(60, 50, 0, cpu_zones=bad), 0.25).mode,
                         "FAULT")
        short = ((60.0, 60.0),) * 3
        self.assertEqual(Supervisor().step(Observation(60, 50, 0, cpu_zones=short), 0.25).mode,
                         "FAULT")

    def test_cluster_caps_mapping(self):
        self.assertEqual(cluster_caps_mhz((0.0, 0.0, 1.0, 1.0)), (338, 1378, 2808, 3900))

    def test_uniform_limits_expand_to_clusters(self):
        limits = ProposedLimits(1700, 3000, 2000, 12, False, "RUN", ())
        self.assertEqual(limits.cpu_clusters(), (2000, 3000, 2000, 3000))


class WorkloadPriorityTests(unittest.TestCase):
    def costs(self, settings=None, **observation):
        controller = Supervisor(settings or Settings())
        return controller._workload_costs(Observation(70, 60, **observation)), controller.workloads

    def test_inactive_workloads_cost_nothing(self):
        (cost_c, cost_g), loads = self.costs(gpu_util=0.0, active_jobs=0, cpu_util=0.02)
        self.assertEqual((loads["gpu_active"], loads["cpu_jobs_active"]), (False, False))
        self.assertAlmostEqual(cost_c, 0.01)
        self.assertAlmostEqual(cost_g, 0.01)

    def test_llm_only_prefers_cpu_cuts_cpu_only_prefers_gpu_cuts(self):
        (llm_c, llm_g), _ = self.costs(gpu_util=0.9, active_jobs=4, cpu_util=0.05)
        self.assertLess(llm_c, llm_g)
        (job_c, job_g), _ = self.costs(gpu_util=0.0, active_jobs=0, cpu_util=0.9)
        self.assertGreater(job_c, job_g)

    def test_priorities_shift_the_split(self):
        both = dict(gpu_util=0.9, active_jobs=4, cpu_util=0.9)
        (c_llm_first, g_llm_first), _ = self.costs(Settings(priority_gpu=4.0, priority_cpu=1.0), **both)
        (c_cpu_first, g_cpu_first), _ = self.costs(Settings(priority_gpu=1.0, priority_cpu=4.0), **both)
        self.assertGreater(g_llm_first / c_llm_first, g_cpu_first / c_cpu_first)

    def test_a_slowed_load_gets_dearer(self):
        # Proportional fairness: weight = priority / current relative speed.
        fresh = Supervisor(Settings())
        slowed = Supervisor(Settings())
        slowed.cpu_cap = 0.4
        observation = Observation(70, 60, 0.0, active_jobs=0, cpu_util=0.9)
        fresh_c, _ = fresh._workload_costs(observation)
        slowed_c, _ = slowed._workload_costs(observation)
        self.assertGreater(slowed_c, fresh_c)

    def test_priority_fields_bounded_and_wired(self):
        Config(priority_gpu=0.1, priority_cpu=10)
        for changes in ({"priority_gpu": 0.05}, {"priority_cpu": 11}, {"cpu_control": "zone"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Config(**changes)
        settings = ShadowPolicy._settings(Config(priority_gpu=3.0, priority_cpu=0.5))
        self.assertEqual((settings.priority_gpu, settings.priority_cpu), (3.0, 0.5))
        from energy_control.cli import _parser
        args = _parser().parse_args(["--priority-gpu", "3", "--priority-cpu", "0.5",
                                     "--cpu-control", "class"])
        self.assertEqual((args.priority_gpu, args.priority_cpu, args.cpu_control), (3.0, 0.5, "class"))

    def test_control_state_reports_workloads_and_cluster_setpoints(self):
        policy = ShadowPolicy(Config())
        hot_p1(policy, ticks=8)
        state = policy.control_state()
        self.assertEqual(len(state["cluster_setpoints_c"]), 4)
        self.assertEqual(state["priorities"], {"gpu": 1.0, "cpu": 1.0})
        self.assertEqual(state["cpu_control"], "cluster")


class TopologyTests(unittest.TestCase):
    def test_cluster_topology(self):
        from energy_control.cpu_frequency import CLUSTER_NAMES, cluster_of
        self.assertEqual(CLUSTER_NAMES, ("E0", "P0", "E1", "P1"))
        self.assertEqual([cluster_of(f"policy{i}") for i in (0, 5, 10, 15, 19)],
                         [("E0", "slow"), ("P0", "fast"), ("E1", "slow"), ("P1", "fast"),
                          ("P1", "fast")])

    def test_status_reports_cluster_caps(self):
        from types import SimpleNamespace
        from energy_control.service import cpu_class_caps, default_service_config, status_payload
        self.assertEqual(cpu_class_caps((2000, 3000, 2400, 3500)), (2400, 3500))
        self.assertEqual(cpu_class_caps((2808, 3900)), (2808, 3900))
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_TS0P", 71.0),),
            gpu=SimpleNamespace(temperature_c=48.0, measured_mhz=1768.0, utilization_pct=92.0,
                                reported_power_w=16.8),
            fan=SimpleNamespace(rpm=(7700, 7600)), cpu_util_pct=30.0,
            cpu_policies=(SimpleNamespace(measured_mhz=3800.0, hardware_max_mhz=3900.0),))
        payload = status_payload(readout, {"gpu": 1800, "cpu": (2000, 3000, 2400, 3500),
                                           "fan": (12,)},
                                 SimpleNamespace(mode="RUN", reasons=()), default_service_config(),
                                 "ab" * 16, {})
        self.assertEqual(payload["cpu"]["caps_mhz"], {"slow": 2400, "fast": 3500})
        self.assertEqual(payload["cpu"]["cluster_caps_mhz"],
                         {"E0": 2000, "P0": 3000, "E1": 2400, "P1": 3500})


if __name__ == "__main__":
    unittest.main()


class VendorWatchTests(unittest.TestCase):
    def readout(self, p0_measured, gpu_measured=1990.0, gpu_util=95.0, others=3600.0):
        from types import SimpleNamespace
        policies = tuple(SimpleNamespace(index=i, measured_mhz=(
            p0_measured if 5 <= i < 10 else others if 15 <= i < 20 else 2808.0)) for i in range(20))
        return SimpleNamespace(cpu_policies=policies,
                               gpu=SimpleNamespace(measured_mhz=gpu_measured, utilization_pct=gpu_util))

    def test_busy_cluster_below_cap_raises_after_hold_and_clears(self):
        from energy_control.service import VendorWatch
        watch = VendorWatch(hold_s=5.0, settle_s=0.0)
        applied = {"cpu": (2808, 3600, 2808, 3600), "gpu": 2000}
        busy = {"E0": 99.0, "P0": 99.0, "E1": 99.0, "P1": 99.0}
        first = watch.check(self.readout(3300.0), applied, busy, 0.0)
        self.assertEqual(first["P0"]["deficit_mhz"], 300)
        self.assertFalse(first["P0"]["vendor_throttle"])        # not held long enough yet
        later = watch.check(self.readout(3300.0), applied, busy, 5.0)
        self.assertTrue(later["P0"]["vendor_throttle"])
        self.assertTrue(later["any_vendor_throttle"])
        self.assertFalse(later["P1"]["vendor_throttle"])        # P1 runs at its cap
        watch.check(self.readout(3600.0), applied, busy, 6.0)   # back to 1:1
        self.assertTrue(watch.check(self.readout(3600.0), applied, busy, 8.0)["P0"]["vendor_throttle"])
        cleared = watch.check(self.readout(3600.0), applied, busy, 11.0)
        self.assertFalse(cleared["P0"]["vendor_throttle"])       # cleared after the hold

    def test_light_load_below_cap_is_normal(self):
        from energy_control.service import VendorWatch
        watch = VendorWatch(hold_s=1.0, settle_s=0.0)
        idle = {"E0": 10.0, "P0": 20.0, "E1": 10.0, "P1": 20.0}
        for t in range(5):
            result = watch.check(self.readout(1400.0, gpu_measured=500.0, gpu_util=3.0),
                                 {"cpu": (2808, 3900, 2808, 3900), "gpu": 1700}, idle, float(t))
        self.assertFalse(result["any_vendor_throttle"])

    def test_busy_gpu_under_cap_trips(self):
        from energy_control.service import VendorWatch
        watch = VendorWatch(hold_s=1.0, settle_s=0.0)
        applied = {"cpu": (2808, 3900, 2808, 3900), "gpu": 2000}
        for t in range(3):
            result = watch.check(self.readout(3900.0, gpu_measured=1500.0, gpu_util=98.0),
                                 applied, {"P0": 99.0}, float(t))
        self.assertTrue(result["gpu"]["vendor_throttle"])


    def test_a_ramping_cap_is_not_a_vendor_limit(self):
        # Live, 2200 MHz ladder: the measured GPU clock lags a ramping cap by 1-3 s.
        from energy_control.service import VendorWatch
        watch = VendorWatch(hold_s=5.0)                  # settle 3 s
        busy = {"E0": 99.0, "P0": 99.0, "E1": 99.0, "P1": 99.0}
        result = None
        for t in range(20):                              # cap steps +100 MHz every second
            cap = 1700 + 100 * (t % 6)
            result = watch.check(self.readout(3600.0, gpu_measured=cap - 150.0),
                                 {"cpu": (2808, 3600, 2808, 3600), "gpu": cap}, busy, float(t))
            self.assertFalse(result["gpu"]["vendor_throttle"])
        for t in range(20, 29):                          # steady cap, real deficit: flagged
            result = watch.check(self.readout(3600.0, gpu_measured=2000.0),
                                 {"cpu": (2808, 3600, 2808, 3600), "gpu": 2200}, busy, float(t))
        self.assertTrue(result["gpu"]["vendor_throttle"])


class BurstReliefTests(unittest.TestCase):
    def test_single_thread_burst_far_below_the_limit_is_not_cut(self):
        # Live 27 September: TS1P 53 -> 73 C in 1 s (projection 70 C) cut P1 to 1860 MHz.
        controller = Supervisor(Settings())
        controller.previous_cpu_demand = True
        calm = ((45.0, 45.0), (50.0, 50.0), (45.0, 45.0), (53.0, 53.0))
        burst = ((45.0, 45.0), (50.0, 50.0), (45.0, 45.0), (73.0, 75.0))
        light = (0.1, 0.3, 0.1, 0.38)                       # the live burst: P1 at 38 %
        for _ in range(8):
            controller.step(Observation(53.0, 45, 0, cpu_demand_active=True, cpu_zones=calm,
                                        cluster_util=light), 0.25)
        command = controller.step(Observation(73.0, 45, 0, cpu_demand_active=True, cpu_zones=burst,
                                              cluster_util=light), 0.25)
        settings = Settings()   # a light P cluster waits at its base cap; the burst cuts nothing below it
        self.assertAlmostEqual(command.cluster_ratios[3],
                               settings.learned_cap_initial_p - settings.partial_cap_margin)
        self.assertEqual(command.cluster_ratios[2], 1.0)

    def test_approach_ramp_slows_with_projection_headroom(self):
        # Live 15:17: full-rate ramps (~72 MHz/s) heated busy P zones ~2.4 C/s into
        # the limit. The upward rate now scales with headroom over a 15 C band.
        settings = Settings()
        def rise(projection):
            controller = Supervisor(settings)
            controller.previous_cpu_demand = True
            for loop in controller.clusters:
                loop.cap = 0.6
            zones = ((60.0, 60.0), (60.0, 60.0), (60.0, 60.0), (projection - 0.5, projection))
            command = controller.step(Observation(projection, 45, 0, cpu_demand_active=True,
                                                  cpu_zones=zones,
                                                  cluster_util=(0.99,) * 4), 0.25)
            return command.cluster_ratios[3] - 0.6
        from simulation.model import cpu_setpoint_ceiling
        setpoint = cpu_setpoint_ceiling(settings)
        far, mid, near = rise(60.0), rise(setpoint - 7.5), rise(setpoint + 0.5)
        self.assertAlmostEqual(far, settings.cpu_recovery_s * 0.25)                 # full rate
        self.assertAlmostEqual(mid, settings.cpu_recovery_s * 0.25 * 7.5 / 15.0)    # half rate
        self.assertAlmostEqual(near, settings.cpu_recovery_s * 0.25 * settings.recovery_taper_min)

    def test_projection_spike_alone_cuts_to_entry_not_minimum(self):
        controller = Supervisor(Settings())
        controller.previous_cpu_demand = True
        controller.step(Observation(84.0, 45, 0, cpu_demand_active=True), 0.25)
        spike = controller.step(Observation(86.0, 45, 0, cpu_demand_active=True), 0.25)
        self.assertGreaterEqual(spike.cpu_ratio, Settings().cpu_entry_ratio - 1e-9)

    def test_approach_near_the_setpoint_is_still_braked(self):
        controller = Supervisor(Settings())
        controller.previous_cpu_demand = True
        command = None   # projection past the setpoint (89 C) - 2
        for step in range(4):
            zones = ((60.0, 60.0), (60.0, 60.0), (60.0, 60.0), (87.0 + step, 89.5 + step))
            command = controller.step(Observation(87.0 + step, 45, 0, cpu_demand_active=True,
                                                  cpu_zones=zones), 0.25)
        self.assertLess(command.cluster_ratios[3], 1.0)


class CombinedLoadFixTests(unittest.TestCase):
    def test_cpu_integral_winds_down_faster_above_target(self):
        from simulation.model import Gains, PID
        slow, fast = PID(Gains(kd=0)), PID(Gains(kd=0, wind_down_factor=3.0))
        for pid in (slow, fast):
            for _ in range(8):
                pid.propose(88.0, 86.0, 0.25)
        self.assertAlmostEqual(1.0 - fast.integral, 3 * (1.0 - slow.integral))
        with self.assertRaises(ValueError):
            Gains(wind_down_factor=0.5)
        settings = ShadowPolicy._settings(Config())
        self.assertEqual(settings.cpu_gains.wind_down_factor, 3.0)
        self.assertEqual(settings.gpu_gains.wind_down_factor, 1.0)   # GPU loop unchanged

    def test_gpu_ramp_gate_is_75_percent(self):
        settings = ShadowPolicy._settings(Config(gpu_max_mhz=2000, gpu_entry_mhz=1700))
        self.assertEqual(settings.busy_threshold, 0.75)
        controller = Supervisor(settings)
        controller.cap = settings.baseline_mhz
        command = None
        for _ in range(12):   # LLM decode at 82 % utilisation ramps above the entry ceiling
            command = controller.step(Observation(60, 55, 0.82, active_jobs=12), 0.25)
        self.assertGreater(command.gpu_cap_mhz, settings.baseline_mhz)
        for value in (0.2, 1.1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(gpu_busy_threshold=value)


class ControlledClockEnvelopeTests(unittest.TestCase):
    """Operator, 27 September: the CPU is always in a controlled clock envelope
    like the GPU and ramps up only once load is detected on that cluster."""

    def zones(self, p1=70.0):
        return ((60.0, 60.0), (70.0, 70.0), (60.0, 60.0), (p1, p1))

    def test_partial_p_cluster_waits_at_its_base_cap(self):
        settings = Settings()
        controller = Supervisor(settings)
        controller.previous_cpu_demand = True
        command = None
        for _ in range(40):   # P1 half loaded and cool: no full clock
            command = controller.step(Observation(70.0, 50, 0, cpu_demand_active=True,
                                                  cpu_zones=self.zones(),
                                                  cluster_util=(0.3, 1.0, 0.3, 0.5)), 0.25)
        base = settings.learned_cap_initial_p - settings.partial_cap_margin
        self.assertAlmostEqual(command.cluster_ratios[3], base)
        self.assertEqual(command.cluster_ratios[2], 1.0)      # E clusters unconstrained

    def test_load_detection_ramps_up_in_a_controlled_way(self):
        settings = Settings()
        controller = Supervisor(settings)
        controller.previous_cpu_demand = True
        partial = (0.3, 1.0, 0.3, 0.5)
        for _ in range(40):
            controller.step(Observation(70.0, 50, 0, cpu_demand_active=True, cpu_zones=self.zones(),
                                        cluster_util=partial), 0.25)
        before = controller.clusters[3].cap
        command = controller.step(Observation(70.0, 50, 0, cpu_demand_active=True,
                                              cpu_zones=self.zones(),
                                              cluster_util=(0.3, 1.0, 0.3, 1.0)), 0.25)
        rise = command.cluster_ratios[3] - before
        self.assertGreater(rise, 0.0)
        self.assertLessEqual(rise, settings.cpu_recovery_s * 0.25 + 1e-12)   # no jump to full clock

    def test_full_load_teaches_the_base_cap(self):
        settings = Settings()
        loop = Supervisor(settings).clusters[3]
        for _ in range(2000):   # 500 s full load regulated at 0.88
            loop.cap = 0.88
            loop.step((loop.setpoint, loop.setpoint), 0.88, 0.25, util=1.0)
        self.assertAlmostEqual(loop.learned, 0.88, places=3)
        self.assertEqual(Supervisor(settings).clusters[0].learned, 1.0)


class CpuCommandShapingTests(unittest.TestCase):
    def test_ramp_sends_few_commands_and_reaches_the_maximum(self):
        policy = ShadowPolicy(Config())
        outputs = []
        for tick in range(400):   # 100 s of cool, busy clusters ramping from the entry clamp
            result = policy.step(PolicyInput(snapshot({"acpi_ts0p": (60.0, 0.0, 60.0),
                                                       "acpi_ts1p": (60.0, 0.0, 60.0)},
                                                      monotonic_s=1.0 + 0.25 * tick), 0, 0.25,
                                             cpu_demand_active=True, cpu_util_pct=99.0,
                                             cluster_util_pct={"E0": 99, "P0": 99, "E1": 99, "P1": 99}))
            outputs.append(result.cpu_clusters())
        changes = sum(a != b for a, b in zip(outputs, outputs[1:]))
        self.assertLessEqual(changes, 60)                       # was one command per tick
        self.assertEqual(outputs[-1], (2808, 3900, 2808, 3900))  # the maximum is reached exactly
        for values in outputs:
            self.assertTrue(all(v % 25 == 0 or v in (338, 1378, 2808, 3900) for v in values))
