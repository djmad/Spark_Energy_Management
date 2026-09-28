# LLM performance versus GPU and CPU clocks

Started 27 September 2026 (goal v2, operator question: "how big is the impact
of the LLM if CPU drops in speed?"). Model: nvidia/Gemma-4-26B-A4B-NVFP4 in
the resident vLLM (prefix caching on, fp8 KV cache, 16-token blocks). Load:
4 concurrent requests, ≈ 20 000 prompt tokens each with a unique opening
(0 % prefix-cache hits), 10 000 generated tokens each (`ignore_eos`).

Metrics come from vLLM's own counters, as deltas over each trial
(`vllm_delta` in the series/impact logs): `prompt_tokens`,
`prompt_tokens_cached`, `ttft_s_sum/ttft_count`, `prefill_s_sum`,
`decode_s_sum`, `generated_tokens`. Trace rows from 27 September on also
carry cumulative token counters (`vllm_gen_tokens`, `vllm_prompt_tokens`,
`vllm_cached_tokens`) for per-second throughput.

## GPU clock sensitivity (entry series 3, fan 12, policy-controlled CPU)

| Step MHz | Rep | Measured MHz | TTFT mean s | Prefill tok/s* | Decode tok/s per request | Aggregate decode tok/s |
| --- | --- | --- | --- | --- | --- | --- |
| 1200 | 1 (first attempt) | 1183 | 30.8 | 998 | 12.4 | 49.7 |
| 1200 | 1 | 1176 | 29.8 | 1 027 | 26.7 | 106.7 |
| 1200 | 2 | 1176 | 26.9 | 1 137 | 32.1 | 128.4 |
| 1300 | 1 | 1287 | 24.8 | 1 237 | 26.3 | 105.2 |
| 1300 | 2 | 1287 | 24.9 | 1 235 | 20.6 | 82.6 |
| 1400 | 1 | 1365 | 23.9 | 1 286 | 25.4 | 101.6 |
| 1400 | 2 | 1365 | 23.7 | 1 298 | 35.3 | 141.1 |
| 1500 | 1 | 1488 | 22.1 | 1 390 | 36.5 | 145.8 |

\* `prompt_tokens / prefill_s_sum` — consistent across trials, not a
single-request benchmark.

Preliminary reading:

- **Prefill is GPU-clock bound**: throughput rises about in proportion to
  the measured clock (≈ +35 % for +26 % clock); TTFT falls from ≈ 28 s to
  22 s.
- **Decode is not explained by the GPU clock**: at the same step it scatters
  by up to 2.6× (12–36 tok/s per request). During these trials the policy
  moved the fast-core cap between ≈ 2 000 and 3 900 MHz while ACPI stayed
  ≈ 42–50 °C, because its CPU-demand signal uses aggregate utilisation
  (≈ 10 %), which cannot see vLLM's single busy engine thread. The
  CPU-impact trials below fix the CPU caps to separate the two effects.

### Decode scatter is a vLLM tail effect, not the CPU clock (27 September, 02:50)

`analysis/llm_throughput.py` compared each trial's decode rate with the
busiest fast core (vLLM's engine thread) during the all-four-running phase:
that core ran at 3 150–3 600 MHz (mean fast-core cap 3 630–3 880 MHz) in
every trial, and the all-four phase lasted a similar 140–214 s. The 2–3×
scatter comes from the tail: in e.g. 1500 r2, three requests finished at
≈ 300 s and the fourth decoded **alone for 780 s at 96 % GPU utilisation but
only ≈ 10 W** (≈ 13 tok/s), with CPU caps and clocks like the fast 1500 r1.
Some requests enter a slow decode mode inside vLLM (cause unknown; vLLM is
out of scope for changes — possibly its native KV offloading). Consequences:

- The earlier hypothesis (policy lowering CPU caps during LLM decode) is
  **not supported** for the four-job phase; the mean fast-core clock
  (≈ 1 550 MHz) was misleading because most fast cores idle.
- Per-request vLLM decode averages are contaminated by these tails; the
  throughput model uses the traced aggregate token rate while all four
  requests run (trace counters, from 27 September) when available.

Refinement from the first traced trial (cpuimpact 1800 MHz / fast cap 3900,
27 September 02:47): the per-second token counters show that the "tail" is
**one of the four requests being slow from the start**, not a slowdown after
the others finish. At 387 s, 33 546 tokens were generated: three requests
had completed their 10 000 tokens (≈ 33 tok/s each) while the fourth had
≈ 3 500 (≈ 9 tok/s) and then continued alone at ≈ 10 tok/s. CPU impact is
therefore evaluated per request (10 000 / (finish − admission − TTFT)
from the commissioning events), comparing the fast requests across CPU caps
and counting slow ones separately.

### Healthy per-request decode versus GPU clock

Per-request rates (commissioning events; mean of the two fastest requests
per trial, busiest fast core ≈ 3 150–3 600 MHz):

| Step MHz | Measured MHz | Healthy decode tok/s per request | Slow requests (< 20 tok/s) |
| --- | --- | --- | --- |
| 1200 | 1172 | 30.5 / 32.6 | 0 of 8 |
| 1300 | 1284 | 33.3 / 28.6 | 3 of 8 |
| 1400 | 1362 | 35.7 / 35.5 | 1 of 8 |
| 1500 | 1488 | 36.6 / 36.2 | 1 of 8 |
| 1600 | 1579 | 38.0 / 37.1 | 1 of 8 |
| 1700 | 1687 | 36.9 / 38.9 | 0 of 8 |
| 1800 | 1766 | 40.2 / 40.1 | 3 of 8 |

Healthy decode rises ≈ 25 % for + 50 % GPU clock: roughly half of the
per-token time (≈ 13 ms of ≈ 25 ms at 1800 MHz) does not scale with the GPU
clock. The CPU-impact trials determine how much of that remainder scales
with the fast-core clock (`analysis/llm_throughput.py`, model
1/rate = A/gpu + B/cpu + D). Slow requests (≈ 8–10 tok/s from the start,
9 of 56 requests in series 3) occur at most clocks.

## CPU impact, block 1: fast-core cap only (27 September, 02:47–03:30)

`trial_runner cpuimpact --mhz 1800 --fast-caps 3900 2600 1378`: GPU lock
1800 MHz, fan 12, `cpu_entry_ratio` 1.0, E-core cap unchanged (2808 MHz),
4 × 20k/10k unique prompts (0 % cache hits), cold start each.

| Fast cap | Busiest P-core (measured) | Busiest E-core | CPU util | Hottest ACPI | TTFT s | Prefill tok/s | Per-request decode tok/s | GPU W (mean) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3900 | 3473 | 2295 | 13 % | 75.8 °C | 20.4 | 1 497 | 10.1 / 28.4 / 29.5 / 40.0 | 14.6 |
| 2600 | 1892 | 2774 | 21 % | 53.8 °C | 20.3 | 1 518 | 38.0 / 38.9 / 39.1 / 39.2 | 14.6 |
| 1378 | 1687* | 2757 | 23 % | 54.3 °C | 19.1 | 1 613 | 26.1 / 38.5 / 38.5 / 38.7 | 14.4 |

\* measured above the requested 1378 MHz cap (CPPC feedback counters);
requested, measured and hardware maximum stay distinct.

Findings:

- **Capping only the fast cores costs ≈ 3 % decode and nothing in
  prefill/TTFT**: Linux moves vLLM's busy threads to the uncapped E-cores
  (busiest E-core 2.3 → 2.8 GHz, utilisation 13 → 22 %).
- **Thermal win: the hottest ACPI zone drops from 75.8 °C to ≈ 54 °C**
  (−21 °C) for the same LLM work — the P-clusters are the CPU hot spots
  (doc/22).
- Model fit over 18 trials (series 3 + block 1, healthy rates):
  1/rate = 26.2/gpu_MHz + 1.96/cpu_MHz + 0.0100 s (RMSE 1.9 tok/s). At
  1800 MHz the GPU-clock part is ≈ 14.6 ms, the clock-independent part
  ≈ 10 ms and the CPU part ≈ 0.5–1.4 ms (2–5 %) per token. Block 1 does not
  yet bound the CPU term, because the E-cores kept running at 2.8 GHz.

## CPU impact, block 2: both clusters capped (chain 3, from 04:01)

`--fast-caps 1378 2600 --slow-caps 1400 2000` at 1800 MHz, fan 12.

| Caps slow/fast | Busiest E / P core | CPU util | Hottest ACPI | TTFT s | Prefill tok/s | Per-request decode tok/s |
| --- | --- | --- | --- | --- | --- | --- |
| 1400 / 1378 | 1397 / 1387 | 23 % | 53.9 °C | 20.5 | 1 481 | 36.5 / 36.7 / 36.8 / 37.3 |
| 2000 / 2600 | 1830 / 2520 | — | 56.1 °C | 18.1 | 1 682 | 10.4 / 40.6 / 41.3 / 41.3 |

With the **whole CPU at ≈ 1.4 GHz** healthy decode is 37.0 tok/s against
≈ 40 uncapped (**−7.5 %**); TTFT and prefill are unchanged. Refit over 19
trials: 1/rate = 26.3/gpu_MHz + 2.99/cpu_MHz + 0.0096 s (RMSE 1.8 tok/s):
at 1800 MHz GPU the CPU term is ≈ 0.9 ms per token at 3.4 GHz and ≈ 2.1 ms
at 1.4 GHz, of ≈ 25–27 ms.

## Conclusions and twin feedback (27 September 2026, 04:40)

Final fit over 20 trials (series 3 + both CPU blocks, healthy per-request
rates): **1/rate = 27.0/gpu_MHz + 3.0/cpu_MHz + 0.0091 s**, RMSE 1.8 tok/s.

| Clocks (GPU / busiest CPU core) | Predicted decode tok/s per request | CPU share of token time |
| --- | --- | --- |
| 1800 / 3400 | 40.0 | 3 % |
| 1800 / 1400 | 38.1 | 8 % |
| 1200 / 3300 | 30.8 | 3 % |

- **The LLM is GPU- and memory-bound, not CPU-bound**: the whole CPU at
  1.4 GHz costs ≈ 5–8 % decode; fast-core caps alone ≈ 3 % (threads move to
  the E-cores); prefill/TTFT do not depend on the CPU clock.
- **GPU clock**: decode +25 % and prefill ≈ +45 % from 1200 to 1800 MHz;
  ≈ 9 ms per token is clock-independent (memory bandwidth, sampling).
- **Thermal lever**: fast-core caps during LLM work lower the hottest ACPI
  zone by ≈ 20 °C at a ≈ 3 % throughput cost — the cheapest relief GB10
  has.
- Controller changes: fast-core reservation during LLM work 0.35 → 0.10;
  no GPU spill on behalf of the CPU below the CPU target (doc/42
  defect 17). CPU cost per watt stays 0.6 until the CPU twin node is fitted
  (per-cluster steps, doc/44), then it is set from these rates
  (GPU ≈ 1.6 %/W, CPU ≈ 0.5 %/W estimated).
- Twin: `simulation.model.GB10_LLM` (`LlmThroughput`) carries the fit;
  simulated samples report `llm_decode_tok_s` and the headless summary
  `llm_decode_tok_s_mean`.
- vLLM anomaly: ≈ 1 request in 6 decodes at ≈ 10 tok/s from the start at
  every clock (not modelled; vLLM out of scope).
