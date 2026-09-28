# Read-only Lenovo shadow observer

## Optional GPU event diagnostics

Run as a non-root user with `python3 -m energy_control.observer --watch-gpu-events`
to enable a spawned read-only NVML event reader. `/api/v1/gpu-events` exposes
its state, observation age, cumulative clock-notification count and critical
event payload. It is unauthenticated like other telemetry, and accepts no
commands or query parameters. It keeps one current record in memory, no event
history on disk. Freshness is checked independently of the two-second thermal
sample interval; reader responsiveness becomes false beyond 0.5 seconds.

`reader_responsive` is diagnostic, not a safety authority. Both
`reset_coverage_qualified` and `clock_enforcement_verified` remain false.
Critical-event FAULT stays visible without automatically restarting the reader.
The observer does not abort production workloads or act on hardware.

Block validation: 16 focused API tests passed, and a temporary non-root observer
on loopback port 18766 returned a fresh QUIET event status through HTTP with
0.046-second age. It shut down with exit code 0; nothing was installed and no
clock/fan setting or workload was changed. The service unit remains uninstalled.

`energy_control/collector.py` collects ACPI thermal zones, CPU cpufreq policies,
`MemAvailable`, GPU-reported telemetry, two vLLM running/waiting gauges and the Lenovo fan driver's floor/RPM
readbacks. `energy_control/observer.py` samples these into the memory-only graph
store and serves the loopback API as a non-root user. It is read-only by
default, **not installed**, and has no actuator methods, workload admission,
or emergency guard.

The fan reader is a replaceable `FanTelemetryAdapter`; the Lenovo implementation
discovers by `dgx_ec_fan_floor`/`dgx_ec_fan` type rather than unstable numeric
indices. This is a telemetry adapter, **not** a portable fan-control adapter.
EC-backed fan reads may block, so this observer must not become the emergency
control path. The default interval is 2 s.

`energy_control/fan.py` separately defines a modular `FanFloorAdapter` contract.
Its Lenovo implementation uses only the guarded driver's `cur_state` additive
minimum and verifies readback; it never writes `max_state` or raw EC/PWM registers.
An unsupported platform fails closed. That actuator is tested only against fake
sysfs files and is **not connected to the broker or invoked on this machine**.
Firmware remains able to command 100% fan speed regardless of our minimum.

One bounded read-only sample on the Lenovo development host on 25 September 2026
found seven ACPI zones, 20 CPU policies, state-12 fan floor, 9000/13500 RPM,
about 26.9 GiB `MemAvailable`, hottest ACPI 49.2°C and GPU 39°C. That sample
reported measured graphics 864 MHz, applications clock 2418 MHz, and hardware
maximum 3003 MHz. **The new service's requested and accepted GPU maximum remain
unknown**; neither the measured clock nor applications-clock field proves a
verified lock. These values are a transient observation, not a qualification
trace or a safe operating recommendation.
An additional [read-only clock-limit probe](12-gpu-limit-readback.md) at 22:52
found the same clock-field distinction and no exposed accepted lock range in
the inspected `nvidia-smi` output.

The collector labels `phase=unclassified`, leaves system-input power and
temperature slopes null, and treats unavailable vLLM queue counts as null.
Its latest-state CPU fields now include the observed per-class hardware
minimum/maximum, requested cap, mean measured `scaling_cur_freq`, normalized
requested-cap ratio, policy count and host logical-CPU count. A class ratio is
null if its policies disagree or the hardware floor is unavailable; it is not
an accepted-limit proof or a measured utilization percentage.
A bounded read-only collector sample on 26 September returned 20 logical CPUs,
20 policies, fast bounds 1378–3900 MHz and slow bounds 338–2808 MHz, with both
requested-cap ratios at 1.0 at that instant. The accepted GPU cap remained
`null`. No service or clock was changed for this check.
It reads only running/waiting counts from the loopback metrics endpoint; model
labels and request bodies are not retained. The 0–20 limit belongs to the
simulator, not real queue telemetry. It does not infer a CPU sensor
identity from `acpitz`, fabricate missing readings as zero, or relabel GPU power
as wall power. CPU utilization needs two `/proc/stat` samples, so the first is
null. The graph API remains memory-only; the commissioning recorder is separate.

