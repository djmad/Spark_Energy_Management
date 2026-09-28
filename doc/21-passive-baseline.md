# Passive Lenovo baseline — 26 September 2026

This is bounded **read-only observation** of whatever work was already running,
not a controller trial or a new load. The probe was run as `operator` through
`timeout`, with no broker connection, GPU/CPU/fan write, service restart or
workload interruption. Code: `energy_control/passive_probe.py`, SHA-256
`9d4e622e243197b2f3585ecbe18f261ace9fff2d515906d9edb3cbd8ae5d5cf0`
at capture. The command was `python3 -m energy_control.passive_probe --samples
61 --interval 2` for each long window, then `--samples 10 --interval 2` for a
short completeness check. The probe retains only aggregate extrema and counts
in memory, prints one JSON summary, and saves no raw time series or prompts.
The collector was subsequently changed to pin ACPI firmware paths (see
[sensor identities](22-acpi-sensor-identity.md)); these aggregate extrema did
not preserve per-zone series and cannot be relabelled retrospectively.

| Window start (Europe/Vienna) | Host samples | ACPI range across all zones/samples | GPU temperature | Highest measured GPU clock | Peak GPU utilization | CPU requested maxima, fast/slow | Fan floor and RPM |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 00:44:51 CEST | 61/61 in 120 s | 37.7–62.3°C | 37–38°C | 871 MHz | 89% | 3900/2808 MHz throughout | state 12; 9000–13500 RPM |
| 00:47:09 CEST | 61/61 in 120 s | 38.0–66.7°C | 37–41°C | 871 MHz | 89% | 3900/2808 MHz throughout | state 12; 9000–13500 RPM |
| 00:49:44 CEST | 10/10 in 18 s | 38.6–62.9°C | 38–41°C | 871 MHz | 85% | 3900/2808 MHz throughout | state 12; 9000–13500 RPM |

Maximum read-pass durations were 86.4 ms, 93.3 ms and 63.8 ms respectively.
Minimum reported `MemAvailable` was 28,368,019,456, 27,247,534,080 and
28,197,466,112 bytes. The long windows observed at most two active and zero
waiting vLLM requests, but those earlier summaries did **not** count missing
queue-metric reads, so zero is not complete-coverage evidence. The final
10-sample check explicitly found zero missing queue metrics, at most four
active and one waiting request, all 20 CPU policies with the expected 10+10
class split, and zero fan-health failures. Its scoped NVIDIA-reported GPU
power ranged 7.09–11.46 W; this is not wall-input power or the operator's
~70 W worst-case GPU observation.

The legacy CPU PID had no observed requested-cap movement in these windows;
its full maxima do not demonstrate its thermal response. The highest ACPI
reading belongs to an *unmapped* zone and the minimum/maximum may be different
zones. A 2-second interval cannot resolve cold-to-prefill spikes, sensor delay,
unobserved peaks or crash timing. The observed 871 MHz clock is instantaneous,
not a verified effective lock. This evidence therefore cannot identify model
gains, certify an <=1800 MHz GPU limit, or authorize load stages 1–7. It is
partial Stage-0 context only; mapped sensors, scoped power, independent guard,
single-owner handoff and accepted-lock proof are still missing.

## Short high-cadence organic-workload check

At 08:34:25 CEST, a further read-only `passive_probe` window collected 40/40
host samples at a requested 0.25-second interval (9.75 seconds from first to
last timestamp). The longest read pass was 86.0 ms. The existing vLLM metrics
reported at most two active and two waiting requests, with no missing queue
reads. GPU utilization reached 88%, yet the maximum measured graphics clock
was 871 MHz and scoped GPU-reported power stayed at 8.09–10.44 W. GPU
temperature was 38–39°C; the unmapped ACPI values ranged 39.8–71.5°C. The
minimum available memory was 27,341,627,392 bytes. All 20 CPU policy maxima
were at 3900/2808 MHz, the additive fan floor remained state 12, and neither
fan-health nor CPU-topology faults were observed. No clock above 1800 MHz was
observed in this short window.

This simultaneous high utilization and low *reported* GPU power cautions
against treating utilization alone as power demand or a prefill-spike proxy.
It is not proof of a numeric effective clock limit, input power, actual work
done, sustained stability, or sensor-internal freshness. The window did not
generate traffic or intentionally exercise a cold-to-load transition; it
cannot calibrate the thermal model or open a load stage.

## Later near-idle read-only window

At 09:58:04 CEST, the same `energy_control/passive_probe.py` revision
(SHA-256 `9d4e622e243197b2f3585ecbe18f261ace9fff2d515906d9edb3cbd8ae5d5cf0`)
was run as `python3 -m energy_control.passive_probe --samples 30 --interval 1`.
All 30 samples succeeded over about 29 seconds, with a maximum 61.8 ms
read-pass duration and no missing queue metrics. Existing vLLM metrics showed
zero active and zero waiting jobs throughout. GPU temperature was 33°C in
every sample; peak utilization was 16%, peak measured graphics clock 968 MHz,
and scoped GPU-reported power ranged 4.10–4.27 W. The seven ACPI zones taken
together ranged 33.9–57.0°C. Minimum `MemAvailable` was 27,947,700,224 bytes.
CPU requested maxima remained 3900/2808 MHz across the expected 20-policy
topology, and the additive fan floor stayed at state 12 with observed RPM
9000–13500. No fan-health failure or measured GPU clock above 1800 MHz was
seen. Read-only `systemctl show` immediately afterward found the legacy CPU
guard active/running and the fan-max one-shot active/exited, each with zero
reported restarts.

This is useful near-idle context, not a naturally observed cold-to-prefill
transition. The constant 33°C GPU reading does not establish sensor refresh
latency or physical peak temperature. Neither a 968 MHz measured clock nor a
quiet queue proves an accepted GPU cap, and there is still no wall-input or
ambient measurement. The physical model and load-stage gates remain open work.
