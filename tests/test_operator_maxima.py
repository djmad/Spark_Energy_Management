"""Operator-settable maximum frequencies (operator, 27 September 2026, doc/52):
the GPU maximum up to the hard limit and per-cluster CPU maxima, both live.
Synthetic only, no hardware claims."""
from dataclasses import replace
import unittest

from energy_control.broker import ActuatorReadback, Config, CPU_CLUSTER_MAX_FIELDS
from energy_control.limits import GPU_HARD_MAX_MHZ, GPU_QUALIFIED_MAX_MHZ
from energy_control.policy import GPU_LOWERING_GRACE_S, PolicyInput, ShadowPolicy
from energy_control.service import service_plan
from energy_control.trial_plan import validate_trial_proposal
from simulation.model import Observation, Settings, Supervisor
from test_v3_control import snapshot

BUSY = {"E0": 99, "P0": 99, "E1": 99, "P1": 99}
COOL = {"acpi_ts0p": (60.0, 0.0, 60.0), "acpi_ts1p": (60.0, 0.0, 60.0)}


def run(policy, ticks, start=0, **kwargs):
    """Cool, fully busy clusters; returns every tick's result."""
    results = []
    for tick in range(start, start + ticks):
        result = policy.step(PolicyInput(snapshot(COOL, monotonic_s=1.0 + 0.25 * tick), 0, 0.25,
                                         cpu_demand_active=True, cpu_util_pct=99.0,
                                         cluster_util_pct=BUSY, **kwargs))
        assert not result.abort_owned_loads, result.reasons
        results.append(result)
    return results


class EnvelopeTests(unittest.TestCase):
    def test_hard_limit_is_2500_and_qualified_is_below(self):
        self.assertEqual(GPU_HARD_MAX_MHZ, 2500)
        self.assertLessEqual(GPU_QUALIFIED_MAX_MHZ, GPU_HARD_MAX_MHZ)
        Config(gpu_max_mhz=2500, gpu_entry_mhz=1700)
        with self.assertRaises(ValueError):
            Config(gpu_max_mhz=2501, gpu_entry_mhz=1700)

    def test_cluster_fields_default_to_hardware_and_are_bounded(self):
        config = Config()
        self.assertEqual(config.cpu_cluster_maxima(), (2808, 3900, 2808, 3900))
        for field, low, high in (("cpu_e0_max_mhz", 338, 2808), ("cpu_p1_max_mhz", 1378, 3900)):
            replace(config, **{field: low})
            replace(config, **{field: high})
            for value in (low - 1, high + 1, float(high)):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replace(config, **{field: value})
        # The class maximum still bounds the operator's cluster value.
        self.assertEqual(Config(cpu_fast_max_mhz=3000).cpu_cluster_maxima(),
                         (2808, 3000, 2808, 3000))
        self.assertEqual(len(CPU_CLUSTER_MAX_FIELDS), 4)

    def test_service_plan_carries_the_hard_limit(self):
        for maximum in (2000, 2200, 2500):
            plan = service_plan(Config(gpu_max_mhz=maximum, gpu_entry_mhz=1700))
            validate_trial_proposal(plan)
            self.assertEqual(plan.gpu_max_mhz, GPU_HARD_MAX_MHZ)
            self.assertEqual(plan.gpu_entry_mhz, 1700)   # entry stays bound to the start config

    def test_broker_accepts_live_raises_of_gpu_and_cluster_maxima(self):
        from tempfile import TemporaryDirectory
        from test_operator_broker import OPERATOR, OperatorBrokerTests
        with TemporaryDirectory() as directory:
            broker, _ = OperatorBrokerTests.broker(None, directory,
                                                   Config(gpu_max_mhz=2000, gpu_entry_mhz=1700,
                                                          cpu_p0_max_mhz=3000,
                                                          cpu_fast_max_mhz=3000))
            proposal = broker.propose({"gpu_max_mhz": 2400, "cpu_p0_max_mhz": 3900},
                                      base_revision=0, peer_uid=OPERATOR)
            self.assertEqual((proposal.config.gpu_max_mhz, proposal.config.cpu_p0_max_mhz),
                             (2400, 3900))
            with self.assertRaises(ValueError):   # the CPU class maxima still need a restart
                broker.propose({"cpu_fast_max_mhz": 3900}, base_revision=0, peer_uid=OPERATOR)

    def test_readback_checks_each_cluster_against_its_operator_maximum(self):
        config = Config(gpu_max_mhz=2000, gpu_entry_mhz=1700, cpu_p1_max_mhz=3000)

        def readback(clusters):
            return ActuatorReadback(config, 2000, 1990, 3900, 2808, 12, 1.0,
                                    cpu_cluster_accepted_max_mhz=clusters)
        self.assertTrue(readback((2808, 3900, 2808, 3000)).matches(config))
        self.assertFalse(readback((2808, 3900, 2808, 3100)).matches(config))
        self.assertTrue(readback(None).matches(config))


