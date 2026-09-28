# Final qualification — goal v2 "Done when"

Written 27 September 2026, 05:52, by the root main agent under the session
claim, **before** the runs it declares (runs 9 and 10). Run 8 was started
before this text and is evaluated against the same criteria as supporting
evidence, marked as such.

## Common setup

- Installed `energy_control` (the only owner of GPU ceiling, CPU maxima and
  fan floor; legacy services masked), `/etc/spark-energy/config.json`
  production values unless stated: entry 1700 MHz, maximum 1800 MHz, targets
  CPU 90 °C / GPU 75 °C, fan minimum 2, preferred 6 (defaults).
- Load: `scripts/sustained_load.py` — 12 concurrent vLLM requests with unique
  prompts (≈ 20 000 prompt / 10 000 generated tokens, real prefill) plus
  50 % duty busy loops on P0 (CPUs 5–9). The load stops within ≈ 1 s when
  the service's readiness file vanishes (service abort or stop).
- Evidence: service trace (1 Hz, `/var/lib/spark-energy/traces`, with
  per-cluster utilisation and vLLM token counters), service journal, run
  logs `sustained-load-run<N>.log`.
- Steady segment: from 5 min after load start to load end.

## SQ — sustained typical load (runs 8 [supporting] and 9 [declared])

Duration 30 min, production config.

Pass criteria (all):

1. No guard or policy abort; the service stays active.
2. Steady segment: GPU ≤ 77 °C (target + 2) at every sample; hottest ACPI
   2 s trend ≤ 92 °C (target + 2) — raw samples may spike, the raw 93 °C
   abort is the hard limit.
3. GPU ceiling at the 1800 MHz maximum ≥ 80 % of the steady segment, or any
   derate explained by a thermal target (no GPU spill while the CPU is below
   its target, doc/42 defect 17).
4. Fan floor at or below the preferred state ≥ 80 % of the steady segment
   unless a target is exceeded.
5. Reported: aggregate generated tokens/s (vLLM counter), mean and max
   temperatures per zone, time share per mode.

## TH — target holding (run 10 [declared])

Duration 20 min, same load, **lowered test targets GPU 50 °C and CPU 72 °C**
(safer than production; abort limits unchanged), then production config
restored.

Pass criteria (all):

1. No guard or policy abort.
2. Steady segment: the controlled temperature (GPU sensor; hottest ACPI
   2 s trend) stays within target + 3 °C in ≥ 90 % of samples and never
   exceeds target + 6 °C.
3. The loops act on their own actuators first (CPU caps for the CPU target,
   GPU ceiling for the GPU target); no sustained oscillation of the GPU
   ceiling larger than 100 MHz peak-to-peak with a period below 30 s.
4. Reported: throughput cost against SQ (aggregate tok/s) and per-zone means.

## Results

(Filled in after each run.)

### Run 8 — SQ, supporting evidence (27 September 05:26–05:56)

`analysis/evaluate_qualification.py --kind SQ`: **pass** (all criteria).
No abort (journal clean); GPU max 58 °C / mean 52.1 °C; hottest ACPI raw
max 82.6 °C, 2 s trend max 81.5 °C / mean 71.9 °C; GPU at 1800 MHz 89.8 %
of the steady segment (RUN 1198 s, HOLD 103 s, RAMP 21 s, REARM 12 s — the
entry fallback on new prefills); fan floor ≤ preferred 6 in 87 %;
**aggregate 228 generated tokens/s** (12 jobs). Zone means: TSOC 71.9,
TS0P 71.3, TS1P 64.4, TUNC 65.3, TGPU 57.3, TS0E 55.2, TS1E 53.9 °C.
Harness note: the load script's 30 s client read timeout produced 23
client errors while 12 concurrent 20k-token prefills queued, and could
delay its stop by up to 30 s during a prefill wait. Fixed before run 9:
read timeout 600 s, and on stop the main thread shuts all request sockets
down (measured stop latency ≈ 0 s against a silent server).

### Run 9 — SQ, declared (27 September 05:57–06:26) — **FAIL**

