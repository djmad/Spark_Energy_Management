from threading import Event, Lock
import unittest

from energy_control.admission import AdmissionClosed
from energy_control.abort import AbortCoordinator
from energy_control.owned_process import LocalOwnedWorkloads
from energy_control.request_gateway import OwnedRequestDispatcher
from energy_control.trial_plan import TrialProposal
from test_safety import good_snapshot


class FakeHandle:
    def __init__(self, *, ack_on_cancel=True):
        self.started = Event()
        self.cancelled = Event()
        self.terminal = Event()
        self.ack_on_cancel = ack_on_cancel

    def start(self):
        if not self.cancelled.is_set():
            self.started.set()

    def cancel(self):
        self.cancelled.set()
        if self.ack_on_cancel:
            self.terminal.set()

    def wait_terminal(self, timeout_s):
        return self.terminal.wait(timeout_s)


class StartsEvenAfterCancellation(FakeHandle):
    """Expose a dispatcher bug that a cooperative fake handle would mask."""

    def start(self):
        self.started.set()


class FakeTransport:
    def __init__(self, handle, *, hold_prepare=False):
        self.handle = handle
        self.entered_prepare = Event()
        self.release_prepare = Event()
        if not hold_prepare:
            self.release_prepare.set()
        self.prepared = 0

    def prepare(self, request, *, workload_id):
        self.workload_id = workload_id
        self.prepared += 1
        self.entered_prepare.set()
        if not self.release_prepare.wait(1):
            raise TimeoutError("fake prepare not released")
        return self.handle


class FakeRecorder:
    def __init__(self, *, fail_intent=False, trial_proposal=None, fail_dispatch=False):
        self.fail_intent = fail_intent
        self.fail_dispatch = fail_dispatch
        self.trial_proposal = trial_proposal
        self.events = []
        self.lock = Lock()

    def write_intent(self, kind, *, requested_mhz=None, workload_id=None):
        with self.lock:
            self.events.append(kind)
        if self.fail_intent:
            raise OSError("fake sync failure")
        self.assert_id = workload_id
        return 7

    def write_outcome(self, intent_seq, *, accepted_mhz=None, measured_mhz=None,
                      verified=False):
        with self.lock:
            self.events.append(("outcome", intent_seq, verified))

    def write_dispatch_intent(self, intent_seq):
        with self.lock:
            self.events.append(("dispatch_intent", intent_seq))
        if self.fail_dispatch:
            raise OSError("fake dispatch sync failure")
        return 8


class FakeGuardOwnership:
    def __init__(self, *, register_ok=True, terminal_ok=True, start_ok=True):
        self.register_ok = register_ok
        self.terminal_ok = terminal_ok
        self.start_ok = start_ok
        self.registered = []
        self.start_ids = []
        self.terminal_ids = []

    def register(self, workload_id):
        self.registered.append(workload_id)
        return self.register_ok

    def terminal(self, workload_id):
        self.terminal_ids.append(workload_id)
        return self.terminal_ok

    def authorize_start(self, workload_id):
        self.start_ids.append(workload_id)
        return self.start_ok


