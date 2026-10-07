# 59 — Twin fan: fan level against the new cooler's twin (6 October 2026)

Operator, 6 October 2026:

> die Lüftergeschwindigkeit annähernd die gesamte Zeit auf 100% läuft. Ab und
> zu gibt es von den Lüftern eine Abstufung, die relativ bald wieder behoben
> wird … ein besseres Einschwingverhalten … damit wir die Temperaturen konstant
> halten … Die Lüfter haben natürlich auch eine mechanische Abnutzung …
> Sweet Spot wären 70 Grad

Unattended go the same evening ("Du kannst hier unattended fortfahren … Bitte
mach dir vorher ein Backup"). vLLM stays resident and was not touched.

## Finding (Spark_Telemetry, 5 Oct 22:00 – 6 Oct 18:20, 1-minute data)

| Quantity | Value |
| --- | --- |
| Fan floor at 12 | 85 % of the time, mean 11.7 |
| Step-downs | 37, mostly to 6–10, back at 12 after 1–12 min |
| GPU power under load | mean 29 W, 1-minute maxima up to 83 W |
| Fan "expected_w" (peak-hold) | mean 54 W |
| TGPU | mean 47 °C, max 56 °C; nvidia max 51 °C |
| Fan feedback term | never active; GPU cap 2500 MHz throughout |

The clock loops never acted; the sawtooth was the predictive fan alone:

1. Its feed-forward still used the first cooler fit (27 September: neck
   3.45 W/K, air 1.92 + 1.50 × share, background 12.3 W, plate target 55 °C;
   doc/58 "Not changed"). On that model any expected power above about 46 W
   needs fan 12, and no level fits beyond it, so the fallback is 12.
2. The expected power is a 300 s peak-hold of the measured power; LLM bursts
   every 1–5 min kept it at about 54 W while the mean was 29 W.
3. Up at once to the feed level (in practice 12), down one level per 60 s:
   each burst that crossed 46 W undid the minutes of release.

On the new cooler (doc/58: neck 4.13 W/K, air 2.05 + 3.59 × share,
background 24.8 W), the 1-minute mean power of the same period needs fan 2 at
most for TGPU ≤ 68 °C; the twin's steady TGPU at the actual floor read 0.8 K
below the measured one on average.

## Change: fan_policy "twin"

`simulation/model.py` `Supervisor._twin_fan`, selected by
`fan_policy: "twin"`. The "predictive" and "load" policies are unchanged and
stay selectable (rollback without code).

- **Online twin.** The two stores of the new cooler run inside the policy from
  the measured GPU power, the CPU estimate and the actual floor.
- **Bias.** Measured TGPU minus the twin's TGPU, smoothed over
  `fan_twin_bias_tau_s` (600 s), clamped to ±`fan_twin_bias_max_c` (8 K).
  It absorbs the room temperature (no sensor) and model error.
- **Smoothed power.** Rises with `fan_twin_rise_s` (45 s, about the fin
  block's time constant), falls with `fan_twin_fall_s` (120 s). A 30 s LLM
  burst counts at about half its height; sustained load counts fully within
  1–2 minutes.
- **Level.** The lowest level ≥ `fan_min_state` whose steady TGPU (room +
  power × (1/neck + 1/air(level)) + hotspot × GPU power + bias) stays at or
  below `fan_temp_target_c` (70 °C).
- **Up** at once, but only to that level (not to 12).
- **Down** one level per `fan_release_step_s` (60 s), and only while the next
  lower level's steady TGPU stays `fan_twin_down_margin_c` (3 K) below the
  target. This hysteresis stops the bouncing.
- **Feedback.** Any loop's guard projection within `fan_twin_fb_band_c` (6 K)
  of its setpoint adds fan, up to 12 at `fan_fb_full_c` (3 K) below it. For
  the GPU this starts at nvidia 72 °C (target 78 °C), and at TGPU 80 °C
  (zone setpoint 86 °C).
- **Near an abort:** 12, as before.

Unchanged: the clock loops, GPU target 78 °C, CPU target 92 °C, the aborts
(GPU 85 °C, ACPI 96 °C), the guard and the entry ceiling. The firmware's own
fan curve may always cool more.

All twin parameters are live tunables (`energy_control/broker.py`
`TUNABLES`).

Status: `control.fan` reports `steady_tgpu_c`, `target_c`, `bias_c` and
`twin_plate_c`; Spark_Telemetry stores them as `energy.control.fan.*`.

## Simulation (synthetic, not hardware evidence)

`python3 -m simulation.fan_policy_compare --replay <telemetry.json>`. The
Supervisor runs closed-loop against the doc/58 cooler. Replay: 6 h of
measured 5 s GPU and CPU power (6 Oct 12:20–18:20).

| Workload | Policy | Fan mean | At 12 | Changes/h | TGPU mean / p95 / max |
| --- | --- | --- | --- | --- | --- |
| Replay 6 h LLM | predictive, floor 6 (today) | 11.3 | 79 % | 17.5 | 47.8 / 54.4 / 59.8 |
| Replay 6 h LLM | **twin, floor 2** | **2.2** | 0.3 % | **1.7** | 57.9 / 66.8 / 69.9 |
| Replay 6 h LLM | twin, floor 4 | 4.1 | 0.3 % | 1.3 | 54.9 / 63.2 / 66.2 |
| Burn-in 2.5 GHz | predictive | 9.3 | 43 % | 19.5 | max 82.5 |
| Burn-in 2.5 GHz | twin, floor 2 | 7.3 | 30 % | 36 | max 82.5 |
| Burn-in + all CPU | predictive | 10.5 | 58 % | 24 | max 83.3 |
| Burn-in + all CPU | twin, floor 2 | 8.7 | 42 % | 32 | max 83.3 |

Notes on the table:

- The simulated "today" reproduces the measured fan: 79 % at 12, against 85 %
  measured.
- Under the burn-in both policies reach fan 12 and the same peak. The
  simulated burn-in runs warmer than measured (72.8 °C at fan 12 in doc/58),
  because of its ±12 W wobble and the hotspot term.
- With the twin, the extra fan changes in the burn-in are the gentle release
  after the load: one level per minute, 12 → 2.

Tests: `tests/test_twin_fan.py`, 11 tests. They cover:

- the release from 12 to the floor;
- bursts that do not bounce the fan;
- an intermediate level for moderate load and 12 for the burn-in;
- hysteresis at the target;
- bias learning;
- feedback, near-abort and the floor;
- validation and the broker's tunables.

Full suite: 775 tests, the same 10 failures and 45 errors as the unchanged
source as non-root (environment).

## Deployment

The agent had no root, so it prepared everything and did not deploy.

- Backup before the change: `/home/djmad/Backups/spark-energy-20261006T1829/`
  (source tree, installed `/opt/spark-energy` of build 20261002T011523, and
  `/etc/spark-energy/config.json`).
- `sudo bash scripts/deploy-twin-fan.sh`, in order:
  1. Refuses while an agent claim exists.
  2. Backs up the config to `config.json.bak-<time>-pre-twin`.
  3. Installs the code with the standard installer (test suite, keeps
     `/opt/spark-energy.prev-*`).
  4. Sets `fan_policy` "twin" and `fan_min_state` 2.
  5. Restarts `energy_control` and `spark-energy-api`.
  6. Waits for a fresh status that reports the twin fan; rolls back on its
     own if none arrives within 120 s.
- vLLM keeps running. The GPU cap starts at the 1700 MHz entry ceiling and
  ramps back (about 30 s on 6 October 11:39).
- Rollback:
  - `sudo bash scripts/rollback-twin-fan.sh` restores the configuration first,
    then the previous code.
  - `--config` restores only the configuration ("predictive", floor 6) and
    keeps the new code.

Deployed by the operator on 7 October 2026 (build 20261007T092306). First
status at idle: fan floor 2 (3690/4185 rpm, before 7560 at floor 6), TGPU
43.7 °C, twin bias +1.7 K.

## Evaluation (operator, after a day of data)

In the Spark Dashboard Usage and Cooling charts:

- **Fan floor and RPM:** expected about 2–4 under LLM load, against 12 before.
- **TGPU:** expected 55–68 °C, steadier, with fewer swings.
- **energy.control.fan.steady_tgpu_c against the measured TGPU:** the bias
  should stay within a few K.
- **GPU cap:** stays at 2500 MHz; no DERATED under LLM load.

If TGPU swings or the cap derates, roll back as above, or raise
`fan_min_state` to 4 (simulated mean 4.1, TGPU max 66 °C) live through the
dashboard's settings.

## Open

- Run a live burn-in block (at most 8 GB, beside vLLM, operator go) to check
  the high-power path near 70–78 °C.
- Fit the new cooler's CPU term (doc/58 open item). The twin fan sees CPU
  heat only through the calorimetric-v2 estimate and the bias.
