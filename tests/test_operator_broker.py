"""Operator broker wiring with fakes: password file, restart fields, loop bridge."""
from dataclasses import replace
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, sleep
import unittest

from energy_control.broker import ActuatorReadback, Config, PasswordVerifier
from energy_control.operator_broker import (API_PLACEHOLDER_UID, JsonlAudit, ServiceBroker,
                                            ServiceConfigActuator, load_verifier,
                                            write_password_file)
from energy_control.service import load_config, persist_config

VERIFIER = PasswordVerifier.provision("correct horse battery")
OPERATOR = 1000


def readback_for(config):
    return lambda requested: ActuatorReadback(
        applied_config=config, gpu_accepted_max_mhz=config.gpu_max_mhz, gpu_measured_mhz=1690,
        cpu_fast_accepted_max_mhz=config.cpu_fast_max_mhz,
        cpu_slow_accepted_max_mhz=config.cpu_slow_max_mhz,
        fan_min_state=max(config.fan_min_state, 6), observed_monotonic_s=monotonic())


class FakeLoop:
    """Stands in for the service loop: take -> apply -> acknowledge."""
    def __init__(self, actuator):
        self.actuator, self.stop, self.applied = actuator, Event(), []
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            config = self.actuator.take()
            if config is not None:
                self.applied.append(config)
                self.actuator.acknowledge(config, readback_for(config))
            sleep(0.01)


class OperatorBrokerTests(unittest.TestCase):
    def test_password_file_is_root_only(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pw.json"
            self.assertIsNone(load_verifier(path))
            write_password_file(VERIFIER, OPERATOR, path)
            verifier, uid = load_verifier(path)
            self.assertEqual(uid, OPERATOR)
            self.assertTrue(verifier.verify("correct horse battery"))
            os.chmod(path, 0o644)
            with self.assertRaises(PermissionError):
                load_verifier(path)

    def broker(self, directory, startup=None):
        actuator = ServiceConfigActuator(apply_timeout_s=1.0, verify_timeout_s=1.0)
        broker = ServiceBroker(VERIFIER, actuator, JsonlAudit(Path(directory) / "audit.jsonl"),
                               api_uid=API_PLACEHOLDER_UID, operator_uid=OPERATOR,
                               startup_config=startup or Config(gpu_max_mhz=1800,
                                                                gpu_entry_mhz=1700))
        return broker, actuator

    def test_raising_restart_fields_is_refused_lowering_is_allowed(self):
        with TemporaryDirectory() as directory:
            broker, _ = self.broker(directory, Config(gpu_max_mhz=1800, gpu_entry_mhz=1500,
                                                      cpu_fast_max_mhz=3000))
            with self.assertRaises(ValueError):   # the CPU class maxima stay restart fields
                broker.propose({"cpu_fast_max_mhz": 3900}, base_revision=0, peer_uid=OPERATOR)
            # The entry ceiling is live (operator, 27 September 2026: no restarts).
            self.assertEqual(broker.propose({"gpu_entry_mhz": 1700}, base_revision=0,
                                            peer_uid=OPERATOR).config.gpu_entry_mhz, 1700)
            proposal = broker.propose({"gpu_entry_mhz": 1400}, base_revision=0, peer_uid=OPERATOR)
            self.assertEqual(proposal.config.gpu_entry_mhz, 1400)

    def test_full_commit_through_the_service_loop(self):
        with TemporaryDirectory() as directory:
            broker, actuator = self.broker(directory)
            loop = FakeLoop(actuator)
            self.addCleanup(loop.stop.set)
            proposal = broker.propose({"gpu_target_c": 70.0}, base_revision=0, peer_uid=OPERATOR)
            token = broker.authorize(proposal.id, "correct horse battery",
                                     operator="cli-password-operator", session="s1",
                                     peer_uid=OPERATOR)
            result = broker.commit(proposal.id, token, operator="cli-password-operator",
                                   session="s1", peer_uid=OPERATOR)
            self.assertEqual((result.status, result.revision, result.faulted), ("applied", 1, False))
            self.assertEqual(loop.applied[-1].gpu_target_c, 70.0)
            audit = [json.loads(line) for line in
                     (Path(directory) / "audit.jsonl").read_text().splitlines()]
            self.assertEqual([a["event"] for a in audit], ["intent", "outcome"])
            self.assertEqual(audit[-1]["status"], "verified")

    def test_wrong_password_and_absent_loop(self):
        with TemporaryDirectory() as directory:
            broker, actuator = self.broker(directory)
            proposal = broker.propose({"gpu_target_c": 70.0}, base_revision=0, peer_uid=OPERATOR)
            with self.assertRaises(PermissionError):
                broker.authorize(proposal.id, "wrong password!!", operator="cli-password-operator",
                                 session="s1", peer_uid=OPERATOR)
            token = broker.authorize(proposal.id, "correct horse battery",
                                     operator="cli-password-operator", session="s1",
                                     peer_uid=OPERATOR)
            result = broker.commit(proposal.id, token, operator="cli-password-operator",
                                   session="s1", peer_uid=OPERATOR)  # no loop running
            self.assertEqual(result.status, "failed_or_partial")
            self.assertTrue(result.faulted)

    def test_persist_config_round_trip(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(gpu_max_mhz=1800, gpu_entry_mhz=1700,
                            fan_curve=((50, 3), (60, 8), (70, 12)))
            persist_config(config, path)
            self.assertEqual(load_config(path), config)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o644")


if __name__ == "__main__":
    unittest.main()
