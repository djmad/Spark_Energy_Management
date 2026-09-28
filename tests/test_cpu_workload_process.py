from functools import partial
from multiprocessing import get_context
import subprocess
import sys
import unittest

from energy_control.cpu_workload_process import serve_cpu_workload


def sleeping_child(started):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started.set()
    return child


class CpuWorkloadProcessTests(unittest.TestCase):
    def test_abort_normal_stop_and_parent_loss_stop_owned_dummy(self):
        for trigger in ("abort", "stop", "eof", "before_start"):
            with self.subTest(trigger=trigger):
                context = get_context("spawn")
                parent, child = context.Pipe()
                abort, started = context.Event(), context.Event()
                owner = context.Process(target=serve_cpu_workload,
                    args=(child, parent, partial(sleeping_child, started), abort, 10))
                owner.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(2))
                    self.assertEqual(parent.recv_bytes(2), b"RD")
                    self.assertFalse(started.is_set())
                    if trigger == "before_start":
                        abort.set()
                    else:
                        parent.send_bytes(b"GO")
                        self.assertTrue(parent.poll(2))
                        self.assertEqual(parent.recv_bytes(2), b"ON")
                        if trigger == "abort":
                            abort.set()
                        elif trigger == "stop":
                            parent.send_bytes(b"STOP")
                        else:
                            parent.close()
                    owner.join(3)
                    self.assertEqual(owner.exitcode, 0)
                    if trigger == "stop":
                        self.assertFalse(abort.is_set())
                    if trigger == "before_start":
                        self.assertFalse(started.is_set())
                finally:
                    abort.set()
                    parent.close()
                    owner.join(3)
