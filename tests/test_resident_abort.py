import unittest

from energy_control.admission import OwnedRequestGate, AdmissionClosed
from energy_control.resident_abort import resident_abort, stop_test_loads


class ResidentAbortTests(unittest.TestCase):
    def test_stop_report_does_not_hide_closed_but_unverified_requests(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        request = gate.register(lambda: None)
        gate.mark_client_done(request)
        result = stop_test_loads(gate)
        self.assertEqual((result.remaining_queued, result.remaining_active), (0, 0))
        self.assertEqual(result.server_unverified, 1)
        self.assertFalse(gate.verify_admission_and_requests())

    def test_normal_stop_requests_cancellation_without_requiring_terminal_receipts(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        cancelled = []
        gate.register(lambda: cancelled.append("queued"))
        active = gate.register(lambda: cancelled.append("active"))
        gate.mark_active(active)
        result = stop_test_loads(gate)
        self.assertEqual(cancelled, ["queued", "active"])
        self.assertTrue(result.cancellation_issued)
        self.assertEqual((result.remaining_queued, result.remaining_active), (1, 1))
        self.assertFalse(gate.verify_admission_and_requests())
        self.assertEqual(result.action_errors, ())
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)

    def test_active_and_queued_requests_cancel_without_service_lifecycle(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        cancelled = []
        def register():
            rid = gate.register(lambda: (cancelled.append(rid), gate.mark_terminal(rid)))
            return rid
        queued, active = register(), register()
        gate.mark_active(active)
        caps = []
        coordinator = resident_abort(gate, (), emergency_gpu=lambda cap: caps.append(cap) or True)
        result = coordinator.trip("thermal abort")
        self.assertEqual(caps, [500])
        self.assertEqual(set(cancelled), {queued, active})
        self.assertEqual(gate.counts(), (0, 0))
        self.assertTrue(result.verified_quiescent)
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)

    def test_cancel_ack_missing_leaves_owned_request_and_abort_unverified(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        gate.register(lambda: None)
        result = resident_abort(gate, (), emergency_gpu=lambda _: True,
                                verify_timeout_s=.001).trip("hot")
        self.assertTrue(result.emergency_gpu_verified)
        self.assertFalse(result.verified_quiescent)
        self.assertEqual(gate.counts(), (1, 0))

    def test_no_container_adapter_or_missing_emergency_adapter(self):
        with self.assertRaises(ValueError):
            resident_abort(OwnedRequestGate(), (object(),), emergency_gpu=lambda _: True)
        with self.assertRaises(ValueError):
            resident_abort(OwnedRequestGate(), (), emergency_gpu=None)
