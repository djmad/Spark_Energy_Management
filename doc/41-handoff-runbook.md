# Handoff, rollback and boot-ordering runbook — goal v2, step 5

Status: written 26 September 2026; software gates closed the same day (service
mode, live factories, entrypoint, installation, boot-order gate). Hardware authority comes from the operator grant in
`AGENTS.md`; only the root main agent session may run this block, after
claiming `/run/spark-energy/agent-command`. The inventory it acts on is
[doc/36](36-service-ownership-inventory.md).

## Current state (read-only check, 26 September 2026, about 15:56 CEST)

| Item | State |
| --- | --- |
| `spark-cpu-thermal-guard.service` | active, enabled (sole CPU thermal control besides firmware) |
| `dgx-fan-max.service` | active, enabled (floor 12) |
| `energy_control.service` | not installed |
| `nvfancontrol` / `nv-cpu-governor` / `nvidia-enable-power-meter-cap` | disabled / masked / masked |
| CPU policies | at hardware maximum (slow 338–2808, fast 1378–3900 MHz), `conservative` |
| GPU | 1170 MHz measured, 35 °C, 4.3 W, no clock-event reasons; `clocks.max.gr` 3003 MHz is the hardware maximum, not the lock |
| Hottest thermal zone | 40.4 °C |
| vLLM | `vllm_node` up about 2 h |
| User units (leave running) | `hostapp-ram-guard`, `hostapp-resource-supervisor`, `spark-vllm-bridge`, `spark-dashboard`, `spark-stack-boot` |
| Agent claim | none |

## 1. Software gates (must all be closed before the block)

1. **Service mode.** The supervisor is commissioning-shaped: its guard binds
   a trial plan (duration ≤ 600 s, admission budgets) and the GPU owner has a
   128-command session budget. The installed service needs a service mode
   with the same fixed limits (1800 MHz, ACPI 93 / GPU 85 °C, entry ceiling,
   lowest-MHz abort) but no trial deadline, an unbounded-but-lockstep GPU
   owner, and bounded, rotating commissioning logs. Commissioning trials keep
   the trial-bound mode.
2. **Live factories.** Child-local, hardware-enabled owners:
   `LoggedGpuClockSetter(enable_hardware=True, on_fault=…, read_ownership=…)`,
   `LenovoGb10CpuMaxima(allow_live_sysfs=True)`,
   `LenovoDgxFanFloor(allow_live_sysfs=True)`, and the live
   `guard_host_source` with owner evidence feeds.
3. **Ownership source.** A trusted, fresh `GpuOwnershipReading`: boot ID from
   `/proc/sys/kernel/random/boot_id`, a driver/reset epoch that changes on
   driver reload or GPU reset (not the driver version), and handoff evidence
   that the legacy writers are inactive **and masked** (from
   `handoff_probe.py`), not a boolean.
4. **Service entrypoint.** Root owners and guard, unprivileged read-only API,
   4 Hz control loop fed by the isolated host sampler, GPU utilisation and
   vLLM queue metrics; readiness file for boot ordering (section 4).
5. **Installation.** Reviewed copy under `/opt/spark-energy` (root-owned, not
   this home tree), staged unit reviewed like `deploy/spark-energy-observer.service`.
6. **Fake end-to-end run** of the installed entrypoint with fake adapters,
   including this runbook's rollback, before the first live write.

## 2. The handoff block

One supervised block, LLM idle (no owned or observed running requests), no
test load, all temperatures below 60 °C, GPU telemetry fresh. Every step is
recorded with intent before and outcome after in the commissioning log.

| # | Action | Check before continuing |
| --- | --- | --- |
| 0 | Claim `/run/spark-energy/agent-command` atomically (session, cwd, ISO time, "goal v2 handoff") | File created by this session |
| 1 | Read-only baseline: units, CPU policies, GPU clocks/temps, vLLM queue | LLM idle, all sensors < 60 °C |
| 2 | Install reviewed code (`scripts/install-energy-control.sh`) | Manifest verifies; unit staged, disabled |
| 3 | `systemctl stop spark-cpu-thermal-guard dgx-fan-max`; `disable` both; `mask` both plus `dgx-fan-control` (absent; masking fences it) | `handoff_probe`: every known writer masked, inactive, job-free |
| 4 | `systemctl enable --now energy_control` immediately after step 3 | Service waits 10 s for cool sensors, applies GPU 200–1200 MHz (logged intent), fan floor 12, reads all 20 CPU policies, starts its guard, writes `/run/spark-energy/entry-ceiling` |
| 5 | Verify: journal, readiness file, fan floor readback, CPU policies uniform, GPU measured clock ≤ ceiling | All true within 60 s |
| 6 | Observe 10 min idle | No abort; evidence fresh; temperatures stable |
| 7 | Record the known state in `doc/`, release the claim | Owner `energy_control`, caps, fan floor written down |

