# Spark Energy Management — agent guide

Unified thermal/power controller (`energy_control`) for the Lenovo ThinkStation
PGX (NVIDIA GB10). One service owns the GPU clock ceiling, the CPU slow/fast
maxima and the additive fan floor. It prevents the cold idle-to-full-load
power-supply shutdown and holds GPU 75 °C / CPU 92 °C with coordinated PIDs
and a calibrated digital twin.

**Authority:** [`goal.md`](goal.md) (goal v2) is the objective and governs older
documents. This file adds the operator's standing hardware authorization below.
Where the two conflict about *who may act*, this file wins. Where they conflict
about *limits*, the stricter one wins.

Read before implementing: `goal.md`, `README.md`, `doc/01-findings.md`,
`doc/35-agent-handover.md` (implementation state and the unfinished
`gpu_owner_session.py`), and `doc/36-service-ownership-inventory.md`.

## Hardware authority (operator grant, 26 September 2026)

Agents have **full command over this machine's hardware within the limits
below**. You do not need a separate go for each block. This covers:

- GPU clock-lock writes, CPU cpufreq maxima, and the fan floor (states 0–12).
- The legacy-service handoff and its rollback.
- Identification traces, cold/idle-to-prefill trials at the entry ceiling,
  prefill-during-decode, and sustained combined-load qualification.
- Installing, starting, stopping and enabling `energy_control` services.

Act, log it, and report what you did. Stop and ask only when an action would
cross a limit, or when a limit's meaning is genuinely unclear.

### Only one commanding agent, ever

At most **one agent session at a time** may change GPU, CPU or fan settings.
This includes clock locks, cpufreq limits, the fan floor, and starting or
stopping the services that own them. Every other agent stays read-only.

- **Claim first.** Before your first hardware write, create
  `/run/spark-energy/agent-command` atomically (for example
  `mkdir -p /run/spark-energy && set -o noclobber && echo … > …`). Write your
  session ID, working directory, start time (ISO 8601) and purpose into it.
- **If the file already exists and is not yours, you are not the commander.**
  Stay read-only and tell the operator who holds it. Never delete, overwrite or
  "take over" another agent's claim, even if it looks stale. Only the operator
  releases a foreign claim.
- **One claim per session.** Do not pass command to subagents, parallel jobs or
  background scripts that outlive your session.
- **Root main agent only.** Only the operator's root (main) agent session may
  hold the claim or touch hardware at all. Subagents never write GPU, CPU or
  fan settings or start/stop their owning services, even when the operator
  allows subagents for software or read-only work (operator, 26 September 2026).
- **Release.** When you finish, leave the machine in a known state (owner,
  caps, fan floor), record it in `doc/`, then remove your claim. If you cannot
  reach a known state, keep the claim and report it.
- `/run` is cleared on reboot, and GPU locks are lost on reboot too. After a
  reboot, re-establish the entry ceiling before claiming anything else.
- **One hardware talker (operator, 27 September 2026).** Only
  `energy_control` talks to the GPU, CPU and fan hardware, for reads and for
  writes. Every other service, including Spark_Dashboard, reads
  `/run/spark-energy/status.json` only.
- **No restarts for tests.** Change settings live through the boot-bound
  override `/run/spark-energy/qualification.json` (root, current boot ID,
  any live Config field incl. `tuning`) or through the broker. Restart
  `energy_control` only to install code or model changes.
- **Once `energy_control` is installed,** it is the only actuator owner. The
  commanding agent then changes settings through its password-confirmed
  broker or CLI, not through direct `nvidia-smi` or sysfs writes.

## Hard limits (never cross; changing them needs the operator)

