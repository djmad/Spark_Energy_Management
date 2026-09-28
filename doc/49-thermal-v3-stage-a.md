# Thermal control v3, Stage A: live A/B (27 September 2026)

Plan: doc/48 §0 (operator decisions D1–D7). Defects: doc/42, 27 and 28.
Hardware claim held by session 3dc0579e, 14:09–14:45.

## What Stage A changes

- **CPU PID integrator** (D2, defect 27): the retired guard's conditional
  integrator. It integrates on the PID's own output and never tracks
  downstream limits, so the proportional-only plateau about 9 °C below
  target is gone.
- **Fan** (D1): floor 12 whenever there is load (LLM work, busy GPU or CPU
  demand); the minimum only after 300 s idle.
- **Guard-aware CPU setpoint** (D3, defect 28):
  - The effective setpoint never enters the guard's no-confirmation zone
    (trend ≥ 90 °C). Its ceiling is 93 − 3 − 3 = **87 °C**; the configured
    90 °C target is the cap above that.
  - It backs off at 1 °C/s while the guard-formula projection is within
    2.5 °C of 93, and recovers at 0.005 °C/s.
  - The policy's own predicted-breach abort uses the guard's quantities.
- **Cap recovery taper** (A2): the upward rate scales with the projection's
  headroom below the setpoint. It is full beyond 8 °C and 10 % at the
  setpoint.
- **Last-resort derate band** (A2): 1.0 °C, was 2.5 °C.
- **Operator surfaces**:
  - config and CLI fields `fan_policy`, `fan_load_state`, `fan_idle_delay_s`,
    `guard_margin_c` and `pid_integrator`;
  - the qualified maximum CPU target of 90 °C (`limits.py`);
  - status, trace and `/v1/limits` expose the effective setpoint, the
    integrals, and the last second's worst projection and largest
    raw-over-trend step.
- **Tests**: 661 OK. Headless scenario OK.

## Live A/B: 20 × `stress-ng --vecfp`, GPU idle (vLLM resident)

`scripts/cpu_load.py` runs the load; the log is
`/var/lib/spark-energy/cpu-load-runs.jsonl`. Trace statistics use the
window 60–300 s after load start.

| Run | Time | bogo ops/s | Hottest mean / p95 / max | Fast cap mean / s.d. / min | P / E measured (**wrong index order; see doc/50**) | Guard-formula projection p95 / max |
| --- | --- | --- | --- | --- | --- | --- |
| Old controller, fan staging 6 | 14:09:46 (480 s) | 33 067 | 77.3 / 79.4 / 80.1 °C | 3301 / 89 / 3048 MHz | 3196 / 2883 MHz | — |
| Stage A1 | 14:19:58 (300 s) | 32 358 (−2 %) | 79.8 / 85.3 / 87.3 °C | 3496 / 280 / 2115 MHz | 3204 / 2737 MHz | 90.0 / 92.1 °C |
| **Stage A2** | 14:36:04 (300 s) | **35 367 (+7.0 %)** | 84.4 / 87.0 / 88.5 °C | 3607 / 136 / 2639 MHz | **3411 / 2915 MHz** | 89.0 / 94.1 °C |

### Stage A1 failed (zigzag, operator: "pretty hard ZIGZAG in ramp up")

1. The caps ramped at the full recovery rate (≈ 75 MHz/s) into the limit.
2. This machine's fast thermals then heated the P zones by 2–3 °C/s, so the
   2 s projection ran 4–6 °C ahead of the trend.
3. The 2.5 °C derate band chopped the caps by about 1 GHz, and the cycle
   repeated every 15–30 s.

The PID integral stayed at about 1 throughout, so the PID never regulated.
Once, the worst-zone projection reached 93.9 °C for under 1 s with the trend
at about 81 °C, so no guard trip. Production was rolled back at 14:26 (A1
kept at `/opt/spark-energy.stageA1-20260927T142626`).

### Stage A2 fixes

- the recovery taper;
- the 1 °C derate band;
- the 87 °C setpoint ceiling.

It was installed at 14:34.

**Result:** calm regulation just under the setpoint, measured P clock
3301 → 3620 MHz (+9.7 %; corrected in doc/50), stress-ng throughput +7.0 %. One real load step in
vecfp's method cycle (14:40:05) produced a projection of 94.1 °C within a
second, with the hottest zone at 88.5 °C. The policy cut the caps at once.
The guard did not trip: the trend was below 90 °C and the breach did not
persist for 1 s.

Measured raw-over-trend steps (4 Hz, A2 window): p95 0.81 °C, max 3.05 °C.
These are the first real sub-second ripple statistics.

## Ripple twin (`simulation/cluster_twin.py`), offline evidence

Model:
- zone gains calibrated to the operator's 20-worker plateau and to the
  retired guard's 91 °C point;
- P lag 2 s, E lag 1.5 s, as observed live (the operator: "not much heat
  capacity on the cooler");
- 4 Hz AR(1) ripple plus rare 1.5–3 °C steps;
- a guard with its own independent sampler.

Runs: 6 × 30 min per load. Throughput is Σ utilisation × f/f_max.

| Load | Controller | Guard trips | Throughput |
| --- | --- | --- | --- |
| Heavy (operator's 20 workers) | old | 0 | 0.811 |
| Heavy | A2, ceiling 87 °C | **0** | **0.934 (+15 %)** |
| Heavy | A2, ceiling 88.5 °C | onset trip | — |
| Heavy | A2, ceiling 87.5 °C | 1 | — |
| Light (vecfp-like) | old | 0 | 0.895 |
| Light (vecfp-like) | A2 | 0 | 0.980 (+9.5 %) |

Throughput is flat within ±0.3 % between ceilings of 86.5 and 87.5 °C.

With the unchanged guard, the practical CPU operating point is therefore
about 87 °C. Running closer to 90 °C would need the guard to ignore
single-sample steps, for example requiring two consecutive samples for the
immediate path. That is a safety-rule change for the operator to decide.

## Known state after the block (14:45)

- `energy_control` runs Stage A2 (`/opt/spark-energy`, installed
  20260927T143436), with `spark-energy-api` restarted on the same code.
- `/etc/spark-energy/config.json` is unchanged: GPU maximum 2000 MHz, entry
  1700 MHz, targets 90/75 °C, fan minimum 2, preferred 6. The new v3 fields
  are at their defaults (fan policy "load", conditional integrator, guard
  margin 2.5).
- Owners: GPU entry/ramp to 2000 MHz under load, CPU caps from the policy,
  fan floor 12 under load.
- No test load is running. The hardware claim is released.