Criterion 1 failed: the service aborted at 1 789 s (10 s before the planned
end) with "policy thermal interval invalid" (doc/42 defect 20); the load
stopped with it and the service restarted cleanly. All thermal and
operating criteria passed: GPU max 59 °C / mean 52.4 °C; hottest ACPI raw
max 83.2 °C, trend max 80.5 °C / mean 71.8 °C; GPU at 1800 MHz 96.8 %;
fan ≤ preferred 100 %; aggregate 193 tokens/s (vLLM slow-request variance,
doc/45). Not counted as a pass. After the fix a reviewed repeat (run 11)
uses the same declared criteria.

### Run 11 — SQ, reviewed repeat of run 9 after defect 20 (06:32–07:02) — **PASS**

All declared criteria passed. No abort (journal clean; the load ran the
full 1 800 s, 36 requests completed, 1 client error); GPU max 59 °C /
mean 52.5 °C; hottest ACPI raw max 82.7 °C, 2 s trend max 81.2 °C /
mean 72.0 °C; GPU at 1800 MHz 95.7 % of the steady segment (RUN 1 303 s,
RAMP 25 s, HOLD 18 s, REARM 15 s); fan ≤ preferred 6 in 93.5 %;
**aggregate 234 generated tokens/s**. Zone means: TSOC 72.0, TS0P 71.7,
TUNC 65.8, TS1P 63.4, TGPU 57.7, TS0E 55.4, TS1E 54.1 °C.

SQ summary: runs 8 and 11 pass with near-identical temperatures (GPU mean
52.1 / 52.5 °C, ACPI trend mean 71.9 / 72.0 °C) and throughput 228 / 234
tokens/s — reproducible. At the 1800 MHz hard limit the machine stays well
below the 75/90 °C targets under this typical load (the GPU cannot reach
75 °C at ≈ 30 W; firmware holds the P-clusters near 80 °C), so the targets
are not binding here; run 10 (TH) demonstrates holding lowered targets.

### Run 10 — TH, declared (07:03–07:23, targets GPU 50 / CPU 72 °C) — **FAIL**

Criteria 1–2 passed (no abort; GPU max 50.0 °C, never above target + 3;
ACPI trend max 65.7 °C), **criterion 3 failed**: the GPU ceiling cycled
625–1800 MHz (worst 30 s peak-to-peak 1 050 MHz, 771 of 783 windows
> 100 MHz). The loops over-relieved: GPU mean 45.4 °C (target 50), ACPI
trend mean 57.7 °C (target 72), fan 8–12; throughput 165 tokens/s (−29 %
against SQ). Cause (doc/42 defect 21): a load-start derivative kick plus
the entry/ramp limits drained the GPU PID's headroom integral through
anti-windup back-calculation; the P term alone then derated the GPU ~6 °C
below its target, and integer GPU-sensor steps kept kicking the derivative.
Production targets were restored after the run (07:25).

### Run 12 — TH, reviewed repeat after defect 21 (07:32–07:52, targets 50/72 °C) — **FAIL (criterion 3), much improved**

Criteria 1–2 passed: no abort; GPU mean 48.7 °C / max 53.0 °C (within
target + 3 in ≥ 90 %, never above + 6); ACPI trend mean 59.3 / max 64.4 °C.
Criterion 3 failed: GPU ceiling worst 30 s peak-to-peak 650 MHz, 536 of 785
windows > 100 MHz (run 10: 1 050 MHz, 771 of 783). The loop now regulates
the GPU *at* its target instead of ~5 °C below it; the ceiling dithers mainly
between 1675 and 1800 MHz (1800 MHz 47 % of the steady segment) with dips to
1150 MHz. Fan staging returned to the preferred 6–7 (run 10: 8–12);
throughput 203 tokens/s (run 10: 165; SQ: 234). Remaining dither: a 1 °C
integer GPU sensor against ≈ 2 W per 100 MHz. Production targets restored
07:53.

### Run 13 — TH, after defect 22 (07:58–08:18, targets 50/72 °C) — **FAIL (criterion 3)**