Why this order: the GPU owner treats ownership as exclusive only once every
known legacy writer is masked, so the legacy units must be fenced *before*
the service starts. The gap (a few seconds at idle) is covered by firmware
thermal protection; the legacy guard's last CPU caps stay in place meanwhile.

The sudoers rule `/etc/sudoers.d/spark-cpu-thermal-guard` permits only
start/stop/restart/enable/disable, not unmask, so it cannot bring a masked
unit back; it is left in place until the dashboard toggle is reworked under
the Spark_Dashboard `AGENTS.md`. The unit's `Conflicts=` also stops
`energy_control` if a legacy unit is ever started again (rollback path).

Boot ordering is implemented in `Spark_Dashboard/scripts/start-selected-vllm.sh`
(used by the boot stack and by every Manager start/restart of vLLM): while
`energy_control` is enabled, vLLM launches only when `energy_control.service`
is **active** and `/run/spark-energy/entry-ceiling` carries the current boot
ID (up to 180 s, then refuse with the missing condition named). The
liveness check was added 27 September 2026, 11:38 (backup
`Spark_Dashboard/backups/20260927-113838/`), so a hard-killed service's stale
readiness file cannot open the gate.

**Abort path during the block:** any failed check → rollback (section 3).

## 3. Rollback

1. Stop `energy_control` (its owners leave the safe state: GPU 200–500 MHz,
   CPU hardware minimum, fan floor 12).
2. **Legacy services decommissioned 27 September 2026** (programs, sudoers
   rule and unit-file backup removed; byte-identical copies in
   `Archive/snapshot-2026-09-25T17-12-00-622Z/sources/`, see
   `Archive/decommissioned-2026-09-27.md`). A rollback is now a *reviewed
   manual reinstall*: copy `usr/local/libexec/spark-cpu-thermal-guard.js`,
   `usr/local/sbin/dgx-fan-control`, `etc/systemd/system/{spark-cpu-thermal-guard,dgx-fan-max}.service`
   and the drop-in `10-cap-mode.conf` back to their origin paths (modes from
   the manifest), `systemctl unmask spark-cpu-thermal-guard dgx-fan-max`,
   `systemctl daemon-reload`, then `systemctl start dgx-fan-max` (floor 12).
   Never install from the archive automatically.
3. `systemctl start spark-cpu-thermal-guard`.
   Its `ExecStartPre` re-applies the 250–500 MHz GPU lock — an acceptable safe
   fallback; it then rewrites all CPU policies every 500 ms.
4. Restore the sudoers rule and the dashboard toggle only if the rollback is
   permanent.
5. Record the rollback and its reason; keep the claim until the legacy state
   is verified.

**Return to energy_control** (tested 27 September 2026): stop both legacy
units, verify the files in `/etc/systemd/system` are identical to the backup
(`cmp`), remove them (and the drop-in directory), `systemctl daemon-reload`,
`systemctl mask spark-cpu-thermal-guard dgx-fan-max`, then
`systemctl start energy_control` (it establishes the CPU baseline itself and
applies the entry ceiling before writing the readiness file).

## 4. Boot ordering

GPU locks do not survive reboot or driver reset.

- `energy_control.service` (system) applies the GPU entry ceiling at boot,
  logs it, and only then writes `/run/spark-energy/entry-ceiling` (boot ID,
  ceiling, monotonic and wall time, driver epoch).
- `spark-stack-boot.service` (user) → `Spark_Dashboard/scripts/boot_stack.sh`
  must wait (bounded, e.g. 120 s) for that file with the current boot ID
  before starting the RAM guard and vLLM, and refuse to start vLLM if it does
  not appear. This change belongs to `Spark_Dashboard/` under its own
  `AGENTS.md`.
- After a driver reset or resume, the service re-applies the entry ceiling
  and rewrites the readiness file before admitting owned work.

## 5. After the handoff

No manual `nvidia-smi` clock, cpufreq or `dgx-fan-control` commands. The
commanding agent changes settings only through the password-confirmed broker
or CLI. Status readers of `/run/spark-cpu-thermal-guard/status.json`
(Spark Dashboard, HostApp tools) move to the energy_control API.

## Legacy clean-up rule (27 September 2026)

When legacy or vendor units are deprecated (files deleted, packages purged),
**keep or re-create their masks** (`systemctl mask <unit>`): energy_control's
GPU ownership check requires every unit in `energy_control/handoff_probe.py`
`UNITS` (spark-cpu-thermal-guard, dgx-fan-max, dgx-fan-control,
nv-cpu-governor) to be a symlink to `/dev/null`. `dpkg --purge` removes a
package's mask; re-mask immediately afterwards (doc/42 defect 25).
