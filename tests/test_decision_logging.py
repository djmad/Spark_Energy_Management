from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.broker import Config
from energy_control.decision_logging import record_shadow_decision
from energy_control.policy import PolicyInput, ProposedLimits, ShadowPolicy
from energy_control.recorder import CommissioningRecorder, inspect_run
from test_safety import good_snapshot


BOOT_ID = "00000000-0000-0000-0000-000000000001"


class DecisionLoggingTests(unittest.TestCase):
    def test_normal_and_abort_proposals_use_fixed_codes_only(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                normal = ProposedLimits(1300, 3000, 2500, 12, False, "RAMP",
                                        ("busy dwell satisfied",))
                self.assertEqual(record_shadow_decision(recorder, normal), 2)
                fault = ProposedLimits(500, 1378, 338, 12, True, "ABORT",
                                       ("projected temperature breach: PRIVATE PROMPT",))
                self.assertEqual(record_shadow_decision(recorder, fault), 3)
            records = inspect_run(path)["records"]
            self.assertEqual(records[1]["reason_code"], "busy_dwell")
            self.assertEqual(records[2]["reason_code"], "projected_temperature")
            self.assertNotIn("PRIVATE PROMPT", path.read_text())
            self.assertEqual(records[1]["scope"], "candidate")

    def test_inconsistent_or_over_limit_proposal_is_refused(self):
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                with self.assertRaises(ValueError):
                    record_shadow_decision(recorder, ProposedLimits(
                        OVER_MAX, 3000, 2500, 12, False, "RAMP", ("busy",)))
                with self.assertRaises(ValueError):
                    record_shadow_decision(recorder, ProposedLimits(
                        500, 1378, 338, 12, False, "ABORT", ("fault",)))

    def test_real_shadow_proposal_can_be_recorded_without_hardware_claim(self):
        policy = ShadowPolicy(Config())
        proposal = policy.step(PolicyInput(good_snapshot(), 0, 0.5,
                                           prefill_arrival=True))
        with TemporaryDirectory() as directory:
            with CommissioningRecorder(Path(directory), boot_id=BOOT_ID) as recorder:
                path = Path(directory) / recorder.run_id / "events.jsonl"
                record_shadow_decision(recorder, proposal)
            decision = inspect_run(path)["records"][1]
            self.assertEqual(decision["mode"], proposal.mode)
            self.assertEqual(decision["gpu_candidate_max_mhz"], proposal.gpu_max_mhz)
            self.assertEqual(decision["scope"], "candidate")


if __name__ == "__main__":
    unittest.main()
