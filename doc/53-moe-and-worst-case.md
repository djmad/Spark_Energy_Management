# Domain-diverse LLM load and the worst-case burn-in (27 September 2026, evening)

Hardware claim: session 3dc0579e, from 18:04. Production: GPU maximum
2200 MHz, entry 1700 MHz, targets 90/75 °C (doc/52).

## 1. Domain-diverse LLM plus CPU ("MoE") test

Operator: "versuche verschiedene MoEs zu triggern, Biologie, Mathematik,
Chemie, Sprache, Geschichte … 20 LLM Prompts" and "20-fach LLM mit
verschiedenen MoEs + CPU".

Prompts (`scripts/moe_prompts.py`):
- 20 authored long-form tasks, one per domain: biology, mathematics
  (proofs), chemistry (mechanisms, synthesis), physics (quantum mechanics),
  history (Habsburg monarchy), comparative linguistics, French literature,
  Japanese culture, Python algorithms, Rust/C lock-free code, SQL, medicine,
  law (GDPR/AI Act), finance (DCF), music theory, philosophy, astronomy,
  geology, poetry, and one multilingual prompt (Chinese, Russian, Arabic).
- Eight languages in all.
- Each job cycles through the domains, so the 20 concurrent streams stay on
  different domains.
- Settings: `ignore_eos`, 3 000 tokens, temperature 0.7.

What is measured: vLLM does not expose the expert routing without changing
the engine, and the engine stays untouched. The test therefore measures the
effects: throughput per domain, GPU power, clocks and temperatures.

Load (`scripts/sustained_load.py --prompt-set moe`): 20 jobs for 8 minutes,
plus 20 `stress-ng --vecfp` workers from minute 3 to minute 8. This is the
same structure as the ladder runs, so the reference is the 2200 MHz repeat
with filler prompts (doc/52).

### Result (18:05:26–18:13:27, no abort, 53 requests, 0 errors)

| Load | Prompts | LLM tok/s | CPU bogo ops/s | GPU cap / measured | GPU W mean / max | GPU °C max | Hottest ACPI max / projection max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LLM only | filler (doc/52, 2200 repeat) | 460 | — | 2109 / 2093 | 25.9 / 40.6 | 58 | 65.3 / 66.0 °C |
| LLM only | **20 domains** | **393** | — | 2084 / 2072 | 23.3 / 28.3 | 52 | 60.3 / 60.9 °C |
| LLM + 20 workers | filler | 355 | 29 601 | 2084 / 2060 | 28.1 / 50.7 | 69 | 87.8 / 90.9 °C |
| LLM + 20 workers | **20 domains** | **378** | **32 107** | 2110 / 2086 | 25.5 / 31.1 | 62 | 87.5 / 91.2 °C |

Decode rate per request, by domain:
- about 24–25 tokens/s: mathematics, physics, Python;
- about 22: chemistry, finance, SQL, Rust, astronomy;
- about 18–20: music, geology, law;
- about 16–17: French, Japanese, poetry, history, multilingual, medicine,
  linguistics, philosophy, biology.

Reading:
- **Speculative decoding explains most of the domain spread.** vLLM runs
  it with four draft tokens per step; the cumulative acceptance is 57 %
  (`vllm:spec_decode_*`). Predictable output (code, formulas) is accepted
  more often than free prose in German, French or Japanese, so it decodes
  faster per step. The filler prompts (word lists under `ignore_eos`)
  decode fastest of all. Acceptance per domain was not measured. How much
  of the LLM-only difference comes from expert diversity cannot be told
  apart from acceptance without instrumenting the engine.
- **Power: the realistic prompts are gentler.** The domain prompts are
  short (a real prefill, but small), so the GPU peaks are 28–31 W instead
  of 41–51 W with the 4 000-word filler prefills. The filler test is the
  harder PSU load, and REARM has less to catch.
- **Combined load:** LLM and CPU throughput are both a little higher than
  with filler, with the same thermal picture (hottest zone 87.5 °C, no
  events). The CPU envelope and the GPU at 2200 MHz behave the same for
  realistic content.

## 2. Worst-case burn-in

Operator:
- "anschließend werden wir das LLM stoppen und gpuburnin mit cpuburnin
  fahren … unser Endgegner";
- "verwende ausschließlich den burnin wie er ist, keine Änderung der
  Parameter";
- "gpuburnin + cpu, beide starten gleichzeitig ohne versetzen";
- "wir haben 30 crashes in dieser Woche gehabt, einer mehr oder weniger ist
  hier nicht relevant. Es muss der Worst case getestet werden".

