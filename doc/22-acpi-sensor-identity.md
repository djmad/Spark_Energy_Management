# Lenovo ACPI thermal-zone identities — read-only discovery

On 26 September 2026, all seven live `/sys/class/thermal/thermal_zone*/type`
files read `acpitz`. Unlike that generic type, each zone's read-only
`device/path` exposes a distinct ACPI firmware object:

| Linux zone | ACPI path | New collector label |
| --- | --- | --- |
| thermal_zone0 | `\_TZ_.TSOC` | `acpi_TSOC` |
| thermal_zone1 | `\_TZ_.TS0E` | `acpi_TS0E` |
| thermal_zone2 | `\_TZ_.TS0P` | `acpi_TS0P` |
| thermal_zone3 | `\_TZ_.TS1E` | `acpi_TS1E` |
| thermal_zone4 | `\_TZ_.TS1P` | `acpi_TS1P` |
| thermal_zone5 | `\_TZ_.TGPU` | `acpi_TGPU` |
| thermal_zone6 | `\_TZ_.TUNC` | `acpi_TUNC` |

The live DSDT's SHA-256 was
`2871cbcb8992f7bd3915d6f19e7cfab577e068dcad58b6f00317a9f2d4e1b28e`.
No DSDT binary was copied into this project. `iasl`/`acpidump` were not
installed, and an attempted vendor-doc web lookup was unavailable, so the
names' physical interpretation is **not** verified. `TGPU` is a firmware
object name, not independent proof that its reading is the same NVIDIA GPU
sensor or of its update cadence. Do not rename `TS0P` or `TS1E` as a specific
CPU core/class without further evidence.

The Lenovo read-only collector now pins these exact zone-index/path pairs and
labels readings by ACPI path suffix. A missing or changed path, unexpected
`acpitz` zone, missing temperature or count change fails the full sample;
there is no fallback to anonymous zone numbers. Fake-sysfs tests include a
changed-path fault. A single non-root live sample after the change succeeded
with 20 CPU policies; it made no device write. Collector SHA-256 at this
check: `50c5c2245a9db4dc9137ce3307db07a986844e6bc3bc16c3364e7c279bd630bb`.
The pure commissioning guard now also defaults to requiring every one of these
seven labels plus the separate `gpu` reading. It refuses preflight if any is
missing and trips an armed fake run on disappearance. Other platforms need an
explicitly qualified sensor profile; the network API cannot choose one.

This improves stable identity for shadow telemetry and a future guard, but it
does not qualify the sensor-to-component mapping or 93°C protection. The
passive baseline in [the prior aggregate](21-passive-baseline.md) kept only
cross-zone extrema and was collected before this path-pinning change, so its
extrema cannot be retrospectively assigned to one firmware object. Mapping,
native refresh rate, inter-sensor delay and coupling require separate bounded
evidence before fitting or hardware control.

## Component mapping confirmed (26 September 2026, evening)

Operator-supplied layout (`spark layout.png`, GB10 die sketch with one diode
per block) plus a pinned-load test: 20 s busy loops on each group of five
cores (by kernel topology cluster 0 = CPUs 0–9, cluster 1 = CPUs 10–19; E-cores
at 338–2808 MHz, P-cores at 1378–3900 MHz), zone rise after ≈ 15 s:

| Loaded cores | Largest rises (°C) |
| --- | --- |
| E-cores 0–4 | TS0E +9.0, TSOC +6.5 |
| P-cores 5–9 | TS0P +31.7, TSOC +31.7, TUNC +26.3 |
| E-cores 10–14 | TS1E +7.9, TSOC +5.6 |
| P-cores 15–19 | TS1P +37.8, TSOC +36.3, TUNC +12.3 |

Mapping: `TS0E` = cluster-0 E-cores (CPUs 0–4), `TS0P` = cluster-0 P-cores
(5–9), `TS1E` = cluster-1 E-cores (10–14), `TS1P` = cluster-1 P-cores
(15–19); `TSOC` tracks the hottest cluster (package / hot-spot sensor);
`TUNC` (uncore) sits nearer cluster 0; `TGPU` rises only 1–2 °C under CPU load
(two-die package; CPU→GPU coupling only through the shared copper).
P-clusters heat by > 30 °C within ≈ 15 s under five busy cores. The sketch's
LPDDR6x, "monolithic die", DLA and hwmon names do not match this machine
(LPDDR5X, CPU + GPU dies joined by NVLink-C2C, no DLA, ACPI zones only).
