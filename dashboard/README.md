# Spark Energy dashboard (standalone, read-only)

A small web page that shows what `energy_control` is doing on a Lenovo
ThinkStation PGX / NVIDIA GB10: temperatures, fans, clocks, power and a
digital twin of the heat path. It is strictly read-only and can be shared.

## What it shows

- **Header**: live/stale badge, controller mode, hottest ACPI zone, GPU clock
  and power, fan floor.
- **Digital twin – heat path** (open by default): a 2D diagram of the heat
  sources (GPU die, the four CPU clusters, background heat), the heat stores
  (die + contact plate, neck, fin block with the case air) and the heat
  removal (fans, room air), with the sensors drawn where they sit, plus KPI
  tiles (sources, removal, stored energy, charge rate, tightest margin to an
  abort, fans, case interior).
- **History graphs** (open by default), last 2 h: temperatures with target
  and abort lines, fan speeds and floor, GPU clock with its cap and a green
  load area, CPU cluster clocks with their caps and a green load area. The
  time axis shows the last 15 min on a log scale (right half) and 15 min–2 h
  linearly (left half).

There are no settings, no controls and no password on this page.

## Run

It needs `energy_control` running, because the only data source is the status
file it publishes (`/run/spark-energy/status.json`, about 1 Hz).

```sh
python3 -m dashboard.server                     # from the project root
python3 dashboard/server.py --port 8790         # same
python3 -m dashboard.server --host 127.0.0.1 --port 8790 --status-file /run/spark-energy/status.json
```

Then open `http://127.0.0.1:8790/`. Python 3.12, standard library only.

A staged systemd example is in `deploy/spark-energy-dashboard.service`
(unprivileged `DynamicUser`, `ProtectSystem=strict`,
`ReadOnlyPaths=/run/spark-energy`, loopback only). It is not installed by any
script.

## Endpoints

All GET; every other method gets 405, every other path 404.

| Path | Content |
| --- | --- |
| `/` | the page (`dashboard/index.html`, self-contained) |
| `/api/cooling[?since_ms=]` | tiered history of the status file; `since_ms` returns only newer rows |
| `/api/energy/status` | latest status payload with its freshness (`fresh`, `stale`, `missing`, `invalid`) and age |
| `/healthz` | liveness |

A sampler thread reads the status file about once a second. History is kept in
memory only for 2 h: raw 1 s samples for 3 min, 5 s means to 15 min, 30 s
means to 2 h (at most 600 buckets per tier, about 534 rows in total). A status
older than 5 s counts as stale.

## Security

- One hardware talker: only `energy_control` reads or writes GPU, CPU and fan
  hardware. This server reads nothing but the status file, runs no commands and
  writes nothing.
- The routes are fixed. No client-supplied path ever reaches the filesystem;
  the only accepted query parameter is `since_ms` on `/api/cooling`.
- It binds to loopback by default. To share it beyond the machine, put it
  behind an authenticated reverse proxy rather than binding a public address.
- Every response carries `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer` and a restrictive `Content-Security-Policy`.
  The page allows only its own inline script (by SHA-256 hash), inline styles
  and same-origin requests; JSON responses allow nothing.
- The status file contains machine telemetry and energy_control's run id and
  status reason. Review that before exposing the page publicly.

## The digital twin model

Everything below lives in the `TWIN CONSTANTS` block (object `K`) and
`CPU_MODEL` in `dashboard/index.html`. After a refit, change the values there.

**Measured, estimated, assumed**

| Quantity | Kind | Origin |
| --- | --- | --- |
| GPU power, nvidia temperature, GPU clock and load | measured | nvidia-smi via energy_control |
| ACPI zones TGPU, TS0P, TS1P, TS0E, TS1E, TSOC, TUNC | measured | energy_control |
| Wi-Fi, NVMe | measured | energy_control; reference, not in the control loop |
| Fan floor and fan speeds | measured | energy_control |
| CPU power per cluster (~) | estimated | `energy_control/power_estimate.py`, calorimetric v2, scale about ±×2 |
| Die + contact plate (≈) | estimated | TGPU − 0.52 K/W × P_GPU (the fit's observation equation); cross-check nvidia − P_GPU / 1.9 W/K |
| Fin block + case air (≈) | model estimate | no sensor: T_plate − P_in / 3.45 W/K, bounded to [room air, plate] |
| Background heat 12.3 W | fitted | board, RAM, NIC, idle SoC |
| Room air 21 °C | measured once | at the intake; no live sensor |

**Fitted cooler constants** (`analysis/sink_fit.py`, two-store fit with the
room air fixed at 21 °C; training on fan-floor runs at floors 2–12, RMS
1.02 K; holdout on fan-12 burn-ins, 2.4 K, bias +0.9 K):

| Part | Value |
| --- | --- |
| Die + primary contact plate (fast store) | C = 28 J/K ≈ 73 g Cu, τ ≈ 8 s |
| Neck, plate → fin block | G = 3.45 W/K |
| Fin block + case air (main store) | C = 272 J/K ≈ 303 g Al, τ ≈ 79 s at fan 12, 123 s at fan 2 |
| Fin block → room air | G = 1.92 + 1.50 × max(0.2, floor/12) W/K |
| TGPU hotspot | 0.52 K/W × P_GPU above the die/plate node |
| Background heat | 12.3 W |

Earlier cool-down time constants of 130–240 s are a slow case component that
the fit cannot separate.

**Reading the diagram**: arrow width ∝ heat flow; node colour = temperature
(30 °C blue → 45 °C mint → 60 °C amber → 90 °C red); dashed = estimated,
model or assumed; mint = fitted. Flows inside the die and the CPU zones are
quasi-steady and equal the source power. Stores hold energy
E = C × (T − T_room); watts are only their charge or discharge rate. Only live,
safety-relevant status is flagged on screen: stale data, missing status,
missing sensors and closeness to target or abort.