Method (`scripts/burnin_block.sh`):
- vLLM is stopped first, through the Spark_Dashboard (the burn-in never runs
  alongside the LLM).
- The operator's unchanged scripts start at the same moment with their
  defaults (no arguments):
  - `~/Documents/burnin.py`: about 100 GB, 16384² bf16 matmul,
    endless, consistency check every 200 iterations;
  - `~/Documents/cpu-burn-10.py`: 20 integer workers.
- Both scripts' SHA-256 are logged.
- Only start and stop are controlled from outside: SIGINT/SIGTERM after
  600 s, or at once when energy_control's readiness file vanishes.
- A black box writes energy_control's status every second with fsync
  (`/var/lib/spark-energy/burnin1-blackbox.jsonl`).
- energy_control keeps control throughout: entry 1700 MHz, ramp to
  2200 MHz, 75 °C GPU target, 85 °C GPU and 93 °C ACPI aborts.
- After the block, vLLM is started again through the dashboard. If the
  machine went dark, it autostarts after the boot's entry ceiling.

### Burn-in 1 (18:15:15): abort after 24 s, no crash

Timeline:
- vLLM was stopped through the dashboard at 18:14:30. Docker force-killed
  the container after 10 s. During that CUDA teardown energy_control aborted
  (defect 33, below), restarted and was ready again at 18:14:59.
- Both burn-ins started at 18:15:15. The GPU script allocated 99.9 GB in
  6.8 s. The GPU ramped 1700 → 2200 MHz at 100 MHz/s (18:15:25–30), then
  drew 66 W.
- **18:15:39: the policy and the independent guard aborted** on the ACPI
  GPU zone: TGPU raw 87.8 °C, trend 88.3 °C, rising 2.9 °C/s, projection
  over 93 °C. The nvidia sensor was only at 68–72 °C.
- Both loads stopped through the readiness file (exit 0, 0 compute errors).
  The GPU script's queued matmuls finished at the emergency clock until
  18:15:57.

Cause, **defect 34 (doc/42):**
- The GPU loop regulated only on the nvidia sensor (75 °C target).
- TGPU runs about 0.21 °C/W above that sensor under LLM load and 0.29 °C/W
  under the matrix load, and it leads in fast power steps (19 °C at 66 W).
- TGPU was also counted as a CPU zone, so the policy cut the CPU for GPU
  heat.

### Matrix-multiplication frequency sweep (operator: "vermesse die Matrixmultiplikation mit verschiedenen Frequenzen, startend bei 1500")

Setup:
- The operator's unchanged `burnin.py`, GPU only (CPU idle), 120 s per
  step (`scripts/gpu_sweep.sh`).
- The clock was pinned by the configuration: entry = maximum below 1700 MHz,
  so those steps show no ramp by design.
- The operator stopped the sweep at 18:30, during the 1800 MHz step.

| Step | Measured | GPU W (last 60 s) | nvidia | TGPU | TGPU − nvidia | TFLOPS | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1500 | 1458 MHz | 30.5 | 52 °C | 61.9 °C | 8.5 °C | 53.8 | 0 |
| 1600 | 1581 MHz | 36.2 | 58 °C | 66.9 °C | 10.5 °C | 60.7 | 0 |
| 1700 | 1680 MHz | 40.3 | 60 °C | 71.9 °C | 11.8 °C | 65.7 | 0 |

Fit (`analysis/gpu_sweep.py`): P = 4.5 + 11.1 × (f / 1 GHz)^2.27 W. That is
about 71 W at 2200 MHz; live it was 66 W.

### GPU zone twin (`simulation/gpu_twin.py`)

Structure:
- a hotspot node (TGPU, 0.29 °C/W, 5 J/K) in front of the fitted GPU node
  (nvidia sensor) and the copper (GB10_FIT constants);
- the matrix power curve above;
- a 4 Hz ripple sampler and the guard's projection rule on TGPU;
- the real Supervisor as the controller.

Validation: replaying the sweep steps gives 30.6 / 35.9 / 40.5 W,
nvidia 51.9 / 55.8 / 59.2 °C and TGPU 60.7 / 66.2 / 71.0 °C. Measured:
30.5 / 36.2 / 40.3 W, 52 / 58 / 60 °C and 62 / 67 / 72 °C.

Worst case, 600 s burn-in at production settings (3 seeds each):

