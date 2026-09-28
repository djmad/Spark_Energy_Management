# GPU ladder 2100/2200 MHz and operator-settable maximum frequencies (27 September 2026, evening)

Operator, 27 September 2026:
- "starte die Läufe bis 2.2 GHz GPU" (start the runs up to 2.2 GHz GPU);
- "exponiere die max Frequenzen für GPU und alle CPU-Cluster am Dashboard,
  damit ich diese selbst einstellen kann im Betrieb später" (expose the
  maximum frequencies of the GPU and every CPU cluster on the dashboard, to
  set them in operation);
- "Freigabe für Endfrequenzen bis 2.5 GHz (ich weiß, dass es hier
  möglicherweise crashen wird, aber das ist mein Risiko)" (release for end
  frequencies up to 2.5 GHz; the operator knows it may crash and takes the
  risk).

Hardware claim: session 3dc0579e, from 17:24.

## GPU ladder

Method:
- Each step uses the boot-bound qualification override
  (`/run/spark-energy/qualification.json`, doc/42 item 26), so a maximum under
  test never survives a reboot.
- Service restart from idle, then 8 minutes of load: LLM with 20 jobs
  (about 4 000 words in, 3 000 tokens out), plus 20 `stress-ng --vecfp`
  workers from minute 3 to minute 8. This is the highest combined power this
  machine carries.
- Unchanged: the PSU entry logic (REARM to 1700 MHz on every new prefill,
  100 MHz/s ramp, 75 % ramp gate), the CPU entry clamp and the 93/85 °C
  aborts.

Pass criteria, declared before each run in
`/var/lib/spark-energy/llm-combined-runs.jsonl`:
1. No guard or policy abort, service active, boot ID unchanged (no PSU
   shutdown).
2. The GPU cap reaches the step maximum under LLM load, and the measured
   clock tracks it (a vendor limit is reported).
3. The GPU stays at or below 77 °C at every sample, with no ACPI trip.
4. Report throughput, GPU power and vendor flags.

Evaluation: `python3 -m analysis.gpu_ladder` (windows: LLM only 60–180 s,
combined 60–300 s after the CPU step).

### Results

| Step | Time | Verdict | LLM only tok/s | LLM + 20 workers tok/s | CPU bogo ops/s | GPU cap / measured, combined | Share at the step maximum | GPU W mean / max | GPU °C max | Hottest ACPI max / projection max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2000 (reference: mixed test 5, doc/51) | 17:05 | — | 443 | 365 | 31 011 | 1947 / 1909 | 74 % | 23.9 / 35.3 | 65 | 87.9 / 91.4 °C |
| **2100** | 17:25 | **pass** | 430 | 381 | 32 206 | 2034 / 2025 | 75 % | 26.1 / 42.8 | 66 | 87.6 / 91.9 °C |
| 2200 | 17:35 | **abort** (not GPU; below) | — | — | — | — | — | — | — | — |
| **2200, reviewed repeat** | 17:47 | **pass** | 460 | 355 | 29 601 | 2084 / 2060 | 67 % | 28.1 / 50.7 | 69 | 87.8 / 90.9 °C |

The time not at the step maximum is REARM (to 1700 MHz on each new prefill)
and the 100 MHz/s ramp back, both unchanged by operator decision. At a
steady cap the measured clock is 2093 MHz at 2100 and 2184–2190 MHz at 2200.

**Throughput.** Run-to-run spread (about ±5 %, from the request mix)
covers the clock effect in these 8-minute runs. The fitted model (doc/45:
1/rate = 27.0/gpu + 3.0/cpu + 0.0091 s per token) predicts about +5 %
decode and about +10 % prefill from 2000 to 2200 MHz. The cost is GPU power:
the peak rose from 35 to 51 W (+43 %), the mean from 24 to 28 W.

**First 2200 attempt: abort 17:38:57, independent guard, "CPU actuator
unhealthy"** (doc/42 defect 31).
- **When:** 61 s after the 20-worker step, while the GPU cycled
  REARM/RAMP 1700–2200.
- **What happened:** the guard found the CPU owner's evidence older than
  1.5 s.
- **Context:** the system was saturated. Trace rows came 1.0–1.4 s apart
  instead of 1 s. There was no slow CPU command (over 0.3 s) and no kernel
  message.
- **The GPU was healthy at 2200:** measured 2177–2190 MHz, at most 37 W and
  66 °C.
- **Safe state:** the owners reached it, and the load stopped through the
  readiness file.

Classification and response:
- It is the same class as defect 30, CPU evidence timing under saturation.
  It is not a GPU or PSU failure.
- systemd restarted the service before the agent removed the override. The
  agent then restarted it at the production 2000 MHz.
