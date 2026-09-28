"""Live settings without restarts and an always-on monitor (operator, 27 September
2026: "we don't like to restart the service for each test", "we will never be
blind", "alle Variablen / PID exportieren und änderbar machen"). Fakes only."""
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time_ns
import unittest

from energy_control.broker import Config, TUNABLES, normalize_tuning
from energy_control.policy import PolicyInput, ShadowPolicy
from energy_control.service import LiveOverride
from test_v3_control import snapshot


def write(path, values):
    path.write_text(json.dumps(values))
    os.chmod(path, 0o644)


class TuningTests(unittest.TestCase):
    def test_tuning_is_validated_and_canonical(self):
        config = Config(tuning=normalize_tuning({"trend_margin_c": 3.5, "pid_band_c": 10}))
        self.assertEqual(config.tuning, (("pid_band_c", 10.0), ("trend_margin_c", 3.5)))
        for bad in ({"trend_margin_c": 0.5}, {"cpu_emergency_c": 95.0}, {"prediction_s": 1.0},
                    {"entry_fallback": 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Config(tuning=normalize_tuning(bad))
        with self.assertRaises(ValueError):          # not canonical
            Config(tuning=(("trend_margin_c", 3.5), ("pid_band_c", 10.0)))

    def test_every_tunable_reaches_the_model_and_is_exported(self):
        values = {name: (low + high) / 2 for name, (low, high) in TUNABLES.items()}
        values["cluster_full_util"] = 0.95          # stays above cluster_busy_util
        values["cpu_reservation_ratio"] = 0.1       # stays below the entry ratio
        values["fan_boost_headroom"], values["fan_release_headroom"] = 0.5, 0.8
        values["setpoint_recovery_c_s"] = 0.05      # slower than the back-off
        values["prefill_rearm"] = 1.0               # a switch: exported as 0 or 1
        policy = ShadowPolicy(Config(tuning=normalize_tuning(values)))
        exported = policy.control_state()["tuning"]
        self.assertEqual(set(exported), set(TUNABLES))
        for name, value in values.items():
            self.assertAlmostEqual(exported[name], value, msg=name)

    def test_live_change_reaches_the_cluster_and_gpu_zone_loops(self):
        policy = ShadowPolicy(Config())
        for tick in range(8):
            policy.step(PolicyInput(snapshot(monotonic_s=1.0 + 0.25 * tick), 0, 0.25))
        loops = (*policy.supervisor.clusters, policy.supervisor.gpu_zone_loop)
        self.assertTrue(all(loop.setpoint == loop.ceiling(policy.supervisor.s) for loop in loops))
        self.assertEqual(policy.supervisor.gpu_zone_loop.setpoint, 86.0)   # 89 - margin 3
        policy.update_config(Config(cpu_target_c=80.0, cpu_kp=0.1,
                                    tuning=normalize_tuning({"trend_margin_c": 3.0,
                                                             "gpu_zone_kp": 0.05})))
        for loop in loops:
            self.assertIs(loop.s, policy.supervisor.s)
            self.assertEqual(loop.setpoint, loop.ceiling(policy.supervisor.s))  # lowered: at once
        self.assertTrue(all(loop.pid.gains.kp == 0.1 for loop in policy.supervisor.clusters))
        self.assertEqual(policy.supervisor.gpu_zone_loop.pid.gains.kp, 0.05)   # its own gains
        policy.update_config(Config(tuning=normalize_tuning({"trend_margin_c": 3.0})))
        for loop in loops:   # raised: only the back-off floor lifts it, then gradual recovery
            ceiling = loop.ceiling(policy.supervisor.s)
            self.assertEqual(loop.setpoint, ceiling - policy.supervisor.s.setpoint_backoff_max_c)


class RenamedFieldTests(unittest.TestCase):
    def test_priority_llm_loads_as_priority_gpu(self):
        # Operator, 27 September 2026: "llm priority ist schlicht falsch".
        from energy_control.service import load_config
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            write(path, {"gpu_max_mhz": 2200, "gpu_entry_mhz": 1700, "priority_llm": 1.5})
            self.assertEqual(load_config(path).priority_gpu, 1.5)
        self.assertEqual(Config().priority_gpu, 1.0)   # GPU : CPU = 1 : 1


class LiveOverrideTests(unittest.TestCase):
    def test_boot_bound_root_only_and_live(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.json"
            override = LiveOverride(path, boot="b1")
            base = Config(gpu_max_mhz=2200, gpu_entry_mhz=1700)
            self.assertFalse(override.poll())
            self.assertEqual(override.apply(base)[0], base)
            write(path, {"boot_id": "b1", "gpu_max_mhz": 1500, "gpu_entry_mhz": 1500,
                         "tuning": {"pid_band_c": 8}})
            self.assertTrue(override.poll())
            effective, note = override.apply(replace(
                base, tuning=normalize_tuning({"trend_margin_c": 3.0})))
            self.assertEqual((effective.gpu_max_mhz, effective.gpu_entry_mhz), (1500, 1500))
            self.assertEqual(effective.tuning_dict(), {"pid_band_c": 8.0, "trend_margin_c": 3.0})
            self.assertIn("this boot only", note)
            self.assertFalse(override.poll())            # unchanged file: no re-apply
            path.unlink()
            self.assertTrue(override.poll())
            self.assertFalse(override.active)
            self.assertEqual(override.apply(base)[0], base)

    def test_refusals(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "qualification.json"
            base = Config(gpu_max_mhz=2200, gpu_entry_mhz=1700)
            for values, mode in (({"boot_id": "b2", "gpu_max_mhz": 1500}, 0o644),
                                 ({"boot_id": "b1", "cpu_fast_max_mhz": 3000}, 0o644),
                                 ({"boot_id": "b1", "x": 1}, 0o644),
                                 ({"boot_id": "b1", "gpu_max_mhz": 1500}, 0o666)):
                with self.subTest(values=values, mode=mode):
                    path.write_text(json.dumps(values))
                    os.chmod(path, mode)
                    override = LiveOverride(path, boot="b1")
                    override.poll()
                    self.assertFalse(override.active)
                    self.assertIn("ignored", override.note)
            write(path, {"boot_id": "b1", "gpu_max_mhz": 99999})
            override = LiveOverride(path, boot="b1")
            override.poll()
            effective, note = override.apply(base)
            self.assertEqual(effective, base)            # outside the hard envelope
            self.assertIn("ignored", note)

    def test_broker_refuses_proposals_while_an_override_is_active(self):
        from test_operator_broker import OPERATOR, OperatorBrokerTests
        from energy_control.operator_broker import (API_PLACEHOLDER_UID, JsonlAudit,
                                                    ServiceBroker, ServiceConfigActuator)
        from test_operator_broker import VERIFIER
        active = [True]
        with TemporaryDirectory() as directory:
            broker = ServiceBroker(VERIFIER, ServiceConfigActuator(),
                                   JsonlAudit(Path(directory) / "audit.jsonl"),
                                   api_uid=API_PLACEHOLDER_UID, operator_uid=OPERATOR,
                                   startup_config=Config(gpu_max_mhz=2200, gpu_entry_mhz=1700),
                                   override_active=lambda: active[0])
            with self.assertRaises(ValueError):
                broker.propose({"gpu_target_c": 70.0}, base_revision=0, peer_uid=OPERATOR)
            active[0] = False
            proposal = broker.propose({"gpu_entry_mhz": 1800, "tuning": {"pid_band_c": 9}},
                                      base_revision=0, peer_uid=OPERATOR)
            self.assertEqual(proposal.config.gpu_entry_mhz, 1800)   # entry is live now
            self.assertEqual(proposal.config.tuning, (("pid_band_c", 9.0),))


class NeverBlindTests(unittest.TestCase):
    """The single hardware reader keeps publishing while it starts or holds its
    safe state (operator, 27 September 2026: "we will never be blind")."""

    def test_wait_publishes_about_once_a_second_and_stops_on_a_dead_sampler(self):
        from threading import Event
        from types import SimpleNamespace
        from energy_control.service import wait_until_cool
        from test_resident_supervisor import FileThermal
        with TemporaryDirectory() as directory:
            temps = Path(directory) / "temps"
            temps.write_text("70 60\n")
            thermal = FileThermal(temps)
            thermal.last_readout = SimpleNamespace(utc_ns=1)
            published = []
            self.assertTrue(wait_until_cool(thermal, Event(), dwell_s=1.2, poll_s=0.05,
                                            publish=published.append, alive=lambda: True))
            self.assertGreaterEqual(len(published), 2)
            self.assertFalse(wait_until_cool(thermal, Event(), dwell_s=5, poll_s=0.05,
                                             alive=lambda: False))

    def test_idle_status_uses_the_status_schema(self):
        from types import SimpleNamespace
        from energy_control.service import idle_status_publisher
        readout = SimpleNamespace(
            utc_ns=1, end_mono_ns=2, acpi_temperatures=(("acpi_TGPU", 60.0),),
            gpu=SimpleNamespace(temperature_c=50.0, measured_mhz=500.0, utilization_pct=0.0,
                                reported_power_w=4.0, hardware_max_mhz=3003.0),
            fan=SimpleNamespace(rpm=(5000, 5100)), cpu_util_pct=3.0,
            cpu_policies=(SimpleNamespace(measured_mhz=1378.0, hardware_max_mhz=3900.0),))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            idle_status_publisher(path, Config(), "SAFE_STATE", "policy abort: x")(readout)
            payload = json.loads(path.read_text())
        self.assertEqual((payload["mode"], payload["reason"]), ("SAFE_STATE", "policy abort: x"))
        self.assertIsNone(payload["gpu"]["cap_mhz"])
        self.assertEqual(payload["gpu"]["hardware_max_mhz"], 3003.0)
        self.assertEqual(payload["zones_c"], {"TGPU": 60.0})


if __name__ == "__main__":
    unittest.main()