**GPU**
- Never command above **2500 MHz**. The operator raised this limit on
  27 September 2026: first from 1800 to 2200 MHz ("we expand our wattage the
  GPU can draw", end goal "productive 2200 MHz, maybe only 2100"), then the
  same evening to 2500 MHz ("Freigabe für Endfrequenzen bis 2.5 GHz (ich weiß,
  dass es hier möglicherweise crashen wird, aber das ist mein Risiko)"). The
  single source is `energy_control/limits.py`.
- Agents raise the production maximum only up to a step qualified by a run at
  that step (`GPU_QUALIFIED_MAX_MHZ`; ladder in 100 MHz steps, stop at the
  first failure). The operator may set any value up to the hard limit from
  the dashboard, at the operator's own risk. Never run at unrestricted/vendor
  clocks.
- A ceiling must be in place **before** any GPU work: at boot, after a driver
  reset or resume, and before model load. Load is detected by GPU utilisation
  only (operator, 28 September 2026: "we detect load only on GPU utilisation,
  prefill we don't need to look at any more, example is our burn-in test"):
  at idle the cap returns to the entry ceiling, and a new load ramps from
  there once the GPU is busy. A prompt that joins a running load does not
  re-arm the entry ceiling (`prefill_rearm` 0; 1 only for owned trials).
- Ramp up only in small, slow steps. Never catch up missed steps.
- Never deliberately reproduce the power-supply shutdown.

**Temperature abort**
- Abort on any ACPI zone ≥ **96 °C**, GPU ≥ **85 °C**, or an earlier predicted
  breach. The operator raised the ACPI abort from 93 to 96 °C on
  27 September 2026 ("wir legen den Abbruch auf 96 °C, unser Soll ist die 92
  im Moment"). The CPU crashes at about 96 °C, so the raw abort has no margin
  left; the projection acts about 2 s ahead of a fast rise. The single source
  is `energy_control/limits.py` (`ACPI_ABORT_C`).
- CPU target 92 °C. The shared CPU-cluster and TGPU zone ceiling is
  min(target, 96 − 3 − `trend_margin_c`), live-tunable down to a margin of
  1 °C.
- Abort action: GPU and CPU go to their lowest MHz, the fan floor goes to 12,
  and owned test loads are cancelled. Nothing resumes automatically.
- The GPU minimum stays at the fixed 200–500 MHz emergency request until the
  lowest accepted value is measured. The CPU minimum is fast 1378 MHz and
  slow 338 MHz.
- Targets must stay below the aborts. Only the operator raises the abort
  limits.

**Trials**
- A power-supply shutdown ends that qualification series.
- Never repeat a failed step automatically. Settle on the last passing value
  minus a margin.

**Workloads**
- vLLM stays resident: do not stop, restart, reload or modify the engine. A
  normal stop cancels only owned test prompts.
- Do not manage or stop RAM protection (`hostapp-ram-guard`,
  `hostapp-resource-supervisor`), NVIDIA daemons or `spark-vllm-bridge`.
- **GPU matrix burn-in is excluded** and never runs alongside the LLM. It needs
  its own go.

**Actuators**
- One writer per actuator. Never let two controllers write the same limit.
- The fan floor is additive only, through `dgx_ec_fan`. No raw EC writes.
- Firmware thermal protection stays intact.

**Handoff**
- Stop, disable and mask `spark-cpu-thermal-guard` and `dgx-fan-max` only in the
  same block in which the successor takes over, with rollback ready.
- Until then, the legacy guard remains the CPU thermal control.
- Rollback: restart `dgx-fan-max` and the legacy guard.

**Boot ordering**
- vLLM autostart must wait for the logged GPU entry ceiling. Make that change in
  `Spark_Dashboard/` under its own `AGENTS.md`.

## How to operate hardware

1. **Before each live block:** state the plan, the limits and the abort path in
   one short note. Check that the guard is running and telemetry is fresh.
2. **Before any upward clock change or load admission:** sync the intent to the
   durable commissioning log.
3. **Keep four clock values distinct:** requested, acknowledged (setter),
   measured, and hardware maximum. A setter acknowledgement is not proof of
   enforcement.
4. **Preserve incomplete and aborted runs.** Record run and boot IDs, phases,
   temperatures and slopes, clocks, fan floor and RPM, GPU power, estimated
   CPU power and decisions.
5. **Prefer a few meaningful blocks** over many tiny stress runs.
6. **Afterwards:** leave the machine in a known state (current owner, caps and
   fan floor) and write the evidence into `doc/`.

## Layout

| Path | Contents |
| --- | --- |
| `energy_control/` | Controller package: broker, guard, actuator owners (`gpu_command`, `cpu_frequency`, `fan`), request gateway, recorder, API/CLI, policy, replay. |
| `simulation/` | `model.py` physical model and pure controllers (no device I/O); `tui.py` curses UI and headless runner. |
| `analysis/` | Offline trace and qualification analysis tools. |
| `tests/` | `unittest` suite with fake actuators; `*_smoke.py` helpers. |
| `deploy/` | Staged systemd units (install reviewed code under `/opt/spark-energy`). |
| `doc/` | Numbered findings, design, evidence and runbooks. Add new ones as the next number. |
| `Archive/` | Immutable source snapshots and evidence. **Never execute**: it contains legacy installers. |
| `scripts/archive-baseline.mjs` | Allowlisted snapshot collector; creates a new snapshot, never refreshes one. |

## Engineering rules

- Python 3.12, standard library only.
- New process paths use `spawn`, not fork.
- The network API runs unprivileged and exposes no arbitrary commands or paths.
- The root broker enforces its safety envelope independently of mutable policy
  and API authentication. No configuration may weaken the guard.
- Operator configuration commits require password confirmation. Automatic
  protective actions do not.
- Graph history is memory-only: 15 min / 60 min / 1 day windows, at most 600
  buckets.
- Validate with fake actuators and trace replay before live use.
- Keep simulation exports separate from hardware commissioning evidence.
- Thermal constants and GPU gains in `simulation/` are synthetic until fitted.
  Fit on training traces and validate on separate holdout traces.
- Never write credentials, `.env` contents, prompt bodies, signing keys or
  broad logs into project files. When adding snapshots, preserve origin paths,
  hashes, licenses and authorship.
- Do not use subagents unless the operator asks.

## Validation

Run after every change:

```sh
python3 -m unittest discover -s tests -v
python3 -m simulation.tui --headless --scenario queue --seconds 30 --prefill-at 10
```

One known warning comes from legacy explicit-fork fake tests. Report the test
count and any failures as they are; do not claim hardware qualification from
fake or simulated results.
