# Machine investigation — 2026-09-25

Initial live reads were around 19:02–19:05 Europe/Vienna. Archive evidence has
its own UTC capture timestamps; dynamic values will differ. All hardware and
service inspection was read-only. No sustained sampling or crash reproduction
was attempted because inference was active.

## Verified live baseline

| Area | Observation | Implication |
| --- | --- | --- |
| Platform | LENOVO 30KL0005GF, aarch64, NVIDIA GB10 | Use the qualified Lenovo driver contract, not generic GPU fan controls |
| Kernel / driver | 7.0.0-1019-nvidia / NVIDIA 580.178.04 | Pin this baseline for initial qualification |
| DMI BIOS string | S0QKT0EA | Keep distinct from semantic firmware versions in historical documentation |
| GPU | 1969 MHz observed, 3003 MHz hardware maximum; 57–59°C | Neither value proves an enforced 2000 MHz cap |
| GPU utilization | Initial sample 96% | Workload active; no restarts appropriate during investigation |
| GPU power | Roughly 18–19 W in sampled output; power limits N/A | This is driver-reported GPU power, not measured whole-system input power |
| Supported-clock query | N/A | Do not assume an enumerated MHz ladder is available |
| Thermal zones | Seven zones, all named acpitz; hottest roughly 90–91°C | Sensor-to-component mapping remains unproven |
| CPU | 20 cppc_cpufreq policies, conservative governor | Read per-policy limits and topology; do not assume identical CPUs |
| CPU classes | 10 policies with max 2808 MHz, 10 with max 3900 MHz | Current controller differentiates classes by hardware maximum |
| Fans | dgx_ec_fan_control 0.1.3; state 12/12, 9000 and 13500 RPM | Maximum additive fan floor is active |
| vLLM metrics | 2 running requests, 0 waiting in one sample | Metrics are available on loopback port 8000; scrape labels were omitted |

Fan project documentation records EC 3.5.8 and semantic UEFI 2.0.14; EC firmware
was not interrogated again through a new probe. No new module was loaded.

## Running ownership

| Component | Status observed | What it owns today |
| --- | --- | --- |
| spark-cpu-thermal-guard.service | Active, enabled, root, zero restarts reported | CPU governors and min/max limits every 500 ms; GPU lock once at startup |
| dgx-fan-max.service | Active (exited), enabled | One-shot state 12; stop requests automatic state 0 |
| dgx-fan-control.service | No installed unit found | Optional adaptive daemon exists in source, but is not running |
| nv-cpu-governor.service | Masked | Avoids a competing CPU governor writer |
| nvidia-persistenced / nvidia-dgx-telemetry | Active | Adjacent GPU infrastructure; not shown to enforce the desired GPU cap |
| dgx-dashboard / dgx-dashboard-admin | Active system services | NVIDIA dashboard; admin process runs as root; not audited internally here |
| spark-dashboard.service | Active operator user service, loopback 8799 | Machine dashboard and lifecycle controls |
| spark-vllm-bridge.service | Active operator user service | TCP relay from Docker bridge port 8000 to loopback vLLM |
| hostapp-ram-guard.service | Active operator user service | 10 ms memory guard; current arguments kill 4 GiB, warn 6 GiB, rearm 10 GiB |
| spark-stack-boot.service | Failed user unit | Boot orchestrator status warrants separate diagnosis; no cause established here |

Root's default user-systemd connection was unavailable. User service observations
were obtained explicitly as operator using /run/user/1000.

## CPU controller details

Installed `/usr/local/libexec/spark-cpu-thermal-guard.js` matches
`HostApp/tools/cpu_thermal_guard.js` byte for byte at inspection.
Target: 93°C; interval: 500 ms; gains: Kp 0.075, Ki 0.012, Kd 0.06.
It uses the hottest valid ACPI zone, anti-windup, immediate reductions and
recovery limited to 0.015 of normalized frequency range per tick. The active
drop-in selects `fast-first`: slow-core ratio is min(1, fast ratio / 0.75), with
an extra emergency ceiling at >=95°C. At >=97°C the CPU cap reaches hardware
minimum. Sensor/control exceptions attempt that same minimum fallback.

The 23 September tuning results report improved stressor throughput, but also
state that neither all-core run met the uninterrupted five-minute temperature
acceptance window. These are useful prior observations, not completed system
stability qualification. The 95°C synthetic-test abort differs from the 97°C
production emergency threshold.