class RequestGatewayTests(unittest.TestCase):
    def test_transport_receives_same_owned_id_as_guard_and_log(self):
        handle = FakeHandle()
        transport = FakeTransport(handle)
        recorder = FakeRecorder()
        guard = FakeGuardOwnership()
        dispatcher = OwnedRequestDispatcher(transport, recorder, guard_ownership=guard,
                                             terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit({"workload_id": "caller-cannot-select-this", "prompt": "private"})
        self.assertTrue(handle.started.wait(0.5))
        self.assertEqual(transport.workload_id, recorder.assert_id)
        self.assertEqual(guard.registered, [transport.workload_id])
        self.assertEqual(guard.start_ids, [transport.workload_id])
        self.assertEqual(len(transport.workload_id), 32)
        dispatcher.close_admission()
        dispatcher.cancel_owned_requests()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertEqual(guard.terminal_ids, [transport.workload_id])

    def test_dispatch_sync_failure_prevents_guard_authorization_and_start(self):
        handle = StartsEvenAfterCancellation()
        guard = FakeGuardOwnership()
        dispatcher = OwnedRequestDispatcher(
            FakeTransport(handle), FakeRecorder(fail_dispatch=True),
            guard_ownership=guard, terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertFalse(handle.started.is_set())
        self.assertTrue(handle.cancelled.is_set())
        self.assertEqual(guard.start_ids, [])
        self.assertFalse(dispatcher.verify_admission_and_requests())

    def test_trial_waiting_request_obeys_active_limit_and_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                proposal = TrialProposal(
                    3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                    admission_cap=2, reserved_token_cap=1280,
                    cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                    config_digest="0" * 64)
                first = FakeHandle()
                second = StartsEvenAfterCancellation()
                prepared_second = Event()

                class Transport:
                    def prepare(self, request, *, workload_id):
                        if request is second:
                            prepared_second.set()
                        return request

                dispatcher = OwnedRequestDispatcher(
                    Transport(), FakeRecorder(trial_proposal=proposal),
                    trial_proposal=proposal, token_measure=lambda _: (100, 50),
                    terminal_timeout_s=1)
                dispatcher.arm_admission()
                dispatcher.submit(first)
                self.assertTrue(first.started.wait(0.5))
                dispatcher.submit(second)
                self.assertTrue(prepared_second.wait(0.5))
                self.assertFalse(second.started.wait(0.05))
                self.assertEqual(dispatcher.counts(), (1, 1))
                if cancel:
                    dispatcher.close_admission()
                    dispatcher.cancel_owned_requests()
                else:
                    first.terminal.set()
                    self.assertTrue(second.started.wait(0.5))
                    dispatcher.close_admission()
                    second.terminal.set()
                self.assertTrue(dispatcher.join_workers(1))
                self.assertTrue(dispatcher.verify_admission_and_requests())
                if cancel:
                    self.assertFalse(second.started.is_set())
                    self.assertTrue(second.cancelled.is_set())

    def test_cancellation_during_start_check_prevents_dispatch(self):
        handle = StartsEvenAfterCancellation()
        guard = FakeGuardOwnership()
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            guard_ownership=guard,
                                            terminal_timeout_s=0.5)

        def cancel_during_check(workload_id):
            dispatcher.close_admission()
            dispatcher.cancel_owned_requests()
            return True

        guard.authorize_start = cancel_during_check
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertFalse(handle.started.is_set())
        self.assertTrue(handle.cancelled.is_set())
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_guard_refusal_after_prepare_prevents_start_and_cancels(self):
        handle = StartsEvenAfterCancellation()
        transport = FakeTransport(handle, hold_prepare=True)
        recorder = FakeRecorder()
        guard = FakeGuardOwnership()
        dispatcher = OwnedRequestDispatcher(transport, recorder, guard_ownership=guard,
                                            terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(transport.entered_prepare.wait(0.2))
        self.assertEqual(recorder.events, ["admit_workload"])
        self.assertEqual(guard.start_ids, [])
        guard.start_ok = False  # Safety changes while preparation is blocked.
        transport.release_prepare.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertEqual(guard.start_ids, guard.registered)
        self.assertFalse(handle.started.is_set())
        self.assertTrue(handle.cancelled.is_set())
        self.assertEqual(guard.terminal_ids, [])
        self.assertFalse(dispatcher.verify_admission_and_requests())
        self.assertEqual(recorder.events, ["admit_workload", ("dispatch_intent", 7)])

    def test_trial_envelope_counts_cumulative_admissions_after_completion(self):
        proposal = TrialProposal(3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                                 admission_cap=2, reserved_token_cap=1200,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        recorder = FakeRecorder(trial_proposal=proposal)
        handle = FakeHandle()
        dispatcher = OwnedRequestDispatcher(
            FakeTransport(handle), recorder, trial_proposal=proposal,
            token_measure=lambda request: request, terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        handle.terminal.set()
        for _ in range(2):
            dispatcher.submit((400, 100))
            self.assertTrue(dispatcher.join_workers(0.5))
        self.assertEqual(dispatcher.counts(), (0, 0))
        with self.assertRaisesRegex(ValueError, "run-wide admission"):
            dispatcher.submit((1, 1))
        self.assertEqual(recorder.events.count("admit_workload"), 2)
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_trial_envelope_reserves_upper_output_tokens(self):
        proposal = TrialProposal(3, 1, 30, 0, 1, 1, 1200, 1200, 12, 512, 128,
                                 admission_cap=2, reserved_token_cap=600,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        recorder = FakeRecorder(trial_proposal=proposal)
        handle = FakeHandle()
        dispatcher = OwnedRequestDispatcher(
            FakeTransport(handle), recorder, trial_proposal=proposal,
            token_measure=lambda request: request, terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        handle.terminal.set()
        dispatcher.submit((400, 100))
        self.assertTrue(dispatcher.join_workers(0.5))
        with self.assertRaisesRegex(ValueError, "run-wide admission or token"):
            dispatcher.submit((100, 100))
        self.assertEqual(recorder.events.count("admit_workload"), 1)

    def test_trial_envelope_limits_slots_and_fails_closed_on_token_excess(self):
        proposal = TrialProposal(3, 1, 30, 0, 1, 0, 1200, 1200, 12, 512, 128,
                                 admission_cap=1, reserved_token_cap=640,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        recorder = FakeRecorder(trial_proposal=proposal)
        handle = FakeHandle()
        transport = FakeTransport(handle)
        dispatcher = OwnedRequestDispatcher(
            transport, recorder, trial_proposal=proposal,
            token_measure=lambda request: request, terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit((500, 128))
        self.assertTrue(handle.started.wait(0.2))
        with self.assertRaises(AdmissionClosed):
            dispatcher.submit((1, 1))
        self.assertEqual(transport.prepared, 1)
        handle.terminal.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        with self.assertRaisesRegex(ValueError, "token bounds"):
            dispatcher.submit((513, 1))
        self.assertEqual(transport.prepared, 1)
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_trial_envelope_rejects_unbound_proposal_or_missing_token_reader(self):
        proposal = TrialProposal(3, 1, 30, 0, 1, 0, 1200, 1200, 12, 512, 128,
                                 admission_cap=1, reserved_token_cap=640,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        with self.assertRaisesRegex(ValueError, "durable recorder"):
            OwnedRequestDispatcher(FakeTransport(FakeHandle()), FakeRecorder(),
                                   trial_proposal=proposal, token_measure=lambda _: (1, 1))
        with self.assertRaisesRegex(ValueError, "trusted token"):
            OwnedRequestDispatcher(FakeTransport(FakeHandle()),
                                   FakeRecorder(trial_proposal=proposal),
                                   trial_proposal=proposal)

    def test_cpu_only_stage_has_zero_llm_admission_slots(self):
        proposal = TrialProposal(2, 1, 30, 4, 0, 0, 1200, 1200, 12,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest="0" * 64)
        recorder = FakeRecorder(trial_proposal=proposal)
        transport = FakeTransport(FakeHandle())
        dispatcher = OwnedRequestDispatcher(transport, recorder,
                                            trial_proposal=proposal)
        dispatcher.arm_admission()
        with self.assertRaises(AdmissionClosed):
            dispatcher.submit(object())
        self.assertEqual(transport.prepared, 0)
        self.assertEqual(recorder.events, [])

    def test_dispatcher_cannot_send_before_arming(self):
        transport = FakeTransport(FakeHandle())
        recorder = FakeRecorder()
        dispatcher = OwnedRequestDispatcher(transport, recorder)
        with self.assertRaises(AdmissionClosed):
            dispatcher.submit(object())
        self.assertEqual(transport.prepared, 0)
        self.assertEqual(recorder.events, [])

    def test_intent_before_dispatch_and_upstream_terminal_ack(self):
        handle = FakeHandle()
        transport = FakeTransport(handle)
        recorder = FakeRecorder()
        dispatcher = OwnedRequestDispatcher(transport, recorder, terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        self.assertEqual(recorder.events[0], "admit_workload")
        self.assertEqual(dispatcher.counts(), (0, 1))
        dispatcher.close_admission()
        self.assertFalse(dispatcher.verify_admission_and_requests())
        handle.terminal.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertTrue(dispatcher.verify_admission_and_requests())
        self.assertEqual(recorder.events[-1], ("outcome", 7, True))

    def test_abort_during_prepare_never_starts_fake_upstream(self):
        handle = StartsEvenAfterCancellation()
        transport = FakeTransport(handle, hold_prepare=True)
        dispatcher = OwnedRequestDispatcher(transport, FakeRecorder(), terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(transport.entered_prepare.wait(0.2))
        dispatcher.close_admission()
        dispatcher.cancel_owned_requests()
        transport.release_prepare.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertTrue(handle.cancelled.is_set())
        self.assertFalse(handle.started.is_set())
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_active_abort_waits_for_upstream_ack(self):
        handle = FakeHandle(ack_on_cancel=False)
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        dispatcher.close_admission()
        dispatcher.cancel_owned_requests()
        self.assertTrue(handle.cancelled.is_set())
        self.assertFalse(dispatcher.verify_admission_and_requests())
        handle.terminal.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_timeout_is_fault_not_false_quiescence(self):
        handle = FakeHandle(ack_on_cancel=False)
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            terminal_timeout_s=0.02)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertEqual(dispatcher.counts(), (0, 1))
        self.assertFalse(dispatcher.verify_admission_and_requests())
        self.assertIn("TimeoutError", dispatcher.faults()[0])
        with self.assertRaises(AdmissionClosed):
            dispatcher.submit(object())

    def test_intent_failure_or_full_gate_never_dispatches(self):
        transport = FakeTransport(FakeHandle())
        dispatcher = OwnedRequestDispatcher(transport, FakeRecorder(fail_intent=True))
        dispatcher.arm_admission()
        with self.assertRaises(OSError):
            dispatcher.submit(object())
        self.assertEqual(transport.prepared, 0)
        self.assertTrue(dispatcher.verify_admission_and_requests())

        handle = FakeHandle(ack_on_cancel=False)
        recorder = FakeRecorder()
        limited = OwnedRequestDispatcher(FakeTransport(handle), recorder,
                                         max_requests=1, terminal_timeout_s=0.5)
        limited.arm_admission()
        limited.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        with self.assertRaises(AdmissionClosed):
            limited.submit(object())
        self.assertEqual(recorder.events, ["admit_workload", ("dispatch_intent", 7)])
        limited.close_admission()
        limited.cancel_owned_requests()
        handle.terminal.set()
        self.assertTrue(limited.join_workers(0.5))

    def test_abort_coordinator_waits_for_dispatcher_terminal_ack(self):
        handle = FakeHandle()
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        control = LocalOwnedWorkloads(
            [], close_admission=dispatcher.close_admission,
            cancel_owned_requests=dispatcher.cancel_owned_requests,
            verify_admission_and_requests=dispatcher.verify_admission_and_requests)
        result = AbortCoordinator(control).evaluate(good_snapshot(fan_healthy=False))
        self.assertTrue(result.verified_quiescent, result.action_errors)
        self.assertTrue(handle.cancelled.is_set())
        self.assertTrue(dispatcher.join_workers(0.5))

    def test_guard_ownership_ack_precedes_worker_and_terminal_removal(self):
        handle = FakeHandle()
        guard = FakeGuardOwnership()
        recorder = FakeRecorder()
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), recorder,
                                            guard_ownership=guard,
                                            terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertEqual(len(guard.registered), 1)
        self.assertEqual(recorder.assert_id, guard.registered[0])
        self.assertEqual(len(guard.registered[0]), 32)
        self.assertTrue(handle.started.wait(0.2))
        dispatcher.close_admission()
        handle.terminal.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertEqual(guard.terminal_ids, guard.registered)
        self.assertTrue(dispatcher.verify_admission_and_requests())

    def test_missing_guard_registration_or_terminal_ack_fails_closed(self):
        transport = FakeTransport(FakeHandle())
        recorder = FakeRecorder()
        guard = FakeGuardOwnership(register_ok=False)
        dispatcher = OwnedRequestDispatcher(transport, recorder, guard_ownership=guard)
        dispatcher.arm_admission()
        with self.assertRaises(RuntimeError):
            dispatcher.submit(object())
        self.assertEqual(transport.prepared, 0)
        self.assertEqual(recorder.events, [])
        self.assertTrue(dispatcher.verify_admission_and_requests())

        handle = FakeHandle()
        guard = FakeGuardOwnership(terminal_ok=False)
        dispatcher = OwnedRequestDispatcher(FakeTransport(handle), FakeRecorder(),
                                            guard_ownership=guard,
                                            terminal_timeout_s=0.5)
        dispatcher.arm_admission()
        dispatcher.submit(object())
        self.assertTrue(handle.started.wait(0.2))
        dispatcher.close_admission()
        handle.terminal.set()
        self.assertTrue(dispatcher.join_workers(0.5))
        self.assertFalse(dispatcher.verify_admission_and_requests())
        self.assertIn("RuntimeError", dispatcher.faults()[0])


if __name__ == "__main__":
    unittest.main()
