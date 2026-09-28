from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
from multiprocessing import get_context
from functools import partial
from dataclasses import replace
from time import monotonic, sleep
import unittest

from energy_control.guard_ownership_process import GuardOwnershipProcess, _sample_fault
GuardOwnershipProcess = partial(GuardOwnershipProcess, start_method="fork")  # legacy fake closures
from energy_control.nvml_event_process import EventProcessStatus
from energy_control.nvml_events import EventObservation
from energy_control.safety import CommissioningGuard
from test_guard_ownership_process import FakeAbort, FakeTerminalCheck, SafeSampler


class SharedEventStatus:
    def __init__(self, context):
        self.failed = context.Event()

    def __call__(self):
        if self.failed.is_set():
            return EventProcessStatus("FAULT", monotonic(), 8, 79)
        return EventProcessStatus("QUIET", monotonic())


class TriggeredReader:
    def __init__(self, trigger):
        self.trigger = trigger
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def poll(self):
        if self.trigger.wait(0.02):
            return EventObservation(monotonic(), 8, 79)
        return None


class StartupFailure:
    def __init__(self):
        raise OSError("fake initialization failure")


class GuardEventTests(unittest.TestCase):
    def test_clock_notification_requires_independent_safe_snapshot(self):
        source = lambda: EventProcessStatus("CLOCK_CHANGE", monotonic(), 16, 0)
        self.assertIsNone(_sample_fault(CommissioningGuard(), SafeSampler(), 0.1,
                                       read_event_status=source))
        unsafe = lambda: replace(SafeSampler()(), gpu_requested_max_mhz=OVER_MAX)
        self.assertIsNotNone(_sample_fault(CommissioningGuard(), unsafe, 0.1,
                                          read_event_status=source))
        combined = lambda: EventProcessStatus("CLOCK_CHANGE", monotonic(), 24, 0)
        self.assertIn("event monitor", _sample_fault(CommissioningGuard(), SafeSampler(), 0.1,
                                                     read_event_status=combined))

    def test_reader_startup_failure_aborts_before_registration(self):
        context = get_context("fork")
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
            reply_timeout_s=2, event_reader_factory=StartupFailure)
        self.addCleanup(guard.close)
        guard.start()
        owned, reason = callback.messages.get(timeout=3)
        self.assertEqual(owned, ())
        self.assertIn("event monitor", reason)
        guard.join(timeout_s=2)
        self.assertEqual(guard.exitcode, 1)
        with self.assertRaises(RuntimeError):
            guard.register("ef" * 16)

    def test_guard_owns_spawned_event_reader_and_aborts_on_delivery(self):
        context = get_context("fork")
        trigger = get_context("spawn").Event()
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
            deadline_s=2, check_s=0.05, reply_timeout_s=2,
            event_reader_factory=partial(TriggeredReader, trigger))
        self.addCleanup(guard.close)
        guard.start()
        identifier = "cd" * 16
        self.assertTrue(guard.register(identifier))
        self.assertTrue(guard.authorize_start(identifier))
        trigger.set()
        owned, reason = callback.messages.get(timeout=2)
        self.assertEqual(owned, (identifier,))
        self.assertIn("event monitor", reason)
        guard.join(timeout_s=2)
        self.assertEqual(guard.exitcode, 1)

    def test_event_fault_aborts_registered_work_without_policy_step(self):
        context = get_context("fork")
        events = SharedEventStatus(context)
        callback = FakeAbort(context)
        guard = GuardOwnershipProcess(callback, FakeTerminalCheck(context), SafeSampler(),
            deadline_s=2, check_s=0.05, read_event_status=events)
        self.addCleanup(guard.close)
        guard.start()
        identifier = "ab" * 16
        self.assertTrue(guard.register(identifier))
        self.assertTrue(guard.authorize_start(identifier))
        events.failed.set()
        # No policy observation/heartbeat causes this abort: child timer does.
        owned, reason = callback.messages.get(timeout=2)
        self.assertEqual(owned, (identifier,))
        self.assertIn("event monitor", reason)
        guard.join(timeout_s=2)
        self.assertEqual(guard.exitcode, 1)

    def test_unknown_stale_and_contradictory_health_are_rejected(self):
        for status in (None, EventProcessStatus("WAITING"), EventProcessStatus("UNAVAILABLE"),
                       EventProcessStatus("QUIET", monotonic() - 1),
                       EventProcessStatus("QUIET", monotonic() + 10),
                       EventProcessStatus("QUIET", float("nan")),
                       EventProcessStatus("QUIET", monotonic(), 8, 79)):
            with self.subTest(status=status):
                fault = _sample_fault(CommissioningGuard(), SafeSampler(), 0.1,
                                      read_event_status=lambda: status)
                self.assertIn("event monitor", fault)

    def test_blocking_status_reader_is_bounded_in_guard_context(self):
        def blocked():
            sleep(1)
        started = monotonic()
        fault = _sample_fault(CommissioningGuard(), SafeSampler(), 0.1,
                              read_event_status=blocked)
        self.assertIn("event monitor acquisition failed", fault)
        self.assertLess(monotonic() - started, 0.3)
