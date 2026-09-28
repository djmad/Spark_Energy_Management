"""RC twin fitter recovers a known synthetic plant (no hardware)."""
from random import Random
import unittest

from analysis.fit_twin import AMBIENT, PARAMETERS, rmse, report, simulate


TRUE = [12.0, 30.0, 400.0, 0.9, 3.0, 1.2, 4.0, 40.0, 2.0]
TRUE_AMBIENT = 27.0


def synthetic_segment(seed, length=240):
    rng = Random(seed)
    inputs, gpu_w, load, fan = [], 5.0, 0.1, 0.4
    for index in range(length):
        if index % 40 == 0:  # Load and fan steps excite the dynamics.
            gpu_w, load, fan = rng.uniform(4, 16), rng.uniform(0.05, 0.9), rng.uniform(0.3, 1.0)
        inputs.append([float(index), 45.0, 40.0, gpu_w, load, fan])
    temps = simulate(TRUE, TRUE_AMBIENT, inputs)
    return [(t, cpu, gpu, w, l, f) for (t, _, _, w, l, f), (cpu, gpu) in zip(inputs, temps)]


class FitTwinTests(unittest.TestCase):
    def test_fit_predicts_holdout_traces(self):
        train = [synthetic_segment(seed) for seed in (1, 2, 3)]
        holdout = [synthetic_segment(seed) for seed in (11, 12)]
        start = [v for _, v, _, _ in PARAMETERS]
        self.assertGreater(sum(rmse(start, AMBIENT[1], holdout)), 1.0)  # Default twin is off.
        result = report(train, holdout, repeats=0)
        self.assertLess(result["holdout_rmse_c"]["cpu"] + result["holdout_rmse_c"]["gpu"], 0.5)


if __name__ == "__main__":
    unittest.main()
