from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from energy_control.abort import AbortCoordinator
from energy_control.broker_abort import BrokerAbortSink
from energy_control.exclusive_abort import ExclusiveWindowLoads
from energy_control.llm_container import ContainerIdentity, ExclusiveLlmContainer
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_broker_abort import BOOT_ID


class ExclusiveAbortTests(unittest.TestCase):
    def test_exclusive_abort_block_including_disk_failure_and_residual_gate(self):
        for drained in (False, True):
            with self.subTest(drained=drained), TemporaryDirectory() as directory:
                original = ContainerIdentity("ab" * 32, "running", 123, "boot-a", "no", 0)
                state, calls, closed = [original], [], [False]
                def runner(argv, **kwargs):
                    calls.append(argv[1])
                    if argv[1] == "kill":
                        self.assertTrue(closed[0])
                        state[0] = replace(original, status="exited", pid=0)
                    return SimpleNamespace(returncode=0, stdout=json.dumps(asdict(state[0])))
                container = ExclusiveLlmContainer(original, exclusive_window=lambda: True,
                                                  enable_stop=True, runner=runner)
                control = ExclusiveWindowLoads(container, [],
                    close_admission=lambda: closed.__setitem__(0, True),
                    verify_admission_closed=lambda: closed[0],
                    verify_residual_work=lambda timeout: drained)
                recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
                path = Path(directory) / recorder.run_id / "events.jsonl"
                coordinator = AbortCoordinator(control)
                sink = BrokerAbortSink(coordinator, recorder)
                with patch.object(recorder, "write_event", side_effect=OSError("disk failed")):
                    result = sink.trip("test thermal trip")
                self.assertEqual(result.verified_quiescent, drained)
                self.assertEqual(calls.count("kill"), 1)
                self.assertFalse(inspect_run(path)["clean_end"])
                coordinator.trip("repeated trip")
                self.assertEqual(calls.count("kill"), 1)
