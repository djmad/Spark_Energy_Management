"""Serial GPU+CPU per-token throughput model (pure fit, no I/O)."""
import unittest

from analysis.llm_throughput import cpu_share, fit_decode, fit_prefill


class ThroughputModelTests(unittest.TestCase):
    def test_recovers_serial_gpu_and_cpu_time(self):
        a, b = 30.0, 60.0  # s*MHz per token
        obs = [{"gpu_mhz": g, "cpu_fast_mhz": c, "decode_tok_s": 1 / (a / g + b / c),
                "prefill_tok_s": 0.9 * g}
               for g, c in ((1200, 3900), (1800, 3900), (1800, 1378), (1500, 2600))]
        decode = fit_decode(obs, intercept=False)
        self.assertAlmostEqual(decode["A_s_mhz_per_token"], a, places=6)
        self.assertAlmostEqual(decode["B_s_mhz_per_token"], b, places=6)
        self.assertAlmostEqual(fit_prefill(obs)["tok_s_per_mhz"], 0.9, places=9)
        self.assertAlmostEqual(cpu_share(decode, 1800, 3900), (b / 3900) / (a / 1800 + b / 3900))

    def test_recovers_clock_independent_time(self):
        a, b, d = 20.0, 30.0, 0.004
        obs = [{"gpu_mhz": g, "cpu_fast_mhz": c, "decode_tok_s": 1 / (a / g + b / c + d)}
               for g, c in ((1200, 3400), (1800, 3400), (1800, 2600), (1800, 1378), (1500, 3400))]
        decode = fit_decode(obs)
        self.assertAlmostEqual(decode["A_s_mhz_per_token"], a, places=5)
        self.assertAlmostEqual(decode["B_s_mhz_per_token"], b, places=5)
        self.assertAlmostEqual(decode["D_s_per_token"], d, places=8)

    def test_twin_carries_the_measured_model(self):
        from simulation.model import GB10_LLM
        # Measured healthy rates; tolerance = fit RMSE (1.8 tok/s).
        self.assertAlmostEqual(GB10_LLM.decode_tok_s(1800, 3400), 39.9, delta=2.0)
        self.assertAlmostEqual(GB10_LLM.decode_tok_s(1800, 1400), 37.0, delta=2.0)
        self.assertAlmostEqual(GB10_LLM.decode_tok_s(1200, 3300), 31.5, delta=2.0)
        self.assertGreater(GB10_LLM.prefill_tok_s(1800), GB10_LLM.prefill_tok_s(1200))

    def test_refuses_underdetermined_data(self):
        with self.assertRaises(ValueError):
            fit_decode([{"gpu_mhz": 1800, "cpu_fast_mhz": 3900, "decode_tok_s": 30}] * 2)


if __name__ == "__main__":
    unittest.main()
