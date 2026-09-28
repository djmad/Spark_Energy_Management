# Release notes

## v1.0 — 28 September 2026

First public release of Spark Energy Management for the Lenovo ThinkStation PGX (NVIDIA
GB10).

### Included

- **`energy_control` service:**
  - GPU entry ceiling and busy-gated ramp, against the power-supply shutdown;
  - per-cluster CPU loops, a GPU PID and a GPU-hotspot (TGPU) zone loop;
  - a twin-based GPU/CPU power balance;
  - an independent guard with a 2 s projection, and an in-process safe state with
    automatic re-arm;
  - live settings, a password-confirmed operator broker and CLI, and a read-only status
    API.
- **Predictive fan** (`fan_policy` "predictive"): feed-forward from the load profile through
  the fitted cooler model, feedback from the loops' projections, gentle ramp-down, and an
  operator floor.
- **Calorimetric CPU power estimate** (calorimetric-v2), calibrated against the measured GPU
  power.
- **Digital twin:** a two-store cooler fit (die/plate 32 J/K, fin block/case 430 J/K, fan
  dependence, room air measured) and a closed-loop fan twin for tuning.
- **Standalone read-only dashboard:** the digital twin view and the history graphs.

### Qualified on the reference machine (27 September 2026)

| Test | Result |
| --- | --- |
| Worst case: GPU matrix burn-in plus 20-core CPU burn, GPU maximum 1700–2200 MHz | Every step passed: 0 compute errors, no abort |
| GPU burn-in alone at 2200 MHz | Mean cap 2152 MHz, standard deviation 54 MHz |
| CPU ceiling 89–92 °C, abort 96 °C | No abort; production ceiling 90 °C |
| LLM (vLLM) at 2200 MHz | 460 tokens/s alone, 355 tokens/s with 20 CPU workers |

### Release assets

- `spark-energy-evidence-20260928-public.zip`: traces, black boxes, calibration and trial runs of
  26–28 September 2026, with paths and host name anonymised. Verify it with
  `spark-energy-evidence-20260928-public.zip.sha256`.

### Licence

CC BY-NC 4.0, except the fan drivers in `drivers/`, which are GPL-2.0-only.

### Known limits

- The CPU power estimate has an absolute scale of about ±×2. GB10 exposes no CPU or module
  power reading.
- The twin's holdout error is 1.95 K. It cannot separate the plate from the fin block, and
  it runs about 7 K warm on TGPU for LLM loads (workload-dependent heat per GPU watt).
- A cold full-load start after a long idle at the fan floor costs about 4 % GPU clock in the
  first minutes. Raise the fan floor if that matters.
- Qualified on one machine only.
