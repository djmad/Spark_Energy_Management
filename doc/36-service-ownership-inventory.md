# Service ownership inventory and handoff — 26 September 2026

Point-in-time, read-only inventory taken at about 15:02 CEST at the operator's
request: which running services or tools could override `energy_control`
once it owns the GPU ceiling, CPU maxima and fan floor. Nothing was stopped,
disabled, restarted or written. The handoff requirements are part of
[goal v2](../goal.md).

## How it was checked

- System and `operator` user units: `systemctl list-units`, `list-unit-files`,
  `list-timers`, `systemctl cat` and `show` (ExecStart only, no environment).
- Writer search: GPU clock lock/reset, power limit, cpufreq governor/min/max
  and fan-state commands in cron, udev rules, sudoers, systemd drop-ins,
  `/usr/local/{bin,sbin,libexec}`, `Spark_Dashboard`, `HostApp/tools`,
  `AI_MODELS/spark-vllm-docker` and `start_scripts`.
- String search of the root NVIDIA daemon binaries for clock, power and
  cpufreq setter names. This is a scoped check, not proof that they cannot write.
- Current actuator state: cpufreq sysfs, the legacy guard status file,
  `nvidia-smi --query-gpu`, `docker inspect`. The fan floor and RPM were
  **not** re-read, to avoid unnecessary EC transactions.

## Overriding writers: must be stopped, disabled and masked at handoff

| Unit / path | What it writes | Why it would override |
| --- | --- | --- |
| `spark-cpu-thermal-guard.service` (system, root, enabled; PID 2818 since 25 Sep 21:05, 0 restarts) | Governor and `scaling_min/max_freq` on all 20 CPU policies every 500 ms; `ExecStartPre` runs `nvidia-smi -i 0 --lock-gpu-clocks=250,500` at every start | Continuous CPU writer. `Restart=always`, `RestartSec=1`: if its process dies, systemd restarts it within about 1 s and locks the GPU again. Only `systemctl stop` plus disable/mask removes it. It has no exit handler, so its last CPU caps stay after stop. |
| `/etc/sudoers.d/spark-cpu-thermal-guard` | Lets `operator` start/stop/restart/enable/disable the guard without a password | A second control route that can bring the old guard back. |
| `Spark_Dashboard/app.py` autostart toggle (`CPU_THERMAL_GUARD`, around line 946) | Enables/disables the guard through the sudoers rule | Can re-enable the legacy guard for the next boot. |
| `dgx-fan-max.service` (system, oneshot, enabled, active/exited) | Boot: `dgx-fan-control set-state 12`; stop: `dgx-fan-control automatic` | Second fan-floor writer. Stopping it removes the floor, so energy_control must write its own floor right after. Firmware keeps cooling in between. |

Current state of these writers:
- **Legacy guard status:** target 93 °C; hottest zone `thermal_zone0` at 42.1 °C; all cap ratios 1, so CPU maxima are at the hardware maximum.
- **CPU policies:** 10 fast at 1378–3900 MHz and 10 slow at 338–2808 MHz, `conservative` governor.

## Dormant writers: keep disabled or masked

| Unit | State | Risk |
| --- | --- | --- |
| `nvfancontrol.service` | disabled | NVIDIA fan daemon, a competing fan writer |
| `nv-cpu-governor.service` | masked | competing CPU governor writer |
| `nvidia-enable-power-meter-cap.service` | masked | power-cap behaviour not reviewed |
| `gamemoded.service` (user) | disabled | can change the CPU governor |

## Manual writers: never run after handoff

- `AI_MODELS/spark-vllm-docker/README.md` known issue 1 recommends `sudo nvidia-smi -lgc 200,2150`. That is above the 1800 MHz hard limit and would compete with the GPU owner.
  - The same note independently reports sudden shutdowns during heavy inference that were fixed by lowering the maximum GPU clock. It says the default is about 2411 MHz with boost to about 3000 MHz, and that the setting lasts only until the next reboot.
- `HostApp/tools/cpu_thermal_guard.js` (the source of the installed guard), `standalone_cpu_thermal_fast_first.js` and `cpu_thermal_pid_test.js` write cpufreq limits when run.
- Any manual `nvidia-smi` clock command or `dgx-fan-control` call.

## Leave running: no setter found in scope

- `vllm_node`: resident model. Not privileged; its only added capability is `CAP_IPC_LOCK`.
- `open-webui` and `hostapp-gateway` containers: not privileged, no added capabilities or devices.
- `nvidia-persistenced`, `nvidia-dgx-telemetry`, `dgx-dashboard`, `dgx-dashboard-admin` (root): the binary string search found no clock, power or cpufreq setters.
- `hostapp-ram-guard` and `hostapp-resource-supervisor`: separate memory protection. Never manage or stop them.
- `spark-vllm-bridge` (byte relay), `spark-dashboard` (reads the legacy status file and cpufreq sysfs), `rasdaemon` (useful crash evidence).

Unowned load that can't be cancelled, only capped:
- the HostApp research timers and services;
- `hostapp-server`;
- Sunshine (GPU streaming host);
- other LLM clients (open-webui, the gateway).

The controller treats these as background demand. Test abort cancels only owned test loads.

## Boot-order gap

GPU clock locks do not survive a reboot or driver reset.
- **Today:** the legacy guard's `ExecStartPre` applies 250–500 MHz at boot.
- **Model start:** `spark-stack-boot.service` (user) runs `Spark_Dashboard/scripts/boot_stack.sh`. When VLLM_STACK autostart is on, it starts the RAM guard and then vLLM. Its only ordering is against user units, so nothing orders it after a system-level GPU cap.
- **After handoff:** energy_control must apply and log the GPU entry ceiling at boot, before the model starts. The boot stack must wait for that readiness. That change belongs to the Spark_Dashboard project and must follow its own instructions.

Status consumers that must move to the energy_control API:
- `Spark_Dashboard/app.py` (reads `/run/spark-cpu-thermal-guard/status.json`, around line 571);
- HostApp tools that read the same file, such as `att181_campaign_supervisor.py` and `cpu_thermal_pid_test.js`.

## Handoff requirements

1. Do not stop the legacy guard until energy_control can take CPU control in the same supervised block. Until then, the legacy guard is the only CPU thermal control apart from firmware, and the CPU crashes at 96 °C.
2. Run the handoff as one operator-approved block, with the LLM idle and no test load:
   - start energy_control observing;
   - stop, disable and mask the legacy guard;
   - write and read back all 20 CPU policies;
   - confirm the GPU entry ceiling (setter acknowledgement plus measured clocks);
   - stop, disable and mask `dgx-fan-max`;
   - set and read back the fan floor, starting at 12;
   - remove or disable the sudoers rule and the dashboard toggle.
3. **Rollback:** unmask and start `dgx-fan-max` (floor 12), then the legacy guard. Its start re-applies the 250–500 MHz GPU lock, which is an acceptable safe fallback. Restore the sudoers rule and dashboard toggle only if the rollback is permanent.
4. Record every step with its intent before the action and its outcome after, in the commissioning log.
