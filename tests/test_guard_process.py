"""Fake-only tests for the process-isolated deadline tripwire."""

from multiprocessing import get_context
import os
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.guard_process import GuardDeadlineProcess


class SharedFakeControl:
    def __init__(self, context, *, fail_cancel=False):
        self.closed = context.Event()
        self.cancelled = context.Event()
        self.terminated = context.Event()
        self.fail_cancel = fail_cancel

    def close_admission(self):
        self.closed.set()

    def cancel_owned_requests(self):
        self.cancelled.set()
        if self.fail_cancel:
            raise RuntimeError("injected cancellation failure")

    def terminate_owned_processes(self):
        self.terminated.set()

    def verify_quiescent(self, timeout_s):
        return self.closed.is_set() and self.cancelled.is_set() and self.terminated.is_set()


def _policy_process_exits_without_cleanup(control, ready):
    guard = GuardDeadlineProcess(AbortCoordinator(control), deadline_s=0.2,
                                 poll_s=0.01)
    guard.start()
    ready.send(guard.pid)
    ready.close()
    os._exit(0)  # Only the fake policy subprocess dies, not the test runner.


class GuardProcessTests(unittest.TestCase):
    def make_guard(self, deadline_s=0.2):
        control = SharedFakeControl(get_context("fork"))
        return GuardDeadlineProcess(AbortCoordinator(control),
                                    deadline_s=deadline_s, poll_s=0.01), control

    def assert_protected(self, guard, control):
        guard.join()
        self.assertEqual(guard.exitcode, 1)
        self.assertTrue(control.closed.is_set())
        self.assertTrue(control.cancelled.is_set())
        self.assertTrue(control.terminated.is_set())

    def test_heartbeat_timeout_aborts_from_a_separate_process(self):
        guard, control = self.make_guard()
        guard.start()
        self.assertNotEqual(guard.pid, os.getpid())
        guard.heartbeat()
        self.assert_protected(guard, control)
        guard.close_heartbeat()

    def test_channel_close_is_fault_not_clean_disarm(self):
        guard, control = self.make_guard()
        guard.start()
        guard.close_heartbeat()
        self.assert_protected(guard, control)

    def test_not_a_rearmable_or_live_hardware_controller(self):
        guard, control = self.make_guard()
        with self.assertRaises(RuntimeError):
            guard.heartbeat()
        guard.start()
        with self.assertRaises(RuntimeError):
            guard.start()
        guard.close_heartbeat()
        self.assert_protected(guard, control)

    def test_guard_survives_fake_policy_process_exit(self):
        context = get_context("fork")
        control = SharedFakeControl(context)
        ready_receive, ready_send = context.Pipe(duplex=False)
        policy = context.Process(target=_policy_process_exits_without_cleanup,
                                 args=(control, ready_send))
        policy.start()
        ready_send.close()
        self.assertTrue(ready_receive.poll(2.0))
        guard_pid = ready_receive.recv()
        ready_receive.close()
        policy.join(2.0)
        self.assertEqual(policy.exitcode, 0)
        self.assertNotEqual(guard_pid, policy.pid)
        self.assertTrue(control.closed.wait(2.0))
        self.assertTrue(control.cancelled.is_set())
        self.assertTrue(control.terminated.is_set())

    def test_cancellation_failure_does_not_skip_process_termination(self):
        control = SharedFakeControl(get_context("fork"), fail_cancel=True)
        guard = GuardDeadlineProcess(AbortCoordinator(control), deadline_s=0.2,
                                     poll_s=0.01)
        guard.start()
        guard.close_heartbeat()
        guard.join()
        self.assertEqual(guard.exitcode, 2)
        self.assertTrue(control.closed.is_set())
        self.assertTrue(control.cancelled.is_set())
        self.assertTrue(control.terminated.is_set())


if __name__ == "__main__":
    unittest.main()