Criteria 1–2 passed (no abort; GPU mean 48.8 °C / max 55.0 °C, ACPI trend
mean 59.2 / max 63.6 °C); criterion 3 failed as in run 12 (worst 700 MHz,
507 of 788 windows > 100 MHz); fan ≤ preferred 95 %; 198 tokens/s.
Production targets restored 08:19.

**TH conclusion (not passed).** After defects 21–22 the loops hold lowered
targets without overshoot (GPU within +3 °C in ≥ 90 %, never +6 °C) and
without over-relief (GPU mean 48.7–48.8 °C at a 50 °C target, fans back
to the preferred state), at a throughput cost of ≈ 14 % against SQ. The
GPU ceiling, however, moves by more than the predeclared 100 MHz within
30 s in most windows: the loop answers request-mix power steps of several
watts seen through a 1 °C integer sensor. The criterion is kept as declared;
this is reported as a limitation. Production relevance is low: at the
production targets and the 1800 MHz limit the typical load never reaches
75/90 °C (SQ runs 8/11).

## Guard: live abort and recovery

Two unplanned live aborts during sustained load demonstrate the abort path
and log recovery end to end (service journal; service-run recorders under
`/var/lib/spark-energy/service-runs/`):

| Event | Trigger | Safe state | Load | Restart | Ready again |
| --- | --- | --- | --- | --- | --- |
| 05:18:35 | independent guard: projected breach TS1P (raw 79.9 °C, false positive, defect 19) | GPU/CPU/fan owners exit 0 (emergency lowest clocks, fan 12) | stopped via readiness file | systemd +16 s | 05:19:03 after the 10 s cool dwell (28 s), fan 12 |
| 06:26:48 | policy interval fault (defect 20) | same | same | systemd +15 s | 06:27:16 (28 s) |

Earlier the same evening: trial-level aborts (acquisition failures, defect
14; command budget, defect 17) each closed to the safe state and the trial
runner restarted the service. Both live defects were fixed; the hard 93/85 °C
limits were never approached (highest raw ACPI 83.2 °C, GPU 59 °C during
production-target load).

## Goal v2 "Done when" — status (27 September 2026, 08:25)

| Item | Status | Evidence |
| --- | --- | --- |
| Twin calibrated with uncertainty, holdout-validated | **Met** (with stated gaps) | doc/44 consolidated table: GPU/copper holdout 1.17 °C; CPU-cluster holdout E 1.8 / P 6.6 °C; throughput fit RMSE 1.8 tok/s; fan-vs-passive separation run in progress |
| Qualification: cold/idle → load at the qualified entry, ramp, no shutdown | **Met** | doc/43 series 3: 14 real-prefill cold starts 1200–1800 MHz, entry 1700 / max 1800 MHz; SQ runs ramp 1700 → 1800 under load |
| Qualification: sustained typical load holding 75/90 °C, throughput and stability measured | **Met for the typical load; targets not binding** | SQ runs 8 and 11 pass (reproducible; 228–234 tokens/s; GPU 52 °C, ACPI trend 72 °C). TH (lowered targets) runs 10/12/13 do not pass criterion 3 (ceiling dither) — reported limitation |
| Guard: abort and log recovery tested live | **Met** | two live aborts with safe state and 28 s recovery (section above) |
| Ownership: sole owner, legacy masked | **Met** | doc/42 (handoff 26 September), unchanged |
| Boot ordering in place | **Met (tested)** | warm reboot 11:08 and power cycle 11:24: vLLM started only after the entry-ceiling file of the current boot (gate held); two boot defects found and fixed (doc/42 23–24) |
| Rollback documented and tested | **Met (tested)** | 11:36–11:40: energy_control stopped → legacy units restored from the backup and started (GPU lock 250–500 MHz, fan floor 12, CPU guard active) → switched back (legacy stopped, files removed, masked; energy_control running, entry 1700 / max 1800); runbook step corrected (doc/41) |
| APIs and CLI working | **Met** | doc/47: status API live and enabled; operator CLI verified live 11:03–11:10 (four password-confirmed commits, all verified and audited) |
| Documentation, measured stability and limitations reported | **Met** | doc/38, 42–47 |

