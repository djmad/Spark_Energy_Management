from energy_control.limits import GPU_HARD_MAX_MHZ
OVER_MAX = GPU_HARD_MAX_MHZ + 1  # first value above the hard envelope
OVER_MAX_F = GPU_HARD_MAX_MHZ + 0.01
import unittest
from dataclasses import replace

from energy_control.broker import Config, config_fingerprint
from energy_control.trial_plan import (TrialProposal, validate_trial_against_config,
                                       validate_trial_proposal)


class TrialPlanTests(unittest.TestCase):
    def test_exact_committed_policy_binding_includes_gains(self):
        config = Config()
        proposal = TrialProposal(2, 1, 30, 4, 0, 0, 1200, 1200, 12,
                                 cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                                 config_digest=config_fingerprint(config))
        self.assertIsNone(validate_trial_against_config(proposal, config))
        for changed in (dict(cpu_fast_max_mhz=3800),
                        dict(config_digest="0" * 64)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_trial_against_config(replace(proposal, **changed), config)
        with self.assertRaises(ValueError):
            validate_trial_against_config(proposal, Config(gpu_kp=0.07))

    def test_read_only_baseline(self):
        proposal = TrialProposal(0, 2, 120, 0, 0, 0)
        self.assertIsNone(validate_trial_proposal(proposal))
        for changed in (dict(cpu_cores=1), dict(gpu_max_mhz=1800),
                        dict(fan_min_state=12), dict(cpu_fast_max_mhz=3900)):
            with self.assertRaises(ValueError):
                validate_trial_proposal(replace(proposal, **changed))

    def test_qualified_small_and_typical_proposals(self):
        cases = (
            TrialProposal(1, 2, 120, 0, 0, 0, 1800, 1200, 12,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(2, 2, 30, 4, 0, 0, 1800, 1200, 12,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(3, 3, 30, 0, 1, 1, 1800, 1200, 12, 512, 128,
                          admission_cap=2, reserved_token_cap=1280,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(4, 3, 60, 4, 1, 4, 1800, 1200, 12, 512, 128,
                          admission_cap=5, reserved_token_cap=3200,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(5, 3, 120, 5, 4, 12, 1800, 1200, 12, 512, 128,
                          admission_cap=16, reserved_token_cap=10240,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(6, 10, 120, 5, 5, 12, 1800, 1200, 12, 512, 128, True, 4,
                          admission_cap=17, reserved_token_cap=10880,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
            TrialProposal(7, 2, 600, 5, 4, 12, 1800, 1200, 12, 512, 128,
                          admission_cap=100, reserved_token_cap=64000,
                          cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                          config_digest="0" * 64),
        )
        for proposal in cases:
            with self.subTest(stage=proposal.stage):
                self.assertIsNone(validate_trial_proposal(proposal))

    def test_rejects_invalid_limits_and_unbounded_llm(self):
        base = TrialProposal(5, 1, 120, 5, 4, 12, 1800, 1200, 12, 512, 128,
                             admission_cap=16, reserved_token_cap=10240,
                             cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                             config_digest="0" * 64)
        mutations = (
            dict(gpu_max_mhz=OVER_MAX), dict(gpu_entry_mhz=OVER_MAX),
            dict(fan_min_state=13), dict(duration_s=121),
            dict(cpu_fast_max_mhz=3901), dict(cpu_slow_max_mhz=2809),
            dict(repetition=4), dict(cpu_cores=6), dict(active_llm=5),
            dict(waiting_llm=13), dict(prompt_token_cap=None),
            dict(output_token_cap=16385), dict(prompt_token_cap=32769), dict(new_prefill_during_decode=True),
            dict(admission_cap=17), dict(reserved_token_cap=None),
            dict(config_digest=None),
            dict(stage=True), dict(duration_s=True),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_trial_proposal(replace(base, **mutation))

    def test_stage_six_bounds_peak_active_count_and_requires_prefill_flag(self):
        base = TrialProposal(6, 1, 120, 4, 2, 10, 1800, 1200, 12, 512, 128, True, 1,
                             admission_cap=12, reserved_token_cap=7680,
                             cpu_fast_max_mhz=3900, cpu_slow_max_mhz=2808,
                             config_digest="0" * 64)
        self.assertIsNone(validate_trial_proposal(base))
        for changed in (dict(active_llm=1), dict(active_llm=6),
                        dict(baseline_active_llm=2),
                        dict(new_prefill_during_decode=False)):
            with self.assertRaises(ValueError):
                validate_trial_proposal(replace(base, **changed))


if __name__ == "__main__":
    unittest.main()
