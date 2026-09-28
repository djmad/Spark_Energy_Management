"""SQ/TH criteria evaluation on synthetic trace rows (no I/O)."""
import unittest

from analysis.evaluate_qualification import evaluate


def rows(n, *, gpu=50.0, acpi=75.0, cap=1800, fan=6, mode="RUN"):
    return [{"utc_ns": int((1000 + i) * 1e9), "gpu_c": gpu, "acpi_c": {"acpi_TSOC": acpi},
             "gpu_cap_mhz": cap, "fan_floor": fan, "mode": mode,
             "vllm_gen_tokens": 200.0 * i} for i in range(n)]


class EvaluateQualificationTests(unittest.TestCase):
    def test_sq_pass_and_throughput(self):
        report = evaluate(rows(900), gpu_target=75, cpu_target=90, preferred_fan=6, aborts=[])
        self.assertTrue(report["pass"])
        self.assertAlmostEqual(report["aggregate_gen_tok_s"], 200.0, delta=0.5)

    def test_sq_fails_on_abort_or_heat(self):
        self.assertFalse(evaluate(rows(900), gpu_target=75, cpu_target=90, preferred_fan=6,
                                  aborts=["guard: abort"])["pass"])
        self.assertFalse(evaluate(rows(900, gpu=78), gpu_target=75, cpu_target=90,
                                  preferred_fan=6, aborts=[])["pass"])

    def test_th_band(self):
        ok = evaluate(rows(900, gpu=51, acpi=73), gpu_target=50, cpu_target=72,
                      preferred_fan=6, aborts=[], kind="TH")
        self.assertTrue(ok["pass"])
        hot = evaluate(rows(900, gpu=57, acpi=73), gpu_target=50, cpu_target=72,
                       preferred_fan=6, aborts=[], kind="TH")
        self.assertFalse(hot["pass"])


if __name__ == "__main__":
    unittest.main()