Remaining operator steps, in the operator's order: the cold-boot test and
the rollback test as the last items (password and CLI test done 11:03–11:10).

## Known machine state (27 September 2026, 09:05)

- `energy_control.service` active and enabled, sole owner of the GPU ceiling,
  CPU maxima and fan floor; legacy `spark-cpu-thermal-guard` and
  `dgx-fan-max` stopped, disabled and masked (unchanged since the handoff).
- `/etc/spark-energy/config.json` production values: entry 1700 MHz,
  maximum 1800 MHz, fan minimum 2 (preferred 6 by default), targets
  CPU 90 / GPU 75 °C (defaults); GPU PID kd 0.04, derivative filter 3 s.
- `spark-energy-api.service` active and enabled (127.0.0.1:18765, read-only).
- Operator broker dormant (no password provisioned).
- vLLM resident and untouched; no test load running.
- The agent keeps the hardware claim `/run/spark-energy/agent-command`
  until the operator's final steps (cold boot, rollback) are done or the
  operator releases it.

## Session end (27 September 2026, 11:15)

Before the operator's reboot: no agent background processes; energy_control
and spark-energy-api active and enabled; production configuration (entry
1700 / max 1800 MHz, targets 90/75 °C); operator CLI verified. The agent
released its hardware claim `/run/spark-energy/agent-command` (held since
26 September 16:23). Next session: create a new claim, then verify the cold
boot (energy_control running with the entry ceiling before vLLM started —
Spark_Dashboard gate, `journalctl -b`), then run the rollback test with the
operator (doc/41).


## Cold boot and rollback (27 September 2026, 11:08–11:40)

- **Warm reboot (11:08):** found defect 23 (CPU governor baseline missing
  at boot; the service reached its start limit; GPU held at the emergency
  lock, vLLM gated). Fixed and installed 11:25.
- **Power cycle (11:24):** the CPU baseline fix worked; the first start
  aborted on the pre-lock vendor clock (defect 24), the second start at
  11:25:56 wrote the entry-ceiling file, and the Spark_Dashboard gate
  started vLLM only after it (≈ 11:26). Settle fix installed 11:33
  (confirmed by unit tests; the next boot confirms it live).
- **Rollback (11:36–11:40):** see the table above; audit lines in
  `/var/lib/spark-energy/rollback-test.jsonl`. The runbook's unmask step
  was wrong (unit files had been moved to the backup) and is corrected.

## Goal v2 — final status

All "Done when" items are met, with two reported limitations: target-holding
at lowered targets dithers the GPU ceiling beyond the predeclared 100 MHz
(TH runs 10/12/13), and defect 24's fix awaits confirmation at the next
boot. Known state after the tests: energy_control and spark-energy-api
active and enabled with the production configuration; legacy units masked;
operator broker live for the operator; vLLM resident.


## Legacy decommissioning (27 September 2026, 11:52–12:00)

At the operator's request the self-built legacy services were removed from
the system after the rollback test: the CPU thermal guard program, the
`dgx-fan-control` helper, the guard's sudoers rule and the unit-file backup
(byte-identical copies in the Archive snapshot, record in
`Archive/decommissioned-2026-09-27.md`); the operator purged `nv-cpu-governor`.
The masks of all four known legacy writer units stay (required by the GPU
ownership check). The Spark Dashboard no longer shows the old guard's card or
autostart switch. Rollback is a reviewed manual reinstall (doc/41).


## GPU maximum 2000 MHz (27 September 2026, 12:45)

Operator decision after the goal: hard envelope 2200 MHz, production maximum
2000 MHz ("known as working"), entry ceiling 1700 MHz (qualified, doc/43).
Steps 2100/2200 toward the operator's end goal are to be qualified with the
boot-bound override (doc/42 item 26) before any production change.

## GPU maximum 2200 MHz (27 September 2026, 17:59)

Steps 2100 and 2200 MHz qualified with the boot-bound override (doc/52).
Production maximum 2200 MHz, qualified maximum 2200 MHz, hard limit
2500 MHz (operator release, at the operator's risk above the qualified value).
