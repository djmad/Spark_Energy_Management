import unittest

from energy_control.history import GraphHistory, GraphSample


def sample(second, cpu=70, gpu=60, cpu_util=50, gpu_util=80):
    return GraphSample(second, 1_800_000_000_000_000_000 + int(second * 1e9),
                       cpu, gpu, cpu_util, gpu_util)


class HistoryTests(unittest.TestCase):
    def test_empty_and_invalid_query(self):
        history = GraphHistory()
        empty = history.query("15m")
        self.assertEqual(empty["buckets"], [])
        self.assertEqual(empty["bucket_width_ms"], 1500)
        self.assertIsNone(empty["coverage_start_mono_s"])
        for window, pixels in (("2d", 100), ("15m", 0), ("15m", 601), ("15m", 1.0)):
            with self.subTest(window=window, pixels=pixels), self.assertRaises(ValueError):
                history.query(window, pixels)

    def test_peak_mean_and_missing_series(self):
        history = GraphHistory()
        history.add(sample(0, gpu=70))
        history.add(sample(.2, gpu=90, cpu=None))
        row = history.query("15m")["buckets"][-1]["values"]
        self.assertEqual(row["gpu_temp_c"], {"count": 2, "min": 70,
                                               "max": 90, "mean": 80})
        self.assertEqual(row["cpu_temp_c"]["count"], 1)
        quality = history.query("15m")["buckets"][-1]["quality"]
        self.assertEqual(quality["sample_count"], 2)
        self.assertEqual(quality["missing_samples"]["cpu_temp_c"], 1)
        self.assertEqual(quality["missing_samples"]["gpu_temp_c"], 0)
        self.assertTrue(quality["open"])

    def test_gaps_and_downsampling_preserve_extrema(self):
        history = GraphHistory()
        history.add(sample(0, gpu=60))
        history.add(sample(3, gpu=91))
        fine = history.query("15m", 600)["buckets"]
        self.assertEqual(len(fine), 3)
        self.assertEqual([row["monotonic_start_s"] for row in fine], [0, 1.5, 3])
        self.assertEqual(fine[1]["values"]["gpu_temp_c"]["count"], 0)
        self.assertIsNone(fine[1]["values"]["gpu_temp_c"]["mean"])
        self.assertTrue(fine[1]["quality"]["gap"])
        self.assertIsNone(fine[1]["utc_start_ns"])
        coarse = history.query("15m", 1)["buckets"]
        self.assertEqual(len(coarse), 1)
        self.assertEqual(coarse[0]["values"]["gpu_temp_c"]["max"], 91)

    def test_coverage_bounds_do_not_count_stale_time_as_data(self):
        history = GraphHistory()
        history.add(sample(10))
        view = history.query("15m", now_s=20)
        self.assertEqual(view["coverage_start_mono_s"], 9)
        self.assertEqual(view["coverage_end_mono_s"], 10)
        self.assertEqual(view["coverage_s"], 1)
        self.assertGreater(view["query_end_mono_s"], view["coverage_end_mono_s"])
        self.assertEqual(view["bucket_width_ms"], 1500)

    def test_bounded_memory_and_retention(self):
        history = GraphHistory()
        for second in range(0, 2 * 86400, 30):
            history.add(sample(second))
        self.assertTrue(all(count <= 600 for count in history.bucket_counts().values()))
        result = history.query("1d", 600)
        self.assertLessEqual(len(result["buckets"]), 600)
        self.assertLessEqual(result["coverage_s"], 86400)
        self.assertGreater(result["coverage_s"], 86000)

    def test_bad_data_rejected_without_mutating_history(self):
        history = GraphHistory()
        history.add(sample(1))
        for bad in (sample(1), sample(2, gpu=float("nan")),
                    sample(2, gpu_util=101), sample(2, cpu=-20)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                history.add(bad)
        self.assertEqual(history.query("15m")["buckets"][-1]["values"]["gpu_temp_c"]["count"], 1)

    def test_expiration_without_new_samples(self):
        history = GraphHistory()
        history.add(sample(10))
        self.assertEqual(len(history.query("1d")["buckets"]), 1)
        self.assertEqual(history.query("1d", now_s=86411)["buckets"], [])
        self.assertEqual(history.bucket_counts()["1d"], 0)

    def test_promotion_keeps_one_copy_and_preserves_peak(self):
        history = GraphHistory()
        for second in range(0, 4001, 10):
            history.add(sample(second, gpu=92 if second == 100 else 60))
        counts = history.bucket_counts()
        self.assertLessEqual(counts["15m"], 600)
        self.assertLessEqual(counts["60m"], 450)
        self.assertLessEqual(counts["1d"], 575)
        day = history.query("1d", 600)["buckets"]
        self.assertEqual(sum(row["values"]["gpu_temp_c"]["count"] for row in day), 401)
        self.assertEqual(max(row["values"]["gpu_temp_c"]["max"] for row in day
                             if row["values"]["gpu_temp_c"]["count"]), 92)

    def test_packed_budget_and_large_gap_promotion(self):
        history = GraphHistory()
        self.assertLessEqual(history.storage_bytes(), 256 * 1024)
        for index in range(2668):
            second = index * 1.5
            history.add(sample(second, gpu=92 if index == 100 else 60))
        history.add(sample(11201, gpu=61))
        day = history.query("1d", 600)["buckets"]
        self.assertEqual(sum(row["values"]["gpu_temp_c"]["count"] for row in day), 2669)
        self.assertEqual(max(row["values"]["gpu_temp_c"]["max"] for row in day
                             if row["values"]["gpu_temp_c"]["count"]), 92)
        self.assertLessEqual(sum(history.bucket_counts().values()), 1680)

    def test_utc_jump_does_not_shift_old_buckets_and_survives_promotion(self):
        history = GraphHistory()
        history.add(sample(0))
        jumped = sample(2)
        history.add(GraphSample(jumped.monotonic_s, jumped.utc_ns + 10_000_000_000,
                                jumped.cpu_temp_c, jumped.gpu_temp_c,
                                jumped.cpu_util_pct, jumped.gpu_util_pct))
        fine = history.query("15m")["buckets"]
        self.assertEqual(fine[0]["utc_start_ns"], sample(0).utc_ns)
        self.assertTrue(fine[-1]["quality"]["clock_discontinuity"])
        history.add(sample(1000, gpu=80))
        day = history.query("1d")["buckets"]
        self.assertEqual(day[0]["utc_start_ns"], sample(0).utc_ns)
        self.assertTrue(day[0]["quality"]["clock_discontinuity"])


if __name__ == "__main__":
    unittest.main()
