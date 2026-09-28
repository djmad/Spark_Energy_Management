from time import sleep, monotonic
import unittest

from energy_control.abort import AbortCoordinator
from energy_control.emergency_gpu_process import EmergencyGpuProcess
import test_abort as fixtures


def success(maximum):
    return maximum == 500


def stalled(maximum):
    sleep(30)
    return True


def delayed(maximum):
    sleep(.2)
    return True


class EmergencyProcessTests(unittest.TestCase):
    def test_late_result_is_observed_without_retry(self):
        executor = EmergencyGpuProcess(delayed)
        try:
            executor.start()
            self.assertFalse(executor(500))
            deadline = monotonic() + 2
            while executor.outcome() is None and monotonic() < deadline:
                sleep(.01)
            self.assertIs(executor.outcome(), True)
            with self.assertRaises(RuntimeError):
                executor(500)
        finally:
            executor.close()

    def test_failed_spawn_cannot_dispatch_or_restart(self):
        executor = EmergencyGpuProcess(lambda _: True)
        with self.assertRaises(Exception):
            executor.start()
        with self.assertRaises(RuntimeError):
            executor(500)
        with self.assertRaises(RuntimeError):
            executor.start()
        executor.close()

    def test_stalled_gpu_does_not_prevent_cancellation(self):
        for callback in (success, stalled):
            with self.subTest(callback=callback.__name__):
                executor = EmergencyGpuProcess(callback)
                try:
                    executor.start()
                    control = fixtures.FakeControl()
                    started = monotonic()
                    result = AbortCoordinator(control, emergency_gpu=executor).trip("hot")
                    self.assertLess(monotonic() - started, 1)
                    self.assertIn("cancel_owned_requests", control.calls)
                    self.assertIn("terminate_owned_processes", control.calls)
                    self.assertEqual(result.emergency_gpu_verified, callback is success)
                    with self.assertRaises(RuntimeError):
                        executor(500)
                finally:
                    executor.close()
