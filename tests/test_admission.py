import unittest

from energy_control.admission import AdmissionClosed, OwnedRequestGate
from energy_control.owned_process import LocalOwnedWorkloads


class AdmissionTests(unittest.TestCase):
    def test_closure_cancellation_and_terminal_acknowledgement(self):
        gate = OwnedRequestGate(max_requests=2)
        gate.arm_admission()
        cancelled = []
        queued = gate.register(lambda: cancelled.append("queued"))
        active = gate.register(lambda: cancelled.append("active"))
        gate.mark_active(active)
        self.assertEqual(gate.counts(), (1, 1))
        gate.close_admission()
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)
        gate.cancel_owned_requests()
        self.assertEqual(cancelled, ["queued", "active"])
        self.assertFalse(gate.verify_admission_and_requests())
        gate.mark_terminal(queued)
        self.assertFalse(gate.verify_admission_and_requests())
        gate.mark_terminal(active)
        self.assertTrue(gate.verify_admission_and_requests())
        self.assertEqual(gate.counts(), (0, 0))

    def test_callback_failure_does_not_skip_another_request_and_can_retry(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        attempts = []

        def flaky():
            attempts.append("flaky")
            if attempts.count("flaky") == 1:
                raise RuntimeError("retry")

        gate.register(flaky)
        gate.register(lambda: attempts.append("other"))
        gate.close_admission()
        with self.assertRaises(RuntimeError):
            gate.cancel_owned_requests()
        self.assertEqual(attempts, ["flaky", "other"])
        gate.cancel_owned_requests()
        self.assertEqual(attempts, ["flaky", "other", "flaky"])
        self.assertFalse(gate.verify_admission_and_requests())

    def test_bounded_and_phase_transition(self):
        gate = OwnedRequestGate(max_requests=1)
        gate.arm_admission()
        rid = gate.register(lambda: None)
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)
        gate.mark_active(rid)
        with self.assertRaises(ValueError):
            gate.mark_active(rid)
        gate.mark_terminal(rid)
        gate.register(lambda: None)

    def test_process_adapter_must_use_ledger_verification(self):
        gate = OwnedRequestGate()
        gate.arm_admission()
        rid = gate.register(lambda: None)
        control = LocalOwnedWorkloads([], close_admission=gate.close_admission,
                                      cancel_owned_requests=gate.cancel_owned_requests,
                                      verify_admission_and_requests=gate.verify_admission_and_requests)
        control.close_admission()
        control.cancel_owned_requests()
        self.assertFalse(control.verify_quiescent(0.02))
        gate.mark_terminal(rid)
        self.assertTrue(control.verify_quiescent(0.02))

    def test_closed_on_boot_and_never_reopens_after_abort(self):
        gate = OwnedRequestGate()
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)
        gate.arm_admission()
        with self.assertRaises(AdmissionClosed):
            gate.arm_admission()
        gate.close_admission()
        with self.assertRaises(AdmissionClosed):
            gate.arm_admission()
        with self.assertRaises(AdmissionClosed):
            gate.register(lambda: None)


if __name__ == "__main__":
    unittest.main()
