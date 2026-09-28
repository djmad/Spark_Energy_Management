"""Estimated CPU power. GB10 has no CPU power sensor (no RAPL, no hwmon power
channel). Operator, 27 September 2026: "wir wollen die ~XX W Anzeige bei der
CPU … es ist klar, dass die Werte nur geschätzt sind".

Per cluster: P = idle + (full - idle) x utilisation x (f / f_max) ^ exponent,
summed over E0, P0, E1, P1, plus a base. Pure function, no device I/O.

Calibrated calorimetrically on 27 September 2026 (doc/55 §6; operator: "for
now we use the measured values from our calibration cycle"). The GPU, whose
power nvidia-smi measures, was the reference heater. cpu_burnin.py was
pinned to the P cores, the E cores, all 20, and the P cores at 2600 MHz.
analysis/calorimetry.py fits each sensor's response to GPU heat and solves
for the CPU heat per block. The sensors agree on the ratios, but their
absolute scale spreads by a factor of about 4.6. The model therefore uses the
geometric mean over the downstream sensors (TGPU, Wi-Fi, NVMe, plate):
- P cores at 3482 MHz: 18.8 W above idle; P cores at 2600 MHz: 9.5 W. The
  exponent is 2.34.
- E cores at 2777 MHz: 2.6 W. All 20 cores: 22.2 W, and the model predicts
  20.6 W.
The previous twin values were P 12.5 W, E 4 W per cluster and exponent 1.7.
The idle and base watts are not calibrated: calorimetry sees only the heat
above idle. The absolute value is good to about a factor of 2 until a direct
measurement exists, since GB10 reports no module power.
"""
from dataclasses import dataclass, field

CLUSTER_CPUS = {"E0": range(0, 5), "P0": range(5, 10), "E1": range(10, 15), "P1": range(15, 20)}


@dataclass(frozen=True)
class CpuPowerModel:
    version: str = "calorimetric-v2 (27 Sep 2026, scale about x2)"
    exponent: float = 2.34
    base_w: float = 1.0
    # cluster -> (idle W, full-load W at f_max, f_max MHz); full = idle + fitted dynamic
    # watts at f_max (P 12.22, E 1.36; analysis/calorimetry.py model_fit).
    clusters: dict = field(default_factory=lambda: {
        "E0": (0.5, 1.86, 2808.0), "P0": (0.8, 13.02, 3900.0),
        "E1": (0.5, 1.86, 2808.0), "P1": (0.8, 13.02, 3900.0)})


MODEL = CpuPowerModel()


def estimate_cpu_power_w(cluster_util_pct, cpu_policies, model=MODEL):
    """Estimated CPU power in W, or None without utilisation or clocks.

    ``cluster_util_pct``: {"E0": %, ...}; ``cpu_policies``: readout policies
    (``index``, ``measured_mhz``)."""
    if not isinstance(cluster_util_pct, dict) or not cpu_policies:
        return None
    measured = {getattr(p, "index", None): getattr(p, "measured_mhz", None) for p in cpu_policies}
    total = model.base_w
    for name, (idle_w, full_w, f_max) in model.clusters.items():
        util = cluster_util_pct.get(name)
        clocks = [measured.get(cpu) for cpu in CLUSTER_CPUS[name]]
        clocks = [c for c in clocks if isinstance(c, (int, float)) and c > 0]
        if not isinstance(util, (int, float)) or not clocks:
            return None
        ratio = min(1.0, max(0.0, sum(clocks) / len(clocks) / f_max))
        load = min(1.0, max(0.0, util / 100.0))
        total += idle_w + (full_w - idle_w) * load * ratio ** model.exponent
    return round(total, 1)
