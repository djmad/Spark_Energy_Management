from dataclasses import replace
import socket
from time import monotonic
import unittest

from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.gpu_evidence_channel import GpuEvidenceReader, publish_evidence
from energy_control.host_sampler import (AbortRouteHealthy, OwnedActuatorSafetySampler,
                                         SetterBackedSafetySampler)
from energy_control.limit_evidence import (LimitEvidence, LimitEvidenceReader, limit_context,
                                           publish_limit_evidence)
from threading import Event
from energy_control.safety import CommissioningGuard
from energy_control.temperature_slope import SlopeUnavailable
from test_safety import good_snapshot


class SetterBackedSamplerTests(unittest.TestCase):
    def setUp(self):
        self.receive, self.send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.receive.close)
        self.addCleanup(self.send.close)
        self.context = ("boot", "driver", "owner", "ab" * 16)
        now = monotonic()
        self.proof = GpuSetterEvidence(200, 1200, 3, 0, now, now, *self.context)
        self.snapshot = good_snapshot(monotonic_s=now)
        self.sampler = SetterBackedSafetySampler(lambda: self.snapshot,
            GpuEvidenceReader(self.receive, self.context))

    def test_join_keeps_clock_distinctions_and_ages(self):
        publish_evidence(self.send, self.proof)
        result = self.sampler()
        self.assertIsNone(result.gpu_accepted_max_mhz)
        self.assertEqual(result.gpu_measured_mhz, 1000)
        self.assertEqual(result.gpu_requested_max_mhz, 1200)
        self.assertGreaterEqual(result.temperatures[0].age_s, self.snapshot.temperatures[0].age_s)
        guard = CommissioningGuard(gpu_evidence_mode="setter_monitor", gpu_setter_context=self.context)
        self.assertFalse(guard.evaluate(result).abort)
        settled = replace(result, gpu_measured_mhz=1300,
                          monotonic_s=self.proof.completed_monotonic_s + 6.0,
                          gpu_clock_age_s=0.1)
        self.assertTrue(guard.clone_unlatched().evaluate(settled).abort)

    def test_measured_clock_may_settle_after_a_reduction(self):
        guard = CommissioningGuard(gpu_evidence_mode="setter_monitor", gpu_setter_context=self.context)
        publish_evidence(self.send, self.proof)  # Completed just now, requested 1200.
        result = replace(self.sampler(), gpu_measured_mhz=1300)
        self.assertFalse(guard.evaluate(result).abort)  # Within 1 s of completion.
        late = replace(result, monotonic_s=self.proof.completed_monotonic_s + 6.0,
                       gpu_clock_age_s=0.1)
        self.assertTrue(any("measured clock above setter request" in r for r in
                            guard.clone_unlatched().evaluate(late).reasons))
        # A clock sampled before the command completed is never judged against it.
        old_sample = replace(result, monotonic_s=self.proof.completed_monotonic_s + 0.3,
                             gpu_clock_age_s=0.4)
        self.assertFalse(guard.clone_unlatched().evaluate(old_sample).abort)

    def test_gpu_proof_does_not_invent_other_actuator_health(self):
        self.snapshot = replace(self.snapshot, cpu_actuator_healthy=False,
            fan_healthy=False, workload_control_healthy=False)
        publish_evidence(self.send, self.proof)
        result = self.sampler()
        self.assertFalse(result.cpu_actuator_healthy)
        self.assertFalse(result.fan_healthy)
        self.assertFalse(result.workload_control_healthy)

    def test_missing_proof_or_stale_thermal_frame_refuses_join(self):
        with self.assertRaises(SlopeUnavailable):
            self.sampler()
        publish_evidence(self.send, self.proof)
        self.snapshot = replace(self.snapshot, monotonic_s=monotonic() - 1)
        with self.assertRaises(SlopeUnavailable):
            self.sampler()


class FakeThermal:
    def __init__(self, snapshot):
        self.snapshot, self.fan_sensor_healthy = snapshot, True

    def __call__(self):
        return self.snapshot


class OwnedActuatorSamplerTests(unittest.TestCase):
    def setUp(self):
        self.sockets = {}
        for name in ("gpu", "cpu", "fan"):
            receive, send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(receive.close)
            self.addCleanup(send.close)
            self.sockets[name] = (receive, send)
        run = "ab" * 16
        self.gpu_context = ("boot", "driver", "owner", run)
        self.cpu_context = limit_context("boot", "cpu", "cpu-owner", run)
        self.fan_context = limit_context("boot", "fan", "fan-owner", run)
        now = monotonic()
        self.thermal = FakeThermal(replace(good_snapshot(monotonic_s=now), fan_healthy=False,
                                           cpu_actuator_healthy=False, workload_control_healthy=False))
        self.abort = Event()
        self.sampler = OwnedActuatorSafetySampler(
            self.thermal, GpuEvidenceReader(self.sockets["gpu"][0], self.gpu_context),
            LimitEvidenceReader(self.sockets["cpu"][0], self.cpu_context),
            LimitEvidenceReader(self.sockets["fan"][0], self.fan_context),
            AbortRouteHealthy(self.abort))
        publish_evidence(self.sockets["gpu"][1],
                         GpuSetterEvidence(200, 1200, 3, 0, now, now, *self.gpu_context))

    def limits(self, cpu=True, fan=True):
        now = monotonic()
        if cpu:
            publish_limit_evidence(self.sockets["cpu"][1], LimitEvidence(
                "cpu", (2000, 2500, 2000, 2500), (2000, 2500, 2000, 2500), 1, now, now, "boot", "cpu-owner", "ab" * 16))
        if fan:
            publish_limit_evidence(self.sockets["fan"][1], LimitEvidence(
                "fan", (6,), (6,), 1, now, now, "boot", "fan-owner", "ab" * 16))

    def guard(self):
        return CommissioningGuard(gpu_evidence_mode="setter_monitor",
                                  gpu_setter_context=self.gpu_context)

    def test_all_owner_evidence_makes_a_healthy_frame(self):
        self.limits()
        result = self.sampler()
        self.assertTrue(result.cpu_actuator_healthy and result.fan_healthy
                        and result.workload_control_healthy)
        self.assertFalse(self.guard().evaluate(result).abort)

    def test_missing_cpu_or_fan_evidence_aborts_with_reason(self):
        self.limits(fan=False)
        decision = self.guard().evaluate(self.sampler())
        self.assertTrue(decision.abort)
        self.assertIn("fan unhealthy", decision.reasons)

    def test_fan_sensor_fault_is_not_hidden_by_owner_evidence(self):
        self.limits()
        self.thermal.fan_sensor_healthy = False
        self.assertFalse(self.sampler().fan_healthy)

    def test_abort_route_set_reports_workload_unhealthy(self):
        self.limits()
        self.abort.set()
        self.assertFalse(self.sampler().workload_control_healthy)

    def test_readers_must_match_their_owner_kind(self):
        with self.assertRaises(ValueError):
            OwnedActuatorSafetySampler(
                self.thermal, GpuEvidenceReader(self.sockets["gpu"][0], self.gpu_context),
                LimitEvidenceReader(self.sockets["fan"][0], self.fan_context),
                LimitEvidenceReader(self.sockets["cpu"][0], self.cpu_context))
