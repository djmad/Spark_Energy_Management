import json
from dataclasses import asdict, replace
from hashlib import sha256
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

from energy_control.broker import (ActuatorReadback, BrokerCore, Config,
                                   PasswordVerifier, Proposal)
from energy_control.config_audit import AUDIT_NAME, AuditUnavailable, ConfigAudit


BOOT_ID = "00000000-0000-0000-0000-000000000001"
CONFIG = Config()
DIGEST = sha256(json.dumps(asdict(CONFIG), sort_keys=True,
                           separators=(",", ":")).encode()).hexdigest()
PROPOSAL = Proposal("test-proposal", 0, 1000, DIGEST, CONFIG,
                    (("gpu_max_mhz", 1200),))


def matching_readback(config):
    return ActuatorReadback(config, config.gpu_max_mhz,
                            min(1000, config.gpu_max_mhz),
                            config.cpu_fast_max_mhz, config.cpu_slow_max_mhz,
                            config.fan_min_state, monotonic())


@unittest.skipUnless(os.geteuid() == 0, "root-owned audit tests require root")
class ConfigAuditTests(unittest.TestCase):
    def test_second_broker_cannot_open_live_audit(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID):
                with self.assertRaisesRegex(AuditUnavailable, "already owned"):
                    ConfigAudit(Path(directory), boot_id=BOOT_ID)
                self.assertEqual(path.read_bytes(), b"")
            with ConfigAudit(Path(directory), boot_id=BOOT_ID):
                self.assertEqual(path.read_bytes(), b"")

    def test_verified_audit_requires_reconciliation_on_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["seq"] for row in rows], [1, 2])
            self.assertEqual([row["kind"] for row in rows], ["intent", "outcome"])
            self.assertNotIn("password", path.read_text())
            with self.assertRaises(AuditUnavailable):
                ConfigAudit(Path(directory), boot_id=BOOT_ID)
            self.assertEqual(len(path.read_text().splitlines()), 2)

    def test_unfinished_and_failed_transactions_block_reopen(self):
        for outcome in (None, "failed_or_partial"):
            with self.subTest(outcome=outcome), TemporaryDirectory() as directory:
                with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                    audit.sync_intent(PROPOSAL, "operator")
                    if outcome:
                        audit.sync_outcome(PROPOSAL, outcome)
                with self.assertRaises(AuditUnavailable):
                    ConfigAudit(Path(directory), boot_id=BOOT_ID)

    def test_torn_tail_and_symlink_refused(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")
            with path.open("ab") as stream:
                stream.write(b'{"seq":3')
            with self.assertRaises(AuditUnavailable):
                ConfigAudit(Path(directory), boot_id=BOOT_ID)
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            path.symlink_to("/etc/passwd")
            with self.assertRaises(OSError):
                ConfigAudit(Path(directory), boot_id=BOOT_ID)

    def test_sync_failure_poisoned(self):
        with TemporaryDirectory() as directory:
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                with patch("energy_control.config_audit.os.fdatasync", side_effect=OSError("disk")):
                    with self.assertRaises(OSError):
                        audit.sync_intent(PROPOSAL, "operator")
                with self.assertRaises(AuditUnavailable):
                    audit.sync_intent(PROPOSAL, "operator")

    def test_explicit_clean_recovery_requires_matching_readback(self):
        with TemporaryDirectory() as directory:
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")

            class FakeReadback:
                def __init__(self):
                    self.match = False
                    self.calls = []

                def verify(self, config):
                    self.calls.append(config)
                    if self.match == "stale":
                        return replace(matching_readback(config),
                                       observed_monotonic_s=monotonic() - 2)
                    return matching_readback(config) if self.match else False

            readback = FakeReadback()
            with ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True) as audit:
                with self.assertRaises(AuditUnavailable):
                    audit.sync_intent(Proposal("next", 1, 2000, DIGEST, CONFIG, ()), "operator")
                with self.assertRaises(AuditUnavailable):
                    audit.reconcile_verified(readback)
                readback.match = "stale"
                with self.assertRaises(AuditUnavailable):
                    audit.reconcile_verified(readback)
                readback.match = True
                config, revision = audit.reconcile_verified(readback)
                self.assertEqual((config, revision), (CONFIG, 1))
                self.assertEqual(len(readback.calls), 3)
                next_proposal = Proposal("next", 1, 2000, DIGEST, CONFIG, ())
                audit.sync_intent(next_proposal, "operator")
                audit.sync_outcome(next_proposal, "verified")
            with ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True) as audit:
                self.assertEqual(audit.reconcile_verified(readback)[1], 2)
            rows = [json.loads(line) for line in (Path(directory) / AUDIT_NAME).read_text().splitlines()]
            self.assertEqual([row["kind"] for row in rows],
                             ["intent", "outcome", "reconciled", "intent", "outcome", "reconciled"])

    def test_recovery_refuses_tampered_digest_or_unfinished_write(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            rows[0]["digest"] = "0" * 64
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaises(AuditUnavailable):
                ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True)
        with TemporaryDirectory() as directory:
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
            with self.assertRaises(AuditUnavailable):
                ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True)

    def test_recovery_refuses_malformed_record_metadata(self):
        def missing_timestamp(rows):
            del rows[0]["utc_ns"]

        def invalid_timestamp(rows):
            rows[0]["mono_ns"] = True

        def invalid_boot(rows):
            rows[0]["boot_id"] = "not-a-uuid"

        def unexpected_field(rows):
            rows[1]["ignored"] = "unsafe"

        def invalid_operator(rows):
            rows[0]["operator"] = "prompt body\n"

        def invalid_kind(rows):
            rows[0]["kind"] = []

        for name, mutate in (("missing timestamp", missing_timestamp),
                             ("invalid timestamp", invalid_timestamp),
                             ("invalid boot", invalid_boot),
                             ("unexpected field", unexpected_field),
                             ("invalid operator", invalid_operator),
                             ("invalid kind", invalid_kind)):
            with self.subTest(name=name), TemporaryDirectory() as directory:
                path = Path(directory) / AUDIT_NAME
                with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                    audit.sync_intent(PROPOSAL, "operator")
                    audit.sync_outcome(PROPOSAL, "verified")
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                mutate(rows)
                path.write_text("".join(json.dumps(row) + "\n" for row in rows))
                with self.assertRaises(AuditUnavailable):
                    ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True)

    def test_recovery_refuses_duplicate_keys_and_nonfinite_numbers(self):
        for replacement in ('"kind":"intent","kind":"intent"',
                            '"kind":"intent","untrusted":NaN'):
            with self.subTest(replacement=replacement), TemporaryDirectory() as directory:
                path = Path(directory) / AUDIT_NAME
                with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                    audit.sync_intent(PROPOSAL, "operator")
                    audit.sync_outcome(PROPOSAL, "verified")
                original = path.read_text()
                self.assertIn('"kind":"intent"', original)
                path.write_text(original.replace('"kind":"intent"', replacement, 1))
                with self.assertRaises(AuditUnavailable):
                    ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True)

    def test_recovery_refuses_oversized_single_record(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")
            lines = path.read_text().splitlines()
            lines[0] += " " * 4096
            path.write_text("\n".join(lines) + "\n")
            with self.assertRaises(AuditUnavailable):
                ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True)

    def test_writer_rejects_unbounded_operator_before_intent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                with self.assertRaises(AuditUnavailable):
                    audit.sync_intent(PROPOSAL, "prompt body with spaces")
            self.assertEqual(path.read_bytes(), b"")

    def test_broker_restart_requires_and_uses_reconciled_state(self):
        class FakeActuator:
            def __init__(self):
                self.current = Config()
                self.applies = 0

            def apply(self, config):
                self.current = config
                self.applies += 1

            def verify(self, config):
                return matching_readback(config) if self.current == config else False

        actuator = FakeActuator()
        verifier = PasswordVerifier.provision("test-recovery-password-only")
        with TemporaryDirectory() as directory:
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                broker = BrokerCore(verifier, actuator, audit, api_uid=1000)
                proposal = broker.propose({"gpu_max_mhz": 1700}, base_revision=0, peer_uid=1000)
                token = broker.authorize(proposal.id, "test-recovery-password-only",
                                         operator="test", session="first", peer_uid=1000)
                self.assertEqual(broker.commit(proposal.id, token, operator="test",
                                               session="first", peer_uid=1000).status, "applied")
            with ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True) as audit:
                with self.assertRaises(AuditUnavailable):
                    audit.sync_intent(PROPOSAL, "test")
                config, revision = audit.reconcile_verified(actuator)
                restarted = BrokerCore(verifier, actuator, audit, api_uid=1000,
                                       initial_config=config, initial_revision=revision)
                self.assertEqual((restarted.config.gpu_max_mhz, restarted.revision), (1700, 1))
                self.assertEqual(actuator.applies, 1)  # readback, never replay

    def test_prior_schema_without_pid_fields_can_be_readback_reconciled(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / AUDIT_NAME
            with ConfigAudit(Path(directory), boot_id=BOOT_ID) as audit:
                audit.sync_intent(PROPOSAL, "operator")
                audit.sync_outcome(PROPOSAL, "verified")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            for field_name in ("cpu_kp", "cpu_ki", "cpu_kd", "gpu_kp", "gpu_ki",
                               "gpu_kd", "cpu_derivative_tau_s", "gpu_derivative_tau_s",
                               "cpu_tracking_tau_s", "gpu_tracking_tau_s",
                               "cpu_entry_ratio", "cpu_recovery_ratio_s", "cpu_idle_down_ratio_s"):
                del rows[0]["config"][field_name]
            rows[0]["digest"] = sha256(json.dumps(rows[0]["config"], sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            class Readback:
                def verify(self, config):
                    return matching_readback(config) if config == CONFIG else False

            with ConfigAudit(Path(directory), boot_id=BOOT_ID, recovery_mode=True) as audit:
                config, revision = audit.reconcile_verified(Readback())
                self.assertEqual((config, revision), (CONFIG, 1))


if __name__ == "__main__":
    unittest.main()
