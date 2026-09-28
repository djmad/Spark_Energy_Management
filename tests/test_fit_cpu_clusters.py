"""Per-cluster CPU twin fit on synthetic step data (pure, no I/O)."""
import unittest

from analysis.fit_cpu_clusters import CLUSTERS, ZONES, filtered, fit, predict_rmse


def synthetic(gains, tau_p, tau_e, *, order=CLUSTERS, load_s=60, rest_s=120):
    samples, t = [], 0.0
    schedule = [(None, 60)] + [x for c in order for x in ((c, load_s), (None, rest_s))]
    for cluster, duration in schedule:
        for _ in range(duration):
            samples.append((t, {c: 1.0 if c == cluster else 0.0 for c in CLUSTERS}, {}))
            t += 1.0
    xs = filtered(samples, tau_p, tau_e)
    baseline = {z: 40.0 for z in ZONES}
    out = [(t, load, {z: baseline[z] + sum(gains[z][c] * x[c] for c in CLUSTERS)
                      for z in ZONES}) for (t, load, _), x in zip(samples, xs)]
    return out, baseline


class CpuClusterFitTests(unittest.TestCase):
    def test_recovers_gains_and_predicts_a_different_schedule(self):
        gains = {z: {c: (36.0 if z[2:] == {"P0": "0P", "P1": "1P", "E0": "0E", "E1": "1E"}[c]
                         else 5.0) for c in CLUSTERS} for z in ZONES}
        train = [synthetic(gains, 2.0, 6.0, load_s=120, rest_s=180)]
        holdout = synthetic(gains, 2.0, 6.0, order=("P1", "E1", "P0", "E0"), load_s=60, rest_s=120)
        k, rmse = fit(train, 2.0, 6.0)
        self.assertLess(rmse, 1e-6)
        self.assertAlmostEqual(k["TS0P"]["P0"], 36.0, places=4)
        self.assertLess(predict_rmse(k, holdout, 2.0, 6.0)[0], 1e-6)


class TwinClusterModelTests(unittest.TestCase):
    def test_p_cluster_step_reaches_the_measured_rise(self):
        from simulation.model import GB10_CPU_CLUSTERS as model
        state, temps = None, None
        for _ in range(60):
            state, temps = model.step(state, {"P0": 1.0}, 1.0)
        self.assertAlmostEqual(temps["TS0P"] - 39.6, 42.9, delta=0.5)
        self.assertLess(temps["TGPU"] - 38.9, 2.0)
        for _ in range(10):
            state, temps = model.step(state, {}, 1.0)
        self.assertLess(temps["TS0P"], 40.0)  # Fast cool-down (tau_down 1 s).


if __name__ == "__main__":
    unittest.main()
