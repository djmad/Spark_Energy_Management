# Entry-ceiling qualification — goal v2, work plan 6

Started 26 September 2026, about 18:25 CEST, by the root main agent under
the standing hardware grant (AGENTS.md) and the session claim.

## Tooling

- `energy_control/trial_runner.py` — supervised live trials. Each trial:
  stop `energy_control` (owners' safe state: GPU 200–500 MHz, CPU minimum,
  fan 12) → wait for cold idle (GPU ≤ 45 °C, ACPI ≤ 55 °C, GPU util < 5 %,
  no vLLM requests; all sensors ≥ 5 °C below their aborts for 5 s) →
  trial-bound commissioning supervisor with the same live owners, a durable
  stage-3 plan (entry = max = step, fan floor 12, 30 s guard deadline) →
  one owned request (synthetic ≈ 3 200-token prompt, 512 output tokens)
  through the guard-authorised dispatcher, entry ceiling verified before
  start → 4 Hz trace → safe state → restart the service.
- `series`: steps 1200 → 1800 MHz by 100 MHz, 2 repetitions each. A fsynced
  intent line precedes every trial in `/var/lib/spark-energy/entry-series.jsonl`
  and a result line follows it. An intent without a result (power loss) or a
  failed result blocks that step and all higher steps; nothing is repeated
  automatically. Pass criteria: no abort, no request fault, measured GPU
  clock ≤ step + 50 MHz.
- Evidence: commissioning runs `/var/lib/spark-energy/commissioning-runs/`,
  4 Hz traces `/var/lib/spark-energy/trial-traces/<trial>/`.

## Baseline trial (1200 MHz, before the series)

`entry-1200-r1-20260926T181754`: 3 161 prompt + 512 generated tokens in
≈ 18 s at 83–96 % GPU utilisation; measured clock ≤ 1176 MHz; GPU power
peak 12.8 W (≈ 9–10 W steady), GPU ≤ 41 °C, hottest ACPI zone ≤ 69.9 °C; no
abort. Power is sampled at 4 Hz by `nvidia-smi`; millisecond supply spikes are
not visible — the pass criterion that matters is "no shutdown".

## Series 1 — single request, ≈ 3 200 in / 512 out (superseded)

All steps 1200–1700 MHz passed both repetitions (GPU peak 10.8–13.5 W,
measured clock always below the lock, GPU ≤ 42 °C). One harness event: after
1400 r2 the service restart hit systemd's start limit; the trial had passed
and a reviewed `correction` line records that. Stopped before 1800 MHz when
the operator asked for larger, concurrent requests (log `entry-series.jsonl`).

## Series 2 — 4 concurrent requests, ≈ 20 000 in / 10 000 out each

Operator direction (26 September 2026): prompts ≈ 20 000 tokens in, 10 000
out, more than one job. Plan limits raised accordingly (stage 3: 1–4 active,
prompt ≤ 32 768, output ≤ 16 384, ≤ 1 200 s; transport body ≤ 1 MiB,
request deadline ≤ 900 s); `ignore_eos` forces the full 10 000 tokens. One
harness event: the first launch failed before sending anything (body over the
old 64 KB bound); a reviewed correction unblocked 1200 MHz. Log:
`entry-series-w19000-o10000-j4.jsonl`.

| Step | Rep | Result | GPU peak W | Clock peak MHz | GPU peak °C | ACPI peak °C | Note |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1200 | 1 | pass | 15.5 | 1176.0 | 42.0 | 71.1 | re-run after reviewed harness/workload correction |
| 1200 | 2 | pass | 10.59 | 1176.0 | 43.0 | 76.0 |  |
| 1300 | 1 | pass | 11.27 | 1287.0 | 43.0 | 72.4 |  |
| 1300 | 2 | pass | 11.36 | 1287.0 | 43.0 | 73.2 |  |
| 1400 | 1 | pass | 11.68 | 1365.0 | 43.0 | 71.1 |  |
| 1400 | 2 | pass | 11.4 | 1365.0 | 43.0 | 74.0 |  |
| 1500 | 1 | pass | 12.62 | 1488.0 | 43.0 | 72.0 | re-run after reviewed harness/workload correction |
| 1500 | 2 | pass | 12.53 | 1488.0 | 44.0 | 76.4 |  |
| 1600 | 1 | pass | 13.27 | 1586.0 | 44.0 | 76.3 |  |
| 1600 | 2 | pass | 13.1 | 1586.0 | 44.0 | 71.0 |  |
| 1700 | 1 | pass | 14.3 | 1690.0 | 44.0 | 74.3 |  |
| 1700 | 2 | pass | 14.19 | 1690.0 | 44.0 | 71.4 | workload anomaly (reviewed correction) |
| 1800 | 1 | pass | 15.0 | 1768.0 | 46.0 | 74.2 |  |
| 1800 | 2 | pass | 15.02 | 1768.0 | 45.0 | 70.1 |  |