`energy_control/temperature_slope.py` now provides a **separate pure adapter**
for a future fast guard loop. It requires two coherent readings of the same
CPU-proxy/GPU sensor identities, rejects an acquisition age or inter-sample gap
over 1 s, and uses the largest positive observed rise over a bounded recent
window. A first reading, missing sensor or timeline break cannot yield a
predictive-safe snapshot. The adapter's default GPU accepted limit and
actuator-health inputs are deliberately unsafe; it never infers them from
measured clocks or RPM alone. It is fake-tested, not wired to the 2 s shadow
observer or a hardware abort loop. Sensor-internal refresh delays and peaks
between polls remain unknown, so this slope is not a guarantee against an
unobserved 93°C crossing.

For future commissioning, the separate
[`build_commissioning_sample`](../energy_control/commissioning_sample.py)
adapter can attach those estimated slopes to the durable sample and keeps
sensor acquisition timestamps separate from recorder-write timestamps. It
requires a matching, fresh numeric GPU-limit result; the present read-only
collector supplies no such result, so this path cannot authorize a live run.

For a manual shadow run under the non-root account:

```sh
python3 -m energy_control.observer --port 18765 --interval 2
```

It binds only `127.0.0.1`, exposes GET `/api/v1/state` and `/api/v1/history`,
and rejects writes. Do not run it as root. It does not supersede the legacy
controller, impose the 1800 MHz cap, or make load tests safe. Before commissioning,
establish sensor identity/freshness and actual GPU locked-clock readback, integrate
workload cancellation and an independent guard, and complete single-owner
handoff. GPU matrix-multiplication burn-in remains excluded from this goal.

The observer executable has an explicit `--enable-mutations` option that
attaches only the fixed API broker socket through the existing password-
confirmed `MutationRouter`. The operator CLI socket is rejected. This is
fake-broker wiring, not a production enablement recommendation: there is no
qualified live root broker, GPU-limit readback, ownership handoff, or trusted
remote browser gateway. Leaving the flag absent retains a read-only API.
The executable pins that API socket to
`/run/energy-control/energy-control-broker.sock`; it no longer offers a custom
socket-path option that could forward a password to an impostor endpoint.

The read-only API now marks latest-state telemetry stale after 1.5 configured
sample intervals (3 s at the 2 s default), rather than after 1 s between normal
samples. `GET /health/ready` returns 503 without a fresh sample; `/health/live`
only means the HTTP process is alive.
If the API's monotonic clock appears earlier than its last acquisition or is
invalid, readiness is also false and latest-state age is unknown; it is never
clamped to zero and shown as fresh. This is display readiness, not guard proof.
State and history carry a per-process
`history_generation`, a shared sample counter and, when readable, the Linux
boot ID. These are display/operations metadata, **not** a relaxation of the
independent commissioning guard's 1 s critical-telemetry limit. A non-root
read-only HTTP smoke check passed on this development host after this change.
The telemetry hub now rejects a graph/state pair whose acquisition timestamps,
CPU/GPU temperatures or utilization disagree, before either is published. This
prevents cards from visually pairing a temperature point with a different
clock-limit state. An unknown accepted GPU limit remains JSON `null`; the API
does not substitute requested, applications or measured clocks for it.
Both monotonic and UTC acquisition timestamps are mandatory for live API
publication; older commissioning records with absent optional timestamp fields
remain inspectable offline but cannot be presented as a coherent live pair.
After this stricter check, a single non-root read-only observer smoke run on
26 September passed the collector-to-loopback-API path. It checked that the
accepted GPU cap remained unknown (`null`); it did not qualify thermal timing,
the enforced GPU lock, or any actuator.