| Case | Guard events | GPU mean | Power | TGPU max |
| --- | --- | --- | --- | --- |
| GPU only, old loop | 1126 (first at 48 s) | 2036 MHz | 60 W | 95.7 °C |
| GPU only, **zone loop** | **0** | 1921 MHz | 53 W | 86.8 °C |
| GPU + 35 W CPU, old loop | 60 (first at 28 s) | 1848 MHz | 49 W | 94.8 °C |
| GPU + 35 W CPU, **zone loop** | **0** | 1776 MHz | 45 W | 87.0 °C |

Reading: a sustained matrix load at 2200 MHz is not holdable under the
93 °C ACPI rule. In steady state TGPU would reach about 104 °C, and about
112 °C with the CPU burn. The zone loop holds TGPU at about 86 °C, at about
1900 MHz for the GPU alone and about 1780 MHz with the CPU burn. The live
repeat is pending.

## 3. Never blind, one hardware talker, no restarts for tests

Operator:
- "fix the holes appearing in the dashboard … we will never be blind";
- "we don't like to restart the service for each test";
- "nur ein einziger Dienst in der Kette darf mit der Hardware sprechen,
  keine parallelen Seitenzugriffe wie z. B. vom Dashboard";
- "der einzige Grund für einen Serviceneustart sind Umstellungen im
  mathematischen Modell … alle Variablen / PID exportieren und änderbar
  machen".

What stopped the service between 17:58 and 18:31 (16 stops):

| Cause | Stops |
| --- | --- |
| Defect 33 | 4 |
| Sweep restarts for each frequency step | 5 |
| Defect 35 | 1 |
| systemd start limit (about 100 s without a controller) | 1 |
| Real guard abort (TGPU) | 1 |

Each stop left the dashboard blind for 25–40 s, because only the controller
process publishes the status.

Changes (build 20260927T184917):
- **energy_control is the only hardware talker.** Spark_Dashboard no longer
  runs `nvidia-smi` and no longer reads cpufreq. Its GPU card and CPU
  frequency groups come from the status, which now also carries the GPU
  hardware maximum. A separate monitor service was drafted and dropped: it
  would have been a second hardware reader.
- **Never blind.** An abort no longer ends the process:
  - the owners hold the safe state;
  - the service stays up and publishes `SAFE_STATE` (with the reason),
    waits for cooling and re-arms in-process;
  - the start publishes `STARTING` while it waits for cooling;
  - only more than 30 aborts in 10 min, a dead sampler or 30 s without
    telemetry exit the process to systemd (start limit 30, restart delay
    5 s).

  Measured on the install restart: one gap of about 1 s (18:50:41–42), then
  continuous `STARTING` and `HOLD`.
- **Live settings, no restarts:**
  - The boot-bound override `/run/spark-energy/qualification.json`
    (root-only) carries any live Config field and applies within about 1 s.
    Removing the file restores the committed configuration.
  - Verified live: GPU maximum 2100 MHz and `pid_band_c` 10 applied and
    reverted with an unchanged service PID.
  - The operator broker refuses proposals while an override is active.
  - The GPU entry ceiling is live. Only the CPU class maxima stay restart
    fields.
- **All model tunables live and exported** (`broker.TUNABLES`, 33
  parameters: bands, setpoint back-off and recovery, tapers, cluster
  gating, learning, fan staging, balance costs, emergency recovery, CPU
  wind-down):
  - settable with the password through the broker and CLI
    (`--tune NAME=VALUE`, `--untune NAME`);
  - through the override for tests;
  - exported in `control.tuning`.

  Not tunable: the aborts, the guard-mirror rules and REARM (PSU).
- **Found on the way:** live target or gain changes never reached the
  cluster loops, which held their own Settings. Fixed: they now receive the
  settings bumplessly (lowered ceiling at once, raised gradually).
- **Defect 33 fixed:** the GPU ownership reading is now stamped after its
  checks, and the owners log the exception text.

Validation: 724 tests OK (before the install) and headless OK; dashboard 17
tests, `/healthz` and `/api/services` 200. The dashboard GPU and CPU cards
now come from energy_control.

## Known state (18:55)

- **energy_control:** build 20260927T184917, production GPU maximum
  2200 MHz, entry 1700 MHz, no override, idle (HOLD). Hard limit 2500 MHz,
  qualified 2200 MHz.
- **vLLM is stopped,** and the RAM guard with it (by the dashboard). A
  restart needs the operator's word.
- No test load is running. The hardware claim of session 3dc0579e is still
  held, for the pending burn-in repeat.