Limitations: wall-clock time in the PID, no per-sensor freshness contract,
partial sensor disappearance can go unnoticed, no multi-policy transaction,
no fan/GPU coordination, and no independent observer if this process hangs.
Status is atomically renamed, but hardware actuator readback is not recorded
as a complete verified transaction. Logs go to a workload-owned directory.

## Critical GPU configuration discrepancy

Both the installed and source CPU unit contain:

```text
ExecStartPre=/usr/bin/nvidia-smi -i 0 --lock-gpu-clocks=250,500
```

Live graphics clocks were 1969 MHz. The startup definition, user requirement
(2000 MHz ceiling), and live measurement are different facts. The later actor
or driver event responsible for current clocks was not identified. No persisted
2000 MHz enforcement was found in the inspected service/source locations.
This was a scoped search, not proof that no other writer exists anywhere.
Restarting or reinstalling the legacy guard can reapply the 500 MHz ceiling.
The standard query used here did not establish the active locked-clock range.

## Fan control is stronger than its current policy

The running policy is simply maximum cooling. The driver itself has platform
guards, serialized firmware transactions, ownership checks and floor readback.
It raises an additive common RPM floor; firmware may always demand more cooling.
State 0 removes the floor; it does not switch the fans off. Two independently
programmable fan PWM curves are not exposed by this interface.

An unused userspace curve already exists: 50/55/60/65/70°C maps to states
3/5/8/10/12, filtered at alpha 0.35 every 2 s, with 4°C down hysteresis.
Thus improved fan policy can reuse established concepts and the guarded driver.

Historical firmware diagnostics describe pending/stale-response failures. Poll
loops do not bound every underlying FF-A call. Do not put EC I/O in the same
blocking execution path as CPU emergency control or infer fan RPM from a
successful state write. Do not introduce raw EC writes or parallel mailbox owners.

## Existing security and integration constraints

The dashboard reads `/run/spark-cpu-thermal-guard/status.json`, treating data
older than three seconds as unavailable. Its CPU temperature label actually
represents the maximum ACPI-zone reading.

The scoped sudoers rule gives operator passwordless start/stop/restart/enable/disable
of the CPU guard. This is narrower than arbitrary sudo, but it bypasses any
future API confirmation if retained as another control route. Frequency policy
and lifecycle authorization must be reconciled during migration.

The Docker vLLM relay is a byte-stream proxy, not request admission control.
Direct loopback clients and the Docker route would bypass a new admission gate
unless both are integrated. Existing model launch and model-switch paths also
need admission before loading/warmup, not just before user inference requests.

## Crash evidence and limits

Current-boot kernel records contain a corrected hardware-error report at boot.
The inspected current/previous-boot filtered records did not establish a GPU Xid
or thermal shutdown as the failure cause. `/sys/fs/pstore` was empty. Several
boots occurred that afternoon; that alone does not explain their causes.

The existing boot script documents earlier unified-memory exhaustion freezes
and deliberately uses sequential startup, a 12 GiB admission floor and a
separate emergency RAM guard. Preserve those protections. Cooling and ramping
cannot fix memory exhaustion, a defective supply or a driver/firmware fault.

Open investigations: active GPU cap provenance and reset behavior; actual
board/input power and transient visibility; sensor mapping; safe first-load
clock; long-prompt prefill behavior; idle-to-load failure trace; all competing
writers; exact reason for the failed boot unit. No claim of root-cause diagnosis
or guaranteed stability follows from this snapshot.

## Read-only follow-up, 23:48 Europe/Vienna

`nvidia-smi` reported 871 MHz measured graphics clock, 2418 MHz applications
clock, 3003 MHz hardware maximum, 83% GPU utilization and 38°C. These fields
still do **not** identify the effective locked-clock range. A targeted text
search for GPU-lock and CPU cpufreq write calls in `/etc/systemd`,
`/usr/local/libexec`, `/usr/local/bin`, the user's systemd directory and
`~/Documents` (excluding this project's Archive and log/JSONL files) found the
installed legacy CPU unit/script and source/test copies, but no additional
installed unit in that search. This is a scoped text inventory, not proof that
no binary, driver, container or transient writer exists. No setting was changed.

## Operator's additional power evidence

The operator reports approximately 70 W GPU, 30 W CPU and 140 W whole-system
load including network cards using the existing burn-in scripts. This is separate
from our live inference snapshot. See [power measurements](06-power-measurements.md)
for provenance, script findings and the proposed measurement campaign.
