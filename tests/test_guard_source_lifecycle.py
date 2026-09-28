from contextlib import contextmanager
from functools import partial
from multiprocessing import get_context
import os
import unittest

from energy_control.guard_ownership_process import GuardOwnershipProcess
from energy_control.process_http_transport import GuardHttpCancellation
from test_process_http_transport import sample_safe, never_verified


@contextmanager
def source(messages, closed, *, fail=False):
    messages.put(os.getpid())
    try:
        if fail:
            raise RuntimeError("synthetic startup failure")
        yield sample_safe
    finally:
        closed.set()


class GuardSourceLifecycleTests(unittest.TestCase):
    def test_sources_created_and_closed_in_guard_child(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                context = get_context("spawn")
                messages = context.Queue()
                closed, abort = context.Event(), context.Event()
                guard = GuardOwnershipProcess(GuardHttpCancellation(abort), never_verified, None,
                    sample_factory=partial(source, messages, closed, fail=failed), deadline_s=1)
                guard.start()
                try:
                    child_pid = messages.get(timeout=2)
                    self.assertNotEqual(child_pid, os.getpid())
                    if not failed:
                        self.assertTrue(guard.register("ab" * 16))
                    guard.close()
                    guard.join(2)
                    self.assertEqual(guard.exitcode, 2)
                    self.assertTrue(abort.is_set())
                    self.assertTrue(closed.is_set())
                finally:
                    guard.close()
                    guard.join(2)
                    messages.close()
                    messages.join_thread()
