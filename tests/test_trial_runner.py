"""Pure parts of the live trial runner (no hardware)."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from energy_control.trial_plan import validate_trial_proposal
from energy_control.trial_runner import (cache_ratio, entry_trial_plan, previous_failures,
                                         request_body, token_upper_bound)


class TrialRunnerTests(unittest.TestCase):
    def test_entry_plan_is_a_valid_bound_stage_3_trial(self):
        for mhz in (1200, 1500, 1800):
            proposal, config = entry_trial_plan(mhz, 1, words=19000, max_tokens=10000, jobs=4)
            validate_trial_proposal(proposal)
            self.assertEqual((proposal.gpu_max_mhz, proposal.gpu_entry_mhz), (mhz, mhz))
            self.assertEqual((config.gpu_max_mhz, config.fan_min_state), (mhz, 12))

    def test_identification_plan_holds_a_lower_fan_floor(self):
        for floor in (12, 8, 4, 2):
            proposal, config = entry_trial_plan(1400, 1, words=19000, max_tokens=10000, jobs=4,
                                                fan_floor=floor)
            validate_trial_proposal(proposal)
            self.assertEqual((proposal.fan_min_state, config.fan_min_state,
                              config.fan_preferred_state), (floor, floor, floor))

    def test_cache_ratio_marks_cache_served_prompts(self):
        self.assertIsNone(cache_ratio({}))
        self.assertEqual(cache_ratio({"prompt_tokens": 1000, "prompt_tokens_cached": 992}), 0.992)
        self.assertEqual(cache_ratio({"prompt_tokens": 1000}), 0.0)

    def test_cpu_impact_plan_caps_and_starts_fast_cores_at_the_cap(self):
        proposal, config = entry_trial_plan(1800, 1, words=100, max_tokens=10, jobs=4,
                                            cpu_fast_max_mhz=2600, cpu_entry_ratio=1.0)
        self.assertEqual((config.cpu_fast_max_mhz, config.cpu_entry_ratio), (2600, 1.0))
        self.assertEqual(proposal.cpu_fast_max_mhz, 2600)
        validate_trial_proposal(proposal)

    def test_cpu_impact_plan_can_cap_both_clusters(self):
        proposal, config = entry_trial_plan(1800, 1, words=100, max_tokens=10, jobs=4,
                                            cpu_fast_max_mhz=1378, cpu_slow_max_mhz=1400,
                                            cpu_entry_ratio=1.0)
        self.assertEqual((config.cpu_fast_max_mhz, config.cpu_slow_max_mhz), (1378, 1400))
        self.assertEqual((proposal.cpu_fast_max_mhz, proposal.cpu_slow_max_mhz), (1378, 1400))
        validate_trial_proposal(proposal)

    def test_every_request_has_a_unique_opening(self):
        # Identical prompts were served from vLLM's prefix cache (no real prefill).
        a, b = request_body(100, 10), request_body(100, 10)
        text = lambda body: body["messages"][0]["content"]
        self.assertNotEqual(text(a)[:40], text(b)[:40])
        self.assertEqual(text(request_body(100, 10, nonce="x")),
                         text(request_body(100, 10, nonce="x")))

    def test_token_bound_covers_the_synthetic_prompt(self):
        body = request_body(19000, 10000)
        prompt, output = token_upper_bound(body)
        proposal, _ = entry_trial_plan(1200, 1, words=19000, max_tokens=10000, jobs=4)
        self.assertLessEqual(prompt, proposal.prompt_token_cap)
        self.assertGreaterEqual(prompt, 19000 * 1.05)  # Covers the measured 1.05 tokens/word.
        self.assertEqual(output, 10000)
        self.assertTrue(body["ignore_eos"])
        self.assertEqual((proposal.active_llm, proposal.admission_cap), (4, 4))
        self.assertTrue(body["stream"])

    def test_marker_is_written_atomically(self):
        from energy_control.markers import write_marker
        with TemporaryDirectory() as directory:
            path = write_marker("sustained-load", "stopped", directory=directory, elapsed_s=12)
            self.assertEqual(json.loads(path.read_text())["outcome"], "stopped")
            self.assertEqual([p.suffix for p in Path(directory).iterdir()], [".json"])

    def test_open_intent_or_failed_result_blocks_the_step(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "series.jsonl"
            rows = [{"event": "intent", "mhz": 1200, "repetition": 1},
                    {"event": "result", "mhz": 1200, "repetition": 1, "passed": True},
                    {"event": "intent", "mhz": 1300, "repetition": 1},  # Power loss here.
                    {"event": "intent", "mhz": 1400, "repetition": 1},
                    {"event": "result", "mhz": 1400, "repetition": 1, "passed": False}]
            path.write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(previous_failures(path), {1300, 1400})
            with open(path, "a") as handle:  # Reviewed: 1400 was a harness error.
                handle.write(json.dumps({"event": "correction", "mhz": 1400, "repetition": 1,
                                         "passed": True}) + "\n")
            self.assertEqual(previous_failures(path), {1300})


if __name__ == "__main__":
    unittest.main()
