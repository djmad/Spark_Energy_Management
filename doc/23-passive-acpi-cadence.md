# Passive ACPI value-change cadence — not sensor-latency qualification

On 26 September 2026, two successive 10-second **read-only** windows sampled
the seven pinned Lenovo ACPI zones every nominal 100 ms while the existing
machine workload continued. The bounded `analysis.sensor_cadence` probe checked
the firmware path identities through the existing collector, retained only
aggregate change intervals in memory, and started no load or hardware write.
It produced 100 and 101 samples; maximum seven-zone scan times were 1.4 and
0.9 ms respectively. No raw temperature trace was saved.

| Pinned zone | Observed value changes, window 1 / 2 | Largest observed step, °C, window 1 / 2 |
| --- | ---: | ---: |
| `acpi_TSOC` | 96 / 95 | 2.5 / 2.4 |
| `acpi_TS0E` | 82 / 73 | 0.4 / 0.3 |
| `acpi_TS0P` | 95 / 95 | 2.5 / 2.4 |
| `acpi_TS1E` | 59 / 57 | 0.7 / 0.4 |
| `acpi_TS1P` | 92 / 88 | 2.1 / 2.3 |
| `acpi_TGPU` | 1 / 0 | 0.1 / 0.0 |
| `acpi_TUNC` | 79 / 84 | 1.7 / 1.6 |

For the six frequently changing zones, the median interval between *observed
value changes* was about 0.101 s in both windows. `acpi_TGPU` changed once in
the first window and not at all in the second. That could mean a stable value,
coarse quantization, slower firmware refresh or caching; this experiment
cannot distinguish them. A changed value proves only that it differed by the
next poll, not the time of the underlying physical change. An unchanged value
does not prove freshness. A 2.5°C step between polls is also not a validated
physical 25°C/s heating rate; aliasing, quantization and sensor semantics are
unresolved.

The collector's `age_s` currently bounds **acquisition duration**, not the
firmware value's internal age. Therefore the guard's 1-second age check cannot
by itself establish that these ACPI values reflect the current die temperature.
Before any load stage, qualify a conservative sensor-latency bound and physical
mapping with independent evidence; retain predictive margin and firmware
protection. The effective numeric GPU-lock proof and live cancellation path
remain separate blockers. This passive result changes no control setting and
does not authorize Stage 1.
The offline commissioning lifecycle now defaults its separate
`sensor_latency_qualified` arm gate to false; these passive windows do not
open it. The flag must ultimately come from a trusted qualification procedure,
not a network/API claim or this value-change summary.

Reproduce the aggregate-only observation (not a load test):

```sh
python3 -m analysis.sensor_cadence --seconds 10 --interval 0.1
```
