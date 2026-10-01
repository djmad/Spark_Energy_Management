"""analysis/calorimetry_fit.py: event parsing, CPU split, and the model. Synthetic only."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from analysis.calorimetry_fit import ORDER, blocks, cpu_parts, gpu_windows, simulate
from energy_control.power_estimate import estimate_cpu_power_w


def trace_row(util, mhz_by_cpu):
    return {"cluster_util_pct": util, "cpu_mhz": [mhz_by_cpu(cpu) for cpu in ORDER]}


class CalorimetryFitTests(unittest.TestCase):
    def test_cpu_split_adds_up_to_the_service_estimate(self):
        util = {"E0": 40.0, "P0": 100.0, "E1": 10.0, "P1": 70.0}
        mhz = lambda cpu: 3500.0 if 5 <= cpu <= 9 or 15 <= cpu <= 19 else 2100.0
        static, dyn_p, dyn_e = cpu_parts(trace_row(util, mhz))
        policies = tuple(SimpleNamespace(index=cpu, measured_mhz=mhz(cpu)) for cpu in range(20))
        self.assertAlmostEqual(static + dyn_p + dyn_e, estimate_cpu_power_w(util, policies), delta=0.06)
        self.assertGreater(dyn_p, dyn_e)
        self.assertIsNone(cpu_parts({"cluster_util_pct": util, "cpu_mhz": [1.0] * 5}))

    def test_events_become_blocks(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            lines = [{"utc": "2026-10-01T22:00:00+02:00", "event": "block", "kind": "gpu",
                      "label": "gpu-1500-f2", "mhz": 1500, "fan": 2, "phase": "start"},
                     {"utc": "2026-10-01T22:09:00+02:00", "event": "block", "kind": "gpu",
                      "label": "gpu-1500-f2", "mhz": 1500, "fan": 2, "phase": "end",
                      "reason": "completed"}]
            path.write_text("".join(json.dumps(line) + "\n" for line in lines))
            found = blocks(path)
        block = found[0]
        self.assertEqual((block["label"], block["kind"], block["fan"], block["reason"]),
                         ("gpu-1500-f2", "gpu", 2, "completed"))
        self.assertEqual(block["end"] - block["start"], 540.0)
        self.assertEqual(gpu_windows(found), [])        # no cool-down logged yet

    def test_windows_join_the_idle_before_and_the_cool_down_and_skip_aborts(self):
        found = [{"label": "settle-gpu-2500-f12", "kind": "idle", "fan": 12, "start": 0.0, "end": 120.0},
                 {"label": "gpu-2500-f12", "kind": "gpu", "fan": 12, "start": 123.0, "end": 543.0,
                  "reason": "completed"},
                 {"label": "after-gpu-2500-f12", "kind": "idle", "fan": 12, "start": 546.0, "end": 906.0},
                 {"label": "gpu-1500-f2", "kind": "gpu", "fan": 2, "start": 1000.0, "end": 1010.0,
                  "reason": "readiness vanished"}]
        self.assertEqual(gpu_windows(found), [(12, 0.0, 906.0)])

    def test_steady_state_has_no_error_and_cpu_heat_warms_the_plate(self):
        params = [25.0, 300.0, 3.0, 2.5, 2.5, 17.0, 0.35]
        # (t, gpu W, cpu static, dyn P, dyn E, fan, TGPU, activity): steady at 30 W GPU
        p_in = 30.0 + 3.6 + 17.0
        ga = 5.0
        tgpu = 21.0 + p_in / ga + p_in / 3.0 + (0.35 + 0.09 * 0.6) * 30.0
        rows = [(float(t), 30.0, 3.6, 0.0, 0.0, 12, tgpu, 0.6) for t in range(120)]
        self.assertLess(max(abs(e) for e in simulate(params, rows)), 1e-6)
        hot = [(t, g, s, 10.0, 0.0, f, tg, a) for t, g, s, _, _, f, tg, a in rows]
        self.assertGreater(simulate(params, hot)[-1], 3.0)          # 10 W more CPU heat
        self.assertGreater(simulate(params, hot, kP=2.0)[-1], simulate(params, hot)[-1])


if __name__ == "__main__":
    unittest.main()
