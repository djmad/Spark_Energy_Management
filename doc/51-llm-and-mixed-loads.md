# LLM, mixed and pure-CPU loads on thermal v3 (27 September 2026, afternoon)

Operator requests, in order:
- "continue with llm loads and combined loads";
- on the GPU ramp-downs: "interesting ramp down of gpu, track this";
- "the psu fail is bound to the wattage increase, not prefill …";
- "dont touch the PSU … we only change [the ramp gate] to a detection value of 75 %";
- "Fahre unattended fort, optimiere das System, dass unter gemixter Load,
  unter reiner CPU-Load und unter reiner LLM-Load immer die maximale
  Performance zur Verfügung steht" (continue unattended; always maximum
  performance under mixed, pure CPU and pure LLM load);
- "grundsätzlich immer ein kontrolliertes Umfeld der Megahertz … unter Load
  hochschalten … auch die CPU jederzeit limitiert, so wie das LLM" (always
  a controlled clock envelope, ramping up only under detected load, with the
  CPU as limited as the GPU).

Load:
- LLM: `scripts/sustained_load.py`, 20 concurrent unique requests
  (vLLM `max-num-seqs` 20), about 4 000 words in and 3 000 tokens out.
- CPU: `scripts/cpu_load.py` (`stress-ng --vecfp`).
- Logs: `/var/lib/spark-energy/llm-combined-runs.jsonl` (plans, aborts,
  fixes), `cpu-load-runs.jsonl`, and the 1 Hz service trace.

## Changes

| # | Change | Why |
| --- | --- | --- |
| 1 | **GPU ramp gate 95 % → 75 %** (`gpu_busy_threshold`, operator) | LLM decode with 20 jobs runs at 82–96 % GPU utilisation, so the cap sat at the 1700 MHz entry ceiling (HOLD) most of the time |
| 2 | PSU entry logic **unchanged** (operator: "dont touch the PSU") | REARM to 1700 MHz on every new prefill, entry ceiling, 100 MHz/s ramp |
| 3 | CPU integral winds down 3× faster above target (`Gains.wind_down_factor`) | abort 15:43: a cluster sat 2.3 °C over its setpoint for tens of seconds |
| 4 | Effective CPU ceiling 87 → **86 °C** (trend margin 4 °C) | same abort |
| 5 | **Controlled clock envelope for the CPU** (operator concept) | abort 15:59 and the 15:58:55 step, detailed below |
| 6 | **Guard grace 0.6 s at 5–10 °C margin** (defect 29) | abort 16:42: one late host frame at the 86 °C operating point |
| 7 | **CPU command shaping and consistent budgets** (defect 30): 25 MHz steps, raises only past 50 MHz, reductions immediate, class maximum exact; owner apply wait 0.5 → 1.0 s; guard CPU evidence window 0.5 → 1.5 s; the owner logs commands slower than 0.3 s | abort 16:59: a verified CPU command under a sudden 20-core step outlived the guard's 0.5 s evidence window (cppc applies new maxima through late kernel work; per-tick commands kept the owner busy) |

**Controlled clock envelope (change 5).** A P cluster that is not fully
loaded (utilisation under 90 %) waits at a *base cap*: the cap it has
learned to hold under full load at its setpoint (EMA 30 s), minus about
100 MHz. Until it has learned, the base is about 3.3 GHz. On load
detection (≥ 90 %) it ramps in a controlled way (tapered, at most
~75 MHz/s). E clusters start unconstrained. The reason: when LLM + 10
workers started at 15:58:55, idle clusters at full clock jumped
+25–28 °C within 1 s (TS1P 51.9 → 79.8 °C). At 15:59:55 a partly loaded P1
at full clock took a migrating worker, rose 4.7 °C/s for 1 s, and the guard
aborted. Twin (migrating load, 1 s lag): TS1P max 91.0 → 84.7 °C and
throughput 0.596 → 0.609.

**Guard grace (change 6).** One slow host frame reset the guard's 2 s slope
window. At under 10 °C margin the guard had allowed no gap at all (three
0.1 s checks). The new grace is bounded: blind for at most 1.6 s, at the
fastest regulated rise (~2 °C/s) at most ~3.2 °C, inside the 5 °C margin.
There is still no grace within 5 °C of an abort, and the limits
themselves are unchanged. The sampler now logs any acquisition slower than
0.5 s with per-part timings. At the service's nice −10 under full load:
`nvidia-smi` p95 84 ms, a full read at most 105 ms, so the slow frames are
rare outliers.

## Aborts during the block (all preserved in the run log)

| Time | By | Cause | Fix |
| --- | --- | --- | --- |
| 15:43:07 | policy (guard mirror) | LLM + 10 workers: TS0P 2.3 °C over its cluster setpoint, then a jump held the projection over 93 °C for 1 s | changes 3 and 4 |
| 15:59:55 | guard (confirmed projection) | partly loaded P1 at full clock took a migrating worker: +4.7 °C/s for 1 s | change 5 |
| 16:42:46 | guard (acquisition) | one late host frame at 85 °C, no grace near the limits | change 6 |
| 16:59:05 | guard (CPU actuator) | CPU command under the 20-worker step outlived the 0.5 s evidence window | change 7 |

After each abort the owners reached their safe state, and systemd restarted
the service after cool-down, as designed. No step was repeated before its
fix.

## Results (time-matched windows)