Each trial: ≈ 190–230 s with 4 requests active (≈ 180 generated tokens/s at
1200–1300 MHz); vLLM counters confirm the full outputs. Fan floor 12 during
every trial (maximum cooling, as planned).

**Series 2 complete (26 September 2026, 20:52): every step 1200–1800 MHz
passed both repetitions.** Two workload anomalies (one of four requests did
not complete normally: 1500 r1 stuck until the 900 s deadline, 1700 r2
RuntimeError as the batch finished) were reviewed and recorded as corrections;
neither had a guard/safety reason, a clock above the lock or a shutdown, and
1500 r1 was re-run cleanly. Dispatcher faults now record the exception text
and transport error for the next occurrence.

## Decision (withdrawn 26 September 2026, 23:50 — see below)

- ~~Qualified entry ceiling: 1700 MHz~~ (last passing step 1800 minus the
  100 MHz margin).
- **Maximum: 1800 MHz** (hard development limit; cannot be tested with margin
  above it), so the idle fallback to the entry ceiling stays enabled.
- GPU power at 1768 MHz under 4 × 20k/10k jobs peaked at 15 W (4 Hz samples)
  versus the > 90 W cold-entry spikes reported at vendor clocks
  (≈ 2.4–3 GHz). Millisecond spikes are not observable here; the qualified
  evidence is "no shutdown in 14 cold-start trials up to 1800 MHz".
- Next: the sustained combined-load block with ramping (entry 1700 → max
  1800) under the service's own fan staging and PIDs.

## Correction — series 1/2 did not test prefill (26 September 2026, 23:50)

vLLM runs with prefix caching (`enable_prefix_caching=True`, 16-token
blocks). Every synthetic prompt in series 1/2, the identification runs and
the sustained runs was byte-identical, so after the first request vLLM served
the prompt from the KV cache: `prompt_tokens_cached_total /
prompt_tokens_total` = 3 469 568 / 3 498 618 = **99.2 %**. The trials
therefore measured the decode phase (≈ 10–15 W GPU) and not the cold-entry
prefill burst the entry ceiling exists for. Consequences:

- The 1700 MHz entry decision is **withdrawn**; the service is back at entry
  1200 MHz / maximum 1800 MHz (`/etc/spark-energy/config.json`).
- The thermal/twin data (decode-dominated heat) stays valid as decode
  evidence; peak-power statements above do not cover prefill.
- Fix: every request now begins with a random 64-bit nonce
  (`trial_runner.request_body`, also used by `scripts/sustained_load.py`), so
  no prefix block after the chat template can match. Each result records the
  vLLM counter deltas (prompt/cached tokens, TTFT, prefill and decode time)
  and `prefix_cache_ratio`; a trial with a ratio above 0.2 (or no counters)
  is logged as a harness error and stops the series — it is neither a pass
  nor a failure.

## Series 3 — real prefill, 4 × ≈ 20 000 in / 10 000 out, unique prompts

Started 26 September 2026, 23:59 (log
`entry-series-w19000-o10000-j4-unique.jsonl`, console
`entry-series-prefill-run.log`), 1200 → 1800 MHz, 2 repetitions, stop at the
first failure. Results below.

### Series 3, first attempt (23:59–00:28)

