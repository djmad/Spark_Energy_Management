import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analysis.legacy_cpu_trace import summarize


def sample(ms, phase, cpu, *, ratio=1):
    return {"event": "sample", "ms": ms, "phase": phase, "hottest_c": cpu,
            "gpu": {"temp_c": 70, "power_w": 40, "util_pct": 90},
            "guard": {"cap_ratio": ratio}}


class LegacyTraceTests(unittest.TestCase):
    def test_bounded_aggregate_and_missing_cooldown_guard(self):
        rows = [sample(0, "baseline", 70), sample(500, "baseline", 71),
                sample(1000, "load", 80), sample(1500, "load", 90, ratio=0.8),
                sample(2000, "load", 92, ratio=0.6),
                sample(2500, "cooldown", 85, ratio=None),
                sample(3000, "cooldown", 84, ratio=None)]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.ndjson"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = summarize(path)
            self.assertEqual(report["load_first_threshold_s"]["90"], 0.5)
            self.assertEqual(report["first_cpu_cap_reduction_s"], 0.5)
            self.assertEqual(report["cooldown_missing_guard_samples"], 2)
            self.assertFalse(report["hardware_qualified"])
            self.assertNotIn("trace", report)

    def test_reversed_time_and_symlink_refused(self):
        rows = [sample(0, "baseline", 70), sample(500, "load", 80),
                sample(400, "load", 82), sample(1000, "cooldown", 75, ratio=None)]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.ndjson"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaises(ValueError):
                summarize(path)
            alias = Path(directory) / "alias.ndjson"
            alias.symlink_to(path)
            with self.assertRaises(OSError):
                summarize(alias)

    def test_guard_input_is_distinct_from_outer_temperature(self):
        rows = [sample(0, "baseline", 70), sample(500, "baseline", 71),
                sample(1000, "load", 80), sample(1500, "load", 90),
                sample(2000, "cooldown", 85)]
        for index, row in enumerate(rows):
            row["guard"].update({"hottest": {"c": row["hottest_c"] + 2},
                                 "at": f"sample-{min(index, 1)}"})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "trace.ndjson"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            report = summarize(path)
        self.assertEqual(report["guard_temperature_pairs"], 5)
        self.assertEqual(report["guard_vs_outer_temperature_max_abs_c"], 2)
        self.assertEqual(report["repeated_guard_status_samples"], 3)


if __name__ == "__main__":
    unittest.main()
