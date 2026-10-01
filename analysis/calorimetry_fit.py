"""Fit the cooler and the CPU power scale from scripts/calorimetry_cooler.sh.

Operator, 1 October 2026: the heatsink was replaced and fitted with new thermal
pads ("es scheint der Wärmeübergang ist nun deutlich besser"; "ich vermute die
Kupfermenge stimmt nicht"). The run gives clean steps with the GPU as the
reference heater (measured W) at fixed fan floors. Model as in sink_fit.py
(room fixed at 21 C):

  plate  Cp dTp/dt = P_in - Gn (Tp - Tf)
  fins   Cf dTf/dt = Gn (Tp - Tf) - Ga(fan) (Tf - T_room)
  Ga(fan) = g0 + g1 * max(0.2, floor / 12)
  P_in   = P_gpu + P_cpu + (1 - s) * P_bg    into the plate
  s * P_bg goes straight into the fin block (NIC plate via heatpipe, board)
  P_cpu  = base + idle + kP * dyn_P + kE * dyn_E   (power_estimate.py, scaled)
  TGPU   = Tp + (r + R1 * a) * P_gpu

1. Cooler: Cp, Cf, Gn, g0, g1, P_bg, r, s on the GPU blocks at fan 12 and fan 2
   (CPU idle, kP = kE = 1); holdout: the GPU block at fan 6. R1 stays at the
   LLM refit's 0.090 K/W: the burn-in alone (activity about 1) cannot split
   r from R1.
2. CPU: with the cooler fixed, kP and kE on the P-core and E-core blocks;
   holdout: all 20 cores and the P cores at 2600 MHz (clock exponent).

    python3 -m analysis.calorimetry_fit EVENTS.jsonl [EVENTS2.jsonl ...] [--traces DIR] [--out result.json]
"""
from datetime import date, datetime, timedelta
import json
from math import exp, log
from pathlib import Path
import statistics as st
import sys

from analysis.sink_fit import ORDER, ROOM_C, activity, nelder_mead
from energy_control.cooler_twin import PARAMS
from energy_control.power_estimate import CLUSTER_CPUS, MODEL

R1 = 0.090
NAMES = ("Cp", "Cf", "Gn", "g0", "g1", "P_bg", "r", "s")
POS = {cpu: k for k, cpu in enumerate(ORDER)}   # trace cpu_mhz is in policy order


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def blocks(*paths):
    """Blocks in time order, each {label, kind, start, end, fan, mhz, reason},
    from one or more event logs (a resumed run adds a second log)."""
    found, open_blocks = [], {}
    for path in paths:
        for line in open(path):
            event = json.loads(line)
            if event.get("event") != "block":
                continue
            key = (str(path), event["label"])
            if event["phase"] == "start":
                open_blocks[key] = {"label": event["label"], "kind": event["kind"],
                                    "fan": event.get("fan"), "mhz": event.get("mhz"),
                                    "start": ts(event["utc"])}
            elif key in open_blocks:
                block = open_blocks.pop(key)
                block["end"] = ts(event["utc"])
                if "reason" in event:
                    block["reason"] = event["reason"]
                found.append(block)
    return sorted(found, key=lambda b: b["start"])


def gpu_windows(found):
    """(fan, start, end) per completed GPU block: from the start of the idle
    block before it to the end of its cool-down ("after-<label>")."""
    windows = []
    for k, block in enumerate(found):
        if block["kind"] != "gpu" or block.get("reason") != "completed":
            continue
        before = found[k - 1] if k and found[k - 1]["kind"] == "idle" else None
        after = next((b for b in found[k + 1:] if b["label"] == "after-" + block["label"]), None)
        if after is None:
            continue
        start = before["start"] if before and block["start"] - before["end"] < 30 else block["start"]
        windows.append((block["fan"], start, after["end"]))
    return windows


