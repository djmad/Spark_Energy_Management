from time import monotonic, sleep
import unittest

from energy_control.nvml_event_process import NvmlEventProcess
from energy_control.nvml_events import EventObservation


class QuietReader:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def poll(self):
        sleep(0.02)
        return None


class EventReader(QuietReader):
    def poll(self):
        return EventObservation(monotonic(), 8, 79)


class FailedReader(QuietReader):
    def poll(self):
        raise OSError("fake driver loss")


class ClockThenCritical(QuietReader):
    def __init__(self):
        self.calls = 0
    def poll(self):
        sleep(0.05)
        self.calls += 1
        return EventObservation(monotonic(), 16 if self.calls < 4 else 24, 0)


class StalledReader(QuietReader):
    def poll(self):
        sleep(10)


class EventProcessTests(unittest.TestCase):
    def test_clock_notifications_continue_but_combined_critical_faults(self):
        monitor = self.start(ClockThenCritical)
        self.await_state(monitor, "CLOCK_CHANGE")
        self.assertGreaterEqual(monitor.clock_events, 1)
        fault = self.await_state(monitor, "FAULT")
        self.assertEqual(fault.event_type, 24)
        self.assertEqual(monitor.clock_events, 3)

    def start(self, factory):
        monitor = NvmlEventProcess(reader_factory=factory)
        self.addCleanup(monitor.close)
        self.assertEqual(monitor.check().state, "UNAVAILABLE")
        monitor.start()
        return monitor

    def await_state(self, monitor, desired, timeout=4):
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            status = monitor.check()
            if status.state == desired:
                return status
            sleep(0.01)
        self.fail(f"did not reach {desired}: {status}")

    def test_quiet_reader_and_no_restart(self):
        monitor = self.start(QuietReader)
        self.await_state(monitor, "QUIET")
        with self.assertRaises(RuntimeError):
            monitor.start()
        self.assertTrue(monitor.close())
        self.assertEqual(monitor.check().state, "UNAVAILABLE")

    def test_event_latches_fault_with_data(self):
        monitor = self.start(EventReader)
        status = self.await_state(monitor, "FAULT")
        self.assertEqual((status.event_type, status.event_data), (8, 79))
        self.assertEqual(monitor.check(), status)

    def test_reader_error_faults(self):
        self.await_state(self.start(FailedReader), "FAULT")

    def test_stall_does_not_block_supervisor(self):
        monitor = self.start(StalledReader)
        self.await_state(monitor, "FAULT")
        started = monotonic()
        self.assertTrue(monitor.close())
        self.assertLess(monotonic() - started, 1)
