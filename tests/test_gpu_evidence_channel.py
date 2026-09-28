from dataclasses import replace
import socket
from time import monotonic
import unittest
from unittest.mock import patch

from energy_control.gpu_evidence import GpuSetterEvidence
from energy_control.gpu_evidence_channel import GpuEvidenceReader, publish_evidence


class EvidenceChannelTests(unittest.TestCase):
    def setUp(self):
        self.receive, self.send = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.receive.close)
        self.addCleanup(self.send.close)
        self.context = ("boot", "driver", "owner", "ab" * 16)
        now = monotonic()
        self.proof = GpuSetterEvidence(200, 1200, 3, 0, now, now, *self.context)
        self.reader = GpuEvidenceReader(self.receive, self.context)

    def test_fresh_evidence_preserves_command_time_and_expires(self):
        publish_evidence(self.send, self.proof)
        self.assertEqual(self.reader.read(), self.proof)
        with patch("energy_control.gpu_evidence_channel.monotonic",
                   return_value=self.proof.ownership_checked_monotonic_s + 1.6):  # window 1.5 s
            self.assertIsNone(self.reader.read())
        publish_evidence(self.send, self.proof)
        self.assertIsNone(self.reader.read())  # Fault stays latched.

    def test_invalidated_evidence_is_not_a_numeric_cap(self):
        publish_evidence(self.send, self.proof)
        self.assertIsNotNone(self.reader.read())
        publish_evidence(self.send, None)
        self.assertIsNone(self.reader.read())

    def test_wrong_owner_or_rewritten_intent_is_rejected(self):
        publish_evidence(self.send, self.proof)
        self.assertIsNotNone(self.reader.read())
        publish_evidence(self.send, None)
        self.assertIsNone(self.reader.read())
        publish_evidence(self.send, replace(self.proof, requested_max_mhz=1800))
        self.assertIsNone(self.reader.read())
        other = GpuEvidenceReader(self.receive, self.context)
        publish_evidence(self.send, replace(self.proof, owner_epoch="foreign"))
        self.assertIsNone(other.read())