| Step | Rep | Result | GPU peak W | Clock peak MHz | GPU peak °C | ACPI peak °C | Prompt / cached tokens | Note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1200 | 1 | pass | 16.27 | 1183 | 44 | 73.8 | 79 623 / 0 | TTFT mean 30.8 s, prefill sum 79.8 s; two requests finished at ≈ 380 s, two only at ≈ 1 280 s (≈ 8 tok/s each, GPU 96 % busy) — workload anomaly, under investigation |
| 1200 | 2 | guard telemetry abort at 387 s | 16.25 | 1176 | 41 | 65.6 | — | all prefills done; 3 consecutive acquisition failures (defect 14, doc/42); reviewed correction, step re-run |

Real prefill is confirmed (0 % prefix-cache hits). At 1200 MHz the
cold-start prefill of 4 × 20k tokens drew ≈ 16 W (4 Hz samples), close to the
decode level, so the reported GPU power shows no pronounced prefill burst at
this clock.

### Series 3, second attempt (from 00:37, guard fix installed)

| Step | Rep | Result | GPU peak W | Clock peak MHz | GPU peak °C | ACPI peak °C | TTFT mean s | Decode sum s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1200 | 1 | pass | 16.81 | 1176 | 43 | 69.5 | 29.8 | 1 499 |
| 1200 | 2 | pass | 17.48 | 1176 | 44 | 72.3 | 26.9 | 1 246 |
| 1300 | 1 | pass | 19.32 | 1287 | 46 | 75.1 | 24.8 | 1 521 |
| 1300 | 2 | pass | 18.72 | 1287 | 45 | 75.7 | 24.9 | 1937 |
| 1400 | 1 | pass | 19.35 | 1365 | 45 | 76.5 | 23.9 | 1575 |
| 1400 | 2 | pass | 19.62 | 1365 | 47 | 76.6 | 23.7 | 1134 |
| 1500 | 1 | pass | 21.19 | 1488 | 47 | 69.8 | 22.1 | 1097 |
| 1500 | 2 | pass | 21.46 | 1488 | 47 | 71.3 | 22.5 | 1896 |
| 1600 | 1 | pass | 23.57 | 1586 | 46 | 69.3 | 21.1 | 1058 |
| 1600 | 2 | pass | 23.49 | 1586 | 47 | 75.4 | 20.4 | 1377 |
| 1700 | 1 | pass | 25.25 | 1690 | 48 | 72.8 | 20.1 | 1085 |
| 1700 | 2 | pass | 25.58 | 1690 | 49 | 74.1 | 19.5 | 1166 |
| 1800 | 1 | pass | 28.75 | 1768 | 51 | 74.9 | 18.4 | 1276 |
| 1800 | 2 | pass | 28.59 | 1774 | 50 | 75.7 | 18.1 | 2484 |

All with 0 % prefix-cache hits and the full 4 × 10 000 generated tokens.

**Series 3 complete (27 September 2026, 02:46): every step 1200–1800 MHz
passed both repetitions with real prefill** (0 % prefix-cache hits, full
4 × 10 000 generated tokens, no shutdown, no guard/safety abort after the
defect-14 fix; the guard teardown line after each trial is expected, doc/42
item 15).

- GPU peak power (4 Hz) rises linearly with the clock: ≈ 16.5 W at 1200 MHz
  to ≈ 28.7 W at 1800 MHz (≈ 2 W per 100 MHz). Measured clock always below
  the lock (1176 … 1774 MHz).
- Temperatures far from the aborts: GPU ≤ 51 °C, hottest ACPI zone ≤ 76.6 °C
  (fan floor 12 during trials).
- TTFT (4 × 20k concurrent) falls from ≈ 28 s at 1200 MHz to ≈ 18 s at
  1800 MHz; decode scatter is a vLLM tail effect (doc/45).

## Decision (27 September 2026, 02:47) — supersedes the withdrawn one

- **Qualified entry ceiling: 1700 MHz** (last passing step 1800 minus the
  100 MHz margin), now with real-prefill evidence.
- **Maximum: 1800 MHz** (hard limit). The idle fallback to the entry ceiling
  stays enabled.
- `/etc/spark-energy/config.json`: `gpu_entry_mhz` 1200 → 1700 (backup
  `config.json.bak-20260927T0247`); effective at the next service start
  (the overnight trial chain restarts the service after each trial).
- Caveat unchanged: millisecond supply spikes are not observable at 4 Hz; the
  evidence is "no shutdown in 14 real-prefill cold starts up to 1800 MHz".