def cpu_parts(row):
    """(static W, dynamic P W, dynamic E W) of power_estimate's model, or None."""
    util, mhz = row.get("cluster_util_pct"), row.get("cpu_mhz") or []
    if not isinstance(util, dict) or len(mhz) != 20:
        return None
    static, dyn = MODEL.base_w, {"P": 0.0, "E": 0.0}
    for name, (idle_w, full_w, f_max) in MODEL.clusters.items():
        load = util.get(name)
        clocks = [mhz[POS[cpu]] for cpu in CLUSTER_CPUS[name] if mhz[POS[cpu]]]
        if not isinstance(load, (int, float)) or not clocks:
            return None
        ratio = min(1.0, max(0.0, sum(clocks) / len(clocks) / f_max))
        static += idle_w
        dyn[name[0]] += (full_w - idle_w) * min(1.0, max(0.0, load / 100)) * ratio ** MODEL.exponent
    return static, dyn["P"], dyn["E"]


def load(t0, t1, traces):
    rows, day = [], date.fromtimestamp(t0)
    while day <= date.fromtimestamp(t1):
        path = Path(traces) / f"{day:%Y%m%d}.jsonl"
        day += timedelta(days=1)
        if not path.exists():
            continue
        for line in open(path):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r["utc_ns"] / 1e9
            tgpu = (r.get("acpi_c") or {}).get("acpi_TGPU")
            parts = cpu_parts(r)
            if (not t0 <= t <= t1 or tgpu is None or r.get("gpu_w") is None
                    or r.get("fan_floor") is None or parts is None):
                continue
            rows.append((t, r["gpu_w"], *parts, r["fan_floor"], tgpu,
                         activity(r["gpu_w"], r.get("gpu_mhz"))))
    rows.sort()
    return rows


def simulate(p, rows, kP=1.0, kE=1.0):
    """Model - measured TGPU per row; starts in steady state, shifted to the first TGPU."""
    Cp, Cf, Gn, g0, g1, P_bg, r = p[:7]
    split = p[7] if len(p) > 7 else 0.0          # share of P_bg entering the fin block
    fins_w = split * P_bg
    def p_in(row):
        return row[1] + row[2] + kP * row[3] + kE * row[4] + (1 - split) * P_bg
    t0, pg, *_ , fan, tg, a = rows[0]
    ga = g0 + g1 * max(0.2, fan / 12)
    Tf = ROOM_C + (p_in(rows[0]) + fins_w) / ga
    Tp = Tf + p_in(rows[0]) / Gn
    shift = tg - (Tp + (r + R1 * a) * pg)
    Tp += shift; Tf += shift
    err, prev = [], t0
    for row in rows:
        t, pg, fan, tg, a = row[0], row[1], row[5], row[6], row[7]
        dt, prev = t - prev, t
        if dt > 30:
            dt = 0.0
        ga, pin = g0 + g1 * max(0.2, fan / 12), p_in(row)
        n = max(1, int(dt / 0.5))
        for _ in range(n):
            h = dt / n
            qn = Gn * (Tp - Tf)
            Tp += h * (pin - qn) / Cp
            Tf += h * (qn + fins_w - ga * (Tf - ROOM_C)) / Cf
        err.append(Tp + (r + R1 * a) * pg - tg)
    return err


def rms(errors):
    return (sum(e * e for e in errors) / len(errors)) ** 0.5 if errors else None


