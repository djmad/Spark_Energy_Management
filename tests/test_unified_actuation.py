from dataclasses import replace
from itertools import count
import unittest

from energy_control.broker import Config
from energy_control.fan import FanFloorStatus
from energy_control.gpu_command import SetterAttempt
from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.policy import ProposedLimits, PolicyInput, ShadowPolicy
from energy_control.unified_actuation import UnifiedActuation, UnifiedController
import test_cpu_actuation as cpu_fixtures
from test_safety import good_snapshot

CONTEXT = ("boot", "driver", "owner", "ab" * 16)


class FakeGpu:
    def __init__(self, calls):
        self.calls, self.maximum, self.fail = calls, 1200, False

    def read(self):
        return GpuSetterEvidence(200, self.maximum, 1, 0, 1., 1., *CONTEXT)

    def apply(self, *, minimum_mhz, maximum_mhz):
        self.calls.append("gpu_write")
        if self.fail:
            raise RuntimeError("fake GPU fault")
        self.maximum = maximum_mhz
        return SetterAttempt(1, "success", 0, 1_000_000_000, True)


class FakeFan:
    def __init__(self, calls):
        self.calls, self.floor, self.fail = calls, 6, False

    def read_floor(self):
        return FanFloorStatus("fake", self.floor, 12)

    def set_minimum(self, value):
        self.calls.append("fan_write")
        if self.fail:
            raise RuntimeError("fake fan fault")
        self.floor = value
        return self.read_floor()


class UnifiedActuationTests(unittest.TestCase):
    def test_external_limit_drift_aborts_before_another_write(self):
        for device in ("cpu", "gpu", "fan"):
            with self.subTest(device=device):
                cycle, calls, gpu, fan, proposal = self.setup_cycle()
                cycle.apply(proposal)
                if device == "cpu":
                    policies = cycle.cpu.adapter.current
                    cycle.cpu.adapter.current = (replace(policies[0],
                        requested_max_khz=policies[0].requested_max_khz - 1000),) + policies[1:]
                elif device == "gpu":
                    gpu.maximum -= 100
                else:
                    fan.floor -= 1
                calls.clear()
                with self.assertRaises((ValueError, RuntimeError)):
                    cycle.apply(proposal)
                self.assertTrue(cycle.faulted)
                self.assertFalse(any(call in ("candidate", "write", "gpu_write", "fan_write")
                                     for call in calls))

    def test_policy_startup_hold_readiness_and_lost_signal_reach_all_adapters(self):
        cycle, calls, gpu, fan, _ = self.setup_cycle()
        controller = UnifiedController(ShadowPolicy(cycle.config), cycle)
        for tick, loading in enumerate((True, True, False, False)):
            proposal = controller.tick(PolicyInput(
                good_snapshot(monotonic_s=1 + tick * 0.5,
                              gpu_requested_max_mhz=gpu.maximum, gpu_accepted_max_mhz=gpu.maximum),
                100, 0.5, cpu_demand_active=True, model_loading=loading))
            self.assertFalse(proposal.abort_owned_loads)
            self.assertEqual(gpu.maximum, proposal.gpu_max_mhz)
            self.assertEqual(fan.floor, proposal.fan_min_state)
            if loading:
                self.assertEqual(proposal.mode, "STARTUP")
                self.assertLessEqual(proposal.cpu_fast_max_mhz, 2639)
                self.assertEqual(gpu.maximum, 1200)
        self.assertGreater(gpu.maximum, 1200)
        before = calls.count("write") + calls.count("gpu_write")
        aborted = controller.tick(PolicyInput(good_snapshot(monotonic_s=3), 100, 0.5,
                                               cpu_demand_active=True))
        self.assertTrue(aborted.abort_owned_loads)
        self.assertTrue(cycle.faulted)
        self.assertEqual(before, calls.count("write") + calls.count("gpu_write"))
        count = len(calls)
        with self.assertRaises(RuntimeError):
            controller.tick(PolicyInput(good_snapshot(monotonic_s=3.5), 0, 0.5))
        self.assertEqual(len(calls), count)

    def setup_cycle(self):
        cpu, calls, recorder = cpu_fixtures.CpuActuationTests().make_step()
        gpu, fan = FakeGpu(calls), FakeFan(calls)
        times = count(1, 0.001)
        cycle = UnifiedActuation(Config(gpu_max_mhz=1800), cpu, gpu, fan,
            read_safety=lambda: good_snapshot(monotonic_s=next(times), gpu_requested_max_mhz=gpu.maximum,
                                              gpu_accepted_max_mhz=gpu.maximum),
            verify_ownership=lambda: True, record_candidate=lambda p: calls.append("candidate"),
            gpu_context=CONTEXT, clock=lambda: 1.)
        proposal = ProposedLimits(1300, 2400, 2000, 12, False, "RAMP", ("busy",))
        return cycle, calls, gpu, fan, proposal

    def test_one_coordinated_order_for_mixed_cpu_gpu_fan_changes(self):
        cycle, calls, gpu, fan, proposal = self.setup_cycle()
        cycle.apply(proposal)
        writes = [call for call in calls if call in ("candidate", "fan_write", "write", "gpu_write")]
        self.assertEqual(writes, ["candidate", "fan_write", "write", "gpu_write", "write"])
        self.assertEqual(gpu.maximum, 1300)
        self.assertEqual(fan.floor, 12)
        self.assertFalse(cycle.faulted)

    def test_actuator_failures_stop_later_increases_and_latch(self):
        for device in ("gpu", "fan"):
            with self.subTest(device=device):
                cycle, calls, gpu, fan, proposal = self.setup_cycle()
                (gpu if device == "gpu" else fan).fail = True
                with self.assertRaises(RuntimeError):
                    cycle.apply(proposal)
                self.assertTrue(cycle.faulted)
                self.assertIn("cancel_owned_requests", calls)
                self.assertIn("event:abort", calls)
                self.assertEqual(calls.count("write"), 1 if device == "gpu" else 0)
                count = len(calls)
                with self.assertRaises(RuntimeError):
                    cycle.apply(proposal)
                self.assertEqual(len(calls), count)

    def test_plan_and_hard_limits_prevent_all_writes(self):
        for change in ({"gpu_max_mhz": 1801}, {"fan_min_state": 0},
                       {"cpu_fast_max_mhz": 4000}, {"abort_owned_loads": True}):
            cycle, calls, _, _, proposal = self.setup_cycle()
            with self.assertRaises(ValueError):
                cycle.apply(replace(proposal, **change))
            self.assertFalse(any(c in calls for c in ("write", "gpu_write", "fan_write")))

    def test_lost_ownership_after_candidate_sync_prevents_writes(self):
        cycle, calls, _, _, proposal = self.setup_cycle()
        cycle._owner = lambda: "candidate" not in calls
        with self.assertRaises(RuntimeError):
            cycle.apply(proposal)
        self.assertFalse(any(c in calls for c in ("write", "gpu_write", "fan_write")))