- The operator asked for a repeat: "ich würde den 2.2 GHz Test nochmal
  starten … trial and error in diesem Fall, sofern nichts Offensichtliches
  gefixt werden muss".
- Nothing obvious to fix was found. Only diagnostics were added: the owners
  log evidence gaps and slow readbacks, and the guard logs why owner
  evidence became invalid.
- The reviewed repeat passed. The new diagnostics logged no CPU evidence
  gap.

**Vendor watch during the repeat:** 12 flagged rows, all GPU, all in REARM
and ramp cycles.
- The measured clock lags a ramping cap by 1–3 s. The measurement refreshes
  more slowly than the cap moves.
- One stretch held 2093 MHz for 7 s at a 2200 cap before reaching 2190 MHz.
- At a steady cap there was no deficit.
- Fix: a deficit counts only after the cap has been steady for 3 s.

**Decision.** The qualified GPU maximum is now 2200 MHz
(`GPU_QUALIFIED_MAX_MHZ`). The production maximum was raised from 2000 to
2200 MHz, following the operator's end goal ("productive 2200 MHz") and
performance priority. The config backup is
`/etc/spark-energy/config.json.bak-*-max2000`. The operator can lower the
maximum live from the dashboard.

## Operator-settable maximum frequencies

- **Hard limit 2500 MHz** (`limits.py`, operator release). The independent
  guard and the GPU owner enforce this envelope: the durable service plan
  now carries the hard limit.
- **Configured GPU maximum** (`gpu_max_mhz`, 1700–2500 MHz). It is a live
  policy bound in both directions:
  - A raise ramps at 100 MHz/s under load.
  - A reduction applies at once. The policy tolerates the previous limit
    for at most 5 s until the owner has applied the lower one.
  - The entry ceiling (1700 MHz) and REARM are unchanged.
- **Per-cluster CPU maxima** (`cpu_e0_max_mhz`, `cpu_p0_max_mhz`,
  `cpu_e1_max_mhz`, `cpu_p1_max_mhz`; E 338–2808, P 1378–3900 MHz). They are
  live bounds of the cluster loops:
  - A raise ramps through the loop's controlled envelope.
  - A reduction applies at the next tick.
  - An operator cap on a P cluster is not a thermal derate, so it never
    drags its E neighbour down (fast-first coupling uses the fraction of the
    operator maximum).
  - The class maxima (`cpu_fast_max_mhz`, `cpu_slow_max_mhz`) stay restart
    fields and output limits.
- **Broker readback** checks each cluster's accepted maximum against its
  operator maximum.
- **Status and `/v1/limits`** publish:
  - `gpu_hard_max_mhz`, `gpu_qualified_max_mhz`;
  - `cpu_cluster_max_mhz`, `cpu_cluster_bounds_mhz`.
- **CLI:** the new fields are available as integers.
- **Dashboard** (Spark_Dashboard, backup `backups/20260927-172935`):
  - A second settings row: GPU max, P0, P1, E0 and E1 max (MHz). Bounds
    come from the service status.
  - The note names the qualified GPU maximum.
  - A proposal above it returns a warning ("not qualified, operator risk (a
    power-supply shutdown is possible)").
  - Every change still needs Propose → password → Apply.
- **Validation:**
  - 705 tests OK (new: `tests/test_operator_maxima.py` and the vendor-watch
    ramp test); headless scenario OK.
  - Dashboard: py_compile, script parse, `bash -n`, 17 tests passed,
    `/healthz` and `/api/services` 200.
  - Live proposals, none committed: P1 3500 MHz accepted without warning;
    GPU 2400 MHz accepted with the warning; GPU 2600 MHz and E0 3000 MHz
    refused.

## Known state (17:59)

- `energy_control` runs the final build (`/opt/spark-energy`, installed
  20260927T175652) with production maximum **2200 MHz**:
  - entry 1700 MHz, targets 90/75 °C;
  - hard limit 2500 MHz, qualified 2200 MHz;
  - CPU cluster maxima at hardware (2808/3900 MHz).
- `spark-energy-api` is on the same code. `spark-dashboard` was restarted
  and shows the new settings.
- There is no qualification override (`/run/spark-energy/qualification.json`
  removed).
- Idle: GPU cap 1700 MHz; CPU caps E 1600 / P 2675 MHz (idle envelope); fan
  floor 12 until the idle delay expires.
- The legacy units stay masked. vLLM remains resident and was never
  restarted. No test load is running. The hardware claim of session
  3dc0579e (17:24–18:00) is released.

## Open

- **Steps above 2200 MHz are not qualified.** The operator may set up to
  2500 MHz at the operator's own risk. On request an agent runs the ladder
  on (2300, 2400, 2500) with the same method.
- **CPU evidence timing under saturation** (defects 30 and 31) is not
  explained yet. The new diagnostics name the gap at the next occurrence.
