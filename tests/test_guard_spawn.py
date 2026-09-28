from multiprocessing import get_context
from threading import Event, Thread
import unittest

from energy_control.guard_ownership_process import GuardOwnershipProcess
from test_guard_ownership_process import FakeAbort, FakeTerminalCheck, SafeSampler


class SpawnGuardTests(unittest.TestCase):
    def test_default_spawn_with_parent_thread_preserves_owned_abort(self):
        context = get_context("spawn")
        abort = FakeAbort(context)
        stop = Event()
        thread = Thread(target=stop.wait)
        thread.start()
        guard = GuardOwnershipProcess(abort, FakeTerminalCheck(context), SafeSampler(),
                                       deadline_s=1, reply_timeout_s=2)
        try:
            guard.start()
            self.assertTrue(guard.register("ab" * 16))
            self.assertTrue(guard.authorize_start("ab" * 16))
            guard.close()
            owned, reason = abort.messages.get(timeout=3)
            self.assertIn("ab" * 16, owned)
            self.assertTrue(reason)
            guard.join()
        finally:
            guard.close()
            stop.set()
            thread.join(1)
            abort.messages.close()
            abort.messages.join_thread()

    def test_unserializable_callback_fails_closed_without_fork_fallback(self):
        guard = GuardOwnershipProcess(lambda *args: True, lambda _: True, SafeSampler())
        with self.assertRaises((AttributeError, TypeError)):
            guard.start()
        with self.assertRaises(RuntimeError):
            guard.start()
        with self.assertRaises(RuntimeError):
            guard.register("ab" * 16)
