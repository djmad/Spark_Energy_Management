from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from energy_control.abort import AbortCoordinator
from energy_control.broker_abort import BrokerAbortSink
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_cpu_actuation import FakeControl


BOOT_ID = "00000000-0000-0000-0000-000000000001"


class BrokerAbortSinkTests(unittest.TestCase):
    def test_disk_failure_does_not_block_abort_or_allow_clean_end(self):
        with TemporaryDirectory() as directory:
            recorder = CommissioningRecorder(Path(directory), boot_id=BOOT_ID)
            path = Path(directory) / recorder.run_id / "events.jsonl"
            calls = []
            sink = BrokerAbortSink(AbortCoordinator(FakeControl(calls)), recorder)
            with patch.object(recorder, "write_event", side_effect=OSError("fake disk")):
                result = sink.trip("broker durable outcome failed")
            self.assertTrue(result.verified_quiescent)
            self.assertIn("terminate_owned_processes", calls)
            self.assertFalse(recorder.ready)
            self.assertFalse(sink.abort_recorded)
            self.assertFalse(inspect_run(path)["clean_end"])
            with self.assertRaises(RuntimeError):
                recorder.close(clean=True)


if __name__ == "__main__":
    unittest.main()
