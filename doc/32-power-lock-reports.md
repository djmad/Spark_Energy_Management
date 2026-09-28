# Additional power-lock reports — not Lenovo calibration

**Latest operator correction:** on this machine the reported power-lock trigger
is 100% load applied with unrestricted clocks. Do not equate this observation
with a proven USB-C handshake defect or assume cold temperature is required.
The immediate prevention strategy remains a verified pre-load clock ceiling;
do not attempt an unrestricted-clock reproduction.

The operator supplied a temperature/clock table describing a roughly 611 MHz,
13 W power-delivery lock, a 2000–2200 MHz operating sweet spot, and shutdown
around 96–97°C. The original table source and measurement methods were not
provided. Treat its exact thresholds and permanent-fix claim as unverified,
not as configuration or a temperature-to-frequency calibration curve.

Related first-person reports:

- https://forums.developer.nvidia.com/t/dgx-spark-grace-blackwell-gb10-performance-drop-gpu-trapped-in-15w-650mhz-loop-with-50-c-artificial-t-limit-temp/370304
  describes approximately 650 MHz and 15 W under reported heavy utilization.
  The thread discusses suspected power delivery and distinguishes accumulated
  power-capping counters from a currently active limiting reason. It does not
  establish the cause of instability on our Lenovo.
- https://forums.developer.nvidia.com/t/investigating-513mhz-cap-for-gpu/361296
  reports a different low-clock condition under load. There is no universal
  611 MHz diagnostic signature established by these observations.

Keep three separate hypotheses: cold-load electrical transient (the primary
operator concern), persistent power-delivery restriction, and thermal limiting
after heat accumulates. Record requested/verified caps, measured clocks,
utilization, power scope, active clock-event reasons and counter deltas before
classifying a low clock. Our intentional 500 MHz abort ceiling must not be
misclassified as a PD failure. Do not increase clocks to defeat firmware limits.

Important telemetry distinction: NVIDIA documents GPU T.Limit as remaining
thermal margin, not absolute chip temperature or a fixed shutdown threshold.
A T.Limit reading of 50°C alone does not establish a 50°C thermal lock.
Reference: https://docs.nvidia.com/deploy/nvidia-smi/ (Temperature section).

No change to hard limits: GPU <=1800 MHz; abort tests by 93°C or earlier;
resident LLM stays running while owned prompts are cancelled and GPU capped at
500 MHz on abort. The supplied 2000–2200 MHz range is outside authorization.
No power cycling, firmware changes, stress loads or hardware writes were made.

## Read-only Lenovo snapshot, 26 September 2026

Scoped command: `nvidia-smi -q -d PERFORMANCE,POWER,TEMPERATURE,CLOCK`.
Tool timestamp: 13:30:48 local display; driver 580.178.04.

| Field | Reported value |
| --- | --- |
| GPU actual temperature | 35°C |
| GPU T.Limit (remaining margin) | 61°C |
| Average / instantaneous reported GPU power | 4.50 / 4.55 W |
| Measured graphics clock | 1176 MHz |
| Applications / hardware maximum clock | 2418 / 3003 MHz |
| Active clock-event reasons | All displayed reasons Not Active |
| Displayed clock-event counters | All zero |
| Current/requested/min/max power limits | N/A |
| Shutdown/slowdown T.Limit thresholds | N/A |

This sample does not show the reported persistent 611 MHz/13 W restriction.
It cannot rule out transient power failure under load, prove a negotiated PD
contract, or establish effective clock-lock readback. No test load was started.
Do not treat the sum of current temperature and margin as a verified shutdown
threshold. The collector audit confirms it reads `temperature.gpu`, not
T.Limit, into its actual-temperature field. Its generic `power.draw` field
remains reported GPU power, not a whole-system or fast-transient measurement.

Next power investigation should correlate event-reason/counter changes with
the prefill timeline and an appropriately sampled power source during the
bounded qualified trial. This snapshot does not justify power cycling or
firmware changes, nor replacing the preventative entry-clock control with a
temperature-only response.