| Load | Controller | LLM tok/s | CPU bogo ops/s | GPU cap / measured (s below 2000) | CPU measured E0 / P0 / E1 / P1 | Hottest mean / max | Projection max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LLM only | 95 % gate | 407 | — | 1886 / 1866 (129 of 218 s) | — | 57.8 / 74.2 °C | 76 °C |
| LLM only | 75 % gate | 471 (+16 %) | — | 1955 / 1921 (43 of 210 s) | — | 58.7 / 71.9 °C | 73 °C |
| LLM only | 75 % gate + envelope | 460 | — | 1953 / 1933 | P at base cap | 56.7 / 64.1 °C | 67 °C |
| LLM + 10 workers | envelope | 375 | 21 869 | 1948 / 1913 | 1323 / 3394 / 745 / 3310 | 84.6 / 87.4 °C | 90.4 °C |
| 20 workers | envelope + defect 29 | — | 35 560 | — | full-clock E, P ≈ 3.6 GHz | ~86 °C | 91.4 °C (onset) |
| LLM + 20 workers | envelope + defects 29/30 (command shaping) | 365 | 31 011 | 1947 / 1909 (55 of 211 s) | 2796 / 3373 / 2775 / 3278 | 84.9 / 87.9 °C | 91.4 °C |

Requested versus measured clocks are 1:1 under load throughout. The vendor
watch fired once, for E0 at 15:59:22 (about 95 % busy, measured about
680 MHz under a 2676 MHz cap). That is a real firmware clamp (G6), not
seen again since.

## Observations and recommendations

- **LLM under CPU contention (fairness).** With 10 stress-ng workers the
  LLM drops from about 460 to 375 tokens/s at an unchanged GPU clock. The
  workers saturate both P clusters and vLLM's CPU threads compete with them
  in the scheduler. The thermal controller cannot change that.
  Recommendation: run batch CPU work at lower priority (`nice 10`, or
  systemd `CPUWeight=` for the batch services), leaving vLLM untouched.
- **REARM.** With 20 jobs the GPU still re-arms to 1700 MHz about 3–7 times
  per minute. That is kept by operator decision; with the 75 % gate the cap
  recovers within about 3 s.
- **Mixed-load cost.** LLM + 20 workers: the LLM keeps 82 % of its solo
  rate (444 → 365 tokens/s) and the CPU keeps 87 % of its solo throughput
  (35 560 → 31 011 bogo ops/s). The GPU clock is unchanged. Contention
  is in the scheduler and, for the CPU, in the extra copper heat.
- **CPU entry clamp flapping.** Under LLM load the aggregate CPU demand
  signal crosses its 10 % threshold repeatedly and re-arms the CPU entry
  clamp (P 2639 MHz for a few seconds). It belongs to the PSU entry logic
  and was left unchanged.

## Result

All three load classes now run at their best measured performance with the
guard untouched in its limits:

| Load | Before (goal v2 controller) | Now |
| --- | --- | --- |
| Pure CPU (20 × vecfp) | 33 067 bogo ops/s, P 3.3 GHz at 77 °C | 35 560–35 750 (+8 %), P ≈ 3.6 GHz at ~86 °C |
| Pure LLM (20 jobs) | 407 tokens/s, GPU below 2000 MHz 59 % of the time | 444–471 (+9–16 %), below 2000 MHz ~18 % (REARM only) |
| LLM + 10 workers | aborted twice before the fixes | 375 tokens/s + 21 869 ops/s, no event |
| LLM + 20 workers | aborted before the fixes | 365 tokens/s + 31 011 ops/s, no event |

Validation of the final build: 691 tests OK, headless queue scenario OK.
These are fake-actuator and simulation results; the hardware evidence is
the live runs above.

## Known state after the block (17:18)

- `energy_control` runs the final build (`/opt/spark-energy`, installed
  20260927T170418, active since 17:05:31, no restarts).
  `spark-energy-api` is on the same code. The previous builds are kept at
  `/opt/spark-energy.prev-20260927T164849` and `…T170418`.
- `/etc/spark-energy/config.json` changed only in `gpu_busy_threshold`
  (0.75, operator). Otherwise: GPU maximum 2000 MHz, entry 1700 MHz,
  targets 90/75 °C; the other v3 fields are at their defaults.
- Idle after the block: GPU cap 1700 MHz (entry ceiling), CPU cluster caps
  E0/E1 1650 MHz and P0/P1 2725 MHz (idle envelope), fan floor 12 until the
  300 s idle delay expires. Effective CPU setpoint 86 °C on all clusters.
- `spark-cpu-thermal-guard` and `dgx-fan-max` remain masked. vLLM remains
  resident and was never restarted.
- No test load is running. The hardware claim of session 3dc0579e
  (15:37–17:18) is released.

## Open for the operator

- **Guard rule for 90/93 °C.** The practical CPU operating point is 86 °C,
  set by the guard's immediate projection rule (trend ≥ 90 °C) and not by
  the 90 °C target. Running closer to 90 °C, and later 93 °C ("93 when we
  are proven"), needs a guard rule change and, for 93 °C, a higher abort.
- **GPU ladder 2100 / 2200 MHz.** This needs a qualification run at each
  step (hard maximum 2200 MHz).
- **Batch CPU priority.** `nice` or `CPUWeight=` for batch services;
  vLLM stays untouched.
- **First dashboard Apply.** This needs the operator's password.