class CpuClusterMaximaTests(unittest.TestCase):
    def test_operator_maxima_bound_their_clusters_only(self):
        policy = ShadowPolicy(Config(cpu_p1_max_mhz=3000, cpu_e0_max_mhz=2000))
        final = run(policy, 400)[-1].cpu_clusters()
        self.assertEqual(final, (2000, 3900, 2808, 3000))   # E1 is not dragged down with P1

    def test_lowering_applies_at_once_and_raising_ramps(self):
        policy = ShadowPolicy(Config())
        self.assertEqual(run(policy, 400)[-1].cpu_clusters(), (2808, 3900, 2808, 3900))
        policy.update_config(Config(cpu_p0_max_mhz=2500))
        lowered = run(policy, 1, start=400)[0].cpu_clusters()
        self.assertEqual(lowered[1], 2500)                   # the next tick
        policy.update_config(Config())
        raised = [r.cpu_clusters()[1] for r in run(policy, 200, start=401)]
        steps = [b - a for a, b in zip([2500] + raised, raised)]
        self.assertLessEqual(max(steps), 150)                # controlled, no jump to 3900
        self.assertEqual(raised[-1], 3900)

    def test_class_control_applies_cluster_maxima(self):
        policy = ShadowPolicy(Config(cpu_control="class", cpu_p1_max_mhz=3000))
        final = run(policy, 400)[-1]
        e0, p0, e1, p1 = final.cpu_clusters()
        self.assertEqual(p1, 3000)
        self.assertEqual(p0, final.cpu_fast_max_mhz)

    def test_model_bound_and_fast_first_use_the_operator_maximum(self):
        settings = Settings(cluster_max_ratio=(1.0, 0.5, 1.0, 1.0))
        controller = Supervisor(settings)
        controller.previous_cpu_demand = True
        zones = ((60.0, 60.0),) * 4
        command = None
        for _ in range(400):
            command = controller.step(Observation(60.0, 50, 0, cpu_demand_active=True,
                                                  cpu_zones=zones,
                                                  cluster_util=(1.0, 1.0, 1.0, 1.0)), 0.25)
        e0, p0, e1, p1 = command.cluster_ratios
        self.assertAlmostEqual(p0, 0.5)
        self.assertEqual(e0, 1.0)                           # an operator cap is not a derate
        for bad in ((1.0, 1.0, 1.0), (1.0, 1.1, 1.0, 1.0), [1.0] * 4):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Settings(cluster_max_ratio=bad)


class GpuMaximumLiveTests(unittest.TestCase):
    def gpu_step(self, policy, tick, observed):
        return policy.step(PolicyInput(
            replace(snapshot(monotonic_s=1.0 + 0.25 * tick), gpu_requested_max_mhz=observed,
                    gpu_accepted_max_mhz=observed), 100, 0.25, active_jobs=12))

    def test_lowering_tolerates_the_previous_limit_until_applied(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=2200, gpu_entry_mhz=1700))
        self.assertFalse(self.gpu_step(policy, 0, 2200).abort_owned_loads)
        policy.update_config(Config(gpu_max_mhz=2000, gpu_entry_mhz=1700))
        result = self.gpu_step(policy, 1, 2200)              # owner not yet applied
        self.assertFalse(result.abort_owned_loads, result.reasons)
        self.assertLessEqual(result.gpu_max_mhz, 2000)
        self.assertFalse(self.gpu_step(policy, 2, 2000).abort_owned_loads)
        self.assertIsNone(policy._gpu_lowering)              # applied: tolerance ends
        self.assertTrue(self.gpu_step(policy, 3, 2200).abort_owned_loads)

    def test_lowering_tolerance_expires(self):
        policy = ShadowPolicy(Config(gpu_max_mhz=2200, gpu_entry_mhz=1700))
        self.gpu_step(policy, 0, 2200)
        policy.update_config(Config(gpu_max_mhz=2000, gpu_entry_mhz=1700))
        ticks = int(GPU_LOWERING_GRACE_S / 0.25)
        for tick in range(1, ticks + 1):
            self.assertFalse(self.gpu_step(policy, tick, 2200).abort_owned_loads)
        result = self.gpu_step(policy, ticks + 2, 2200)
        self.assertTrue(result.abort_owned_loads)
        self.assertIn("observed GPU limit exceeds committed policy", result.reasons)

    def test_raising_ramps_to_the_new_maximum(self):
        settings = ShadowPolicy._settings(Config(gpu_max_mhz=2500, gpu_entry_mhz=1700))
        controller = Supervisor(settings)
        controller.cap = 2000
        caps = []
        for _ in range(40):   # 10 s of busy decode
            caps.append(controller.step(Observation(60, 55, 0.9, active_jobs=12), 0.25).gpu_cap_mhz)
        steps = [b - a for a, b in zip([2000] + caps, caps)]
        self.assertLessEqual(max(steps), settings.ramp_mhz_s * 0.25 + 1e-9)
        self.assertEqual(caps[-1], 2500)


class StatusTests(unittest.TestCase):
    def test_status_publishes_the_settable_envelope(self):
        import json
        from types import SimpleNamespace
        from energy_control.service import default_service_config, status_payload
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_TS0P", 71.0),),
            gpu=SimpleNamespace(temperature_c=48.0, measured_mhz=1768.0, utilization_pct=92.0,
                                reported_power_w=16.8),
            fan=SimpleNamespace(rpm=(7700,)), cpu_util_pct=30.0,
            cpu_policies=(SimpleNamespace(measured_mhz=3800.0, hardware_max_mhz=3900.0),))
        limits = SimpleNamespace(mode="RUN", reasons=())
        config = replace(default_service_config(), cpu_p1_max_mhz=3000)
        payload = status_payload(readout, {"gpu": 1200, "cpu": (2808, 3900, 2808, 3000),
                                           "fan": (6,)}, limits, config, "ab" * 16, {})
        values = payload["limits"]
        self.assertEqual(values["gpu_hard_max_mhz"], GPU_HARD_MAX_MHZ)
        self.assertEqual(values["gpu_qualified_max_mhz"], GPU_QUALIFIED_MAX_MHZ)
        self.assertEqual(values["cpu_cluster_max_mhz"],
                         {"E0": 2808, "P0": 3900, "E1": 2808, "P1": 3000})
        self.assertEqual(values["cpu_cluster_bounds_mhz"], {"E": [338, 2808], "P": [1378, 3900]})
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