def unpack(x):
    return [exp(v) for v in x[:6]] + list(x[6:8])


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    opt = {k: argv[argv.index(k) + 1] for k in ("--out", "--traces") if k in argv}
    out_path, traces = opt.get("--out"), opt.get("--traces", "/var/lib/spark-energy/traces")
    found = blocks(*[a for a in argv if not a.startswith("--") and a not in opt.values()])
    windows = gpu_windows(found)
    data = {}
    for fan, t0, t1 in windows:
        data.setdefault(f"fan{fan}", []).append(load(t0, t1, traces))
    cpu = [b for b in found if b["label"] == "settle-cpu" or b["label"].startswith("after-")
           and b["label"][6:] in ("p-cores", "e-cores", "all-cores", "p-cores-2600")]
    if cpu and cpu[0]["label"] == "settle-cpu":
        data["cpu"] = [load(cpu[0]["start"], cpu[-1]["end"], traces)]
    train = [rows[::2] for k in ("fan12", "fan2") for rows in data.get(k, ())]
    if not train:
        raise SystemExit("no completed GPU blocks at fan 12 or fan 2")

    def cost(x):
        p = unpack(x)
        if not (0 <= p[6] <= 1.5 and 0 <= p[7] <= 1):
            return 1e9
        return rms([e for rows in train for e in simulate(p, rows)])

    current = [PARAMS.plate_j_k, PARAMS.fins_j_k, PARAMS.neck_w_k, PARAMS.air_base_w_k,
               PARAMS.air_fan_w_k, PARAMS.background_w, PARAMS.hotspot_k_w,
               getattr(PARAMS, "background_fins_share", 0.0)]
    best = None
    for start in (current[:7] + [0.5], [80.0, 600.0, 6.0, 2.5, 2.5, 15.0, 0.3, 0.3]):
        x0 = [log(v) for v in start[:6]] + start[6:8]
        x, v = nelder_mead(cost, x0, [0.3] * 6 + [0.1, 0.2], iters=2500)
        if best is None or v < best[1]:
            best = (x, v)
    p = unpack(best[0])

    def scores(params):
        res = {}
        for name in ("fan12", "fan2", "fan6"):
            if data.get(name):
                e = [v for rows in data[name] for v in simulate(params, rows)]
                res[name] = {"rms_k": round(rms(e), 2), "bias_k": round(st.mean(e), 2)}
        return res

    Cp, Cf, Gn, g0, g1, P_bg, r = p[:7]
    ga12, ga2 = g0 + g1, g0 + 0.2 * g1
    result = {
        "blocks": [{k: (round(v, 1) if isinstance(v, float) else v) for k, v in b.items()}
                   for b in found],
        "windows": [[fan, datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
                     datetime.fromtimestamp(t1).isoformat(timespec="seconds")]
                    for fan, t0, t1 in windows],
        "rows": {k: sum(len(rows) for rows in v) for k, v in data.items()},
        "fit": dict(zip(NAMES, (round(v, 3) for v in p))), "r1_fixed": R1,
        "train_rms_k": round(best[1], 2),
        "new_fit": scores(p), "current_params": scores(current),
        "plate_copper_g": round(Cp / 0.385), "fins_alu_g": round(Cf / 0.897),
        "tau_plate_s": round(Cp / Gn, 1), "tau_fins_fan12_s": round(Cf / ga12),
        "tau_fins_fan2_s": round(Cf / ga2), "g_air_fan12": round(ga12, 2), "g_air_fan2": round(ga2, 2),
        "plate_to_room_k_per_w_fan12": round(1 / Gn + 1 / ga12, 3),
        "plate_to_room_k_per_w_fan2": round(1 / Gn + 1 / ga2, 3),
        "current_plate_to_room_k_per_w_fan12": round(
            1 / PARAMS.neck_w_k + 1 / PARAMS.air_w_k(12), 3)}

    rows = data["cpu"][0] if data.get("cpu") else None
    if rows:
        split = next((b["end"] for b in found if b["label"] == "after-e-cores"), rows[-1][0])
        cpu_train = [i for i, row in enumerate(rows) if row[0] <= split]
        cpu_hold = [i for i, row in enumerate(rows) if row[0] > split]

        def cpu_cost(x):
            if not (0.05 <= x[0] <= 20 and 0.05 <= x[1] <= 20):
                return 1e9
            e = simulate(p, rows, *x)
            return rms([e[i] for i in cpu_train])

        k, v = nelder_mead(cpu_cost, [1.0, 1.0], [0.3, 0.3], iters=400)
        e_fit, e_one = simulate(p, rows, *k), simulate(p, rows)
        result["cpu"] = {
            "kP": round(k[0], 3), "kE": round(k[1], 3), "train_rms_k": round(v, 2),
            "holdout_rms_k": round(rms([e_fit[i] for i in cpu_hold]), 2) if cpu_hold else None,
            "holdout_bias_k": round(st.mean([e_fit[i] for i in cpu_hold]), 2) if cpu_hold else None,
            "unscaled_train_rms_k": round(rms([e_one[i] for i in cpu_train]), 2),
            "P_cluster_full_w": round(MODEL.clusters["P0"][0]
                                      + k[0] * (MODEL.clusters["P0"][1] - MODEL.clusters["P0"][0]), 2),
            "E_cluster_full_w": round(MODEL.clusters["E0"][0]
                                      + k[1] * (MODEL.clusters["E0"][1] - MODEL.clusters["E0"][0]), 2)}
    print(json.dumps(result, indent=1))
    if out_path:
        Path(out_path).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
