# Resident supervisor — goal v2, step 4

Status: implemented and fake-integration tested on 26 September 2026. No live
factory is supplied; nothing here touches hardware, services or vLLM.

## Composition

`energy_control/supervisor.py` (`ResidentSupervisor`) builds one run from:

| Part | Module | Isolation |
| --- | --- | --- |
| Durable run recorder | `recorder.py` | Supervisor process; owners log through a private RPC (`gpu_recorder_channel.py`) |
| GPU clock owner | `gpu_owner_session.py` → `gpu_owner_process.py` | Spawned child, single writer |
| CPU maxima owner | `limit_owner_process.py` (`kind="cpu"`) | Spawned child, single writer |
| Fan-floor owner | `limit_owner_process.py` (`kind="fan"`) | Spawned child, single writer (EC I/O cannot block CPU/GPU) |
| Independent guard | `guard_ownership_process.py` + `guard_host_source.py` | Spawned child with its own sensor sampler and evidence feeds |
| Coordinated policy | `policy.py` (`ShadowPolicy` → `simulation/model.py`) | Supervisor process |
| Owned LLM requests | `request_gateway.py` + `process_http_transport.py` | Worker threads + spawned HTTP workers |

One spawn-context **abort event** is shared by all owners and HTTP workers and
is the guard's cancellation route. Guard abort, missed heartbeats, supervisor
death, an owner fault, a dispatcher fault or a policy abort all converge on
it. Each owner then runs its own emergency action in parallel:

- GPU: fixed 200–500 MHz request through the same writer;
- CPU: every policy maximum to its hardware minimum (fast 1378, slow 338 MHz);
- fan: additive floor 12.

Nothing resumes automatically.

## Evidence feeds

Every owner publishes private datagrams to two feeds: one drained by its
supervisor-side session (policy), one read by the guard. No consuming socket
is shared.

- GPU: typed setter evidence (`gpu_evidence.py`), refreshed every 100 ms.
- CPU: readback of all 20 policies, required uniform per class and equal to
  the last command (`limit_evidence.py`), every 100 ms, max age 0.5 s. Any
  other writer changing a policy trips abort (drift detection) — so the
  legacy guard must be stopped in the same block the owner takes over.
- Fan: floor readback every 500 ms, max age 2 s (readback may cost an EC
  transaction).

The guard joins them onto its own thermal frames
(`OwnedActuatorSafetySampler`): missing GPU evidence makes the frame
unavailable; missing CPU/fan evidence reports that actuator unhealthy; fan
health also needs the RPM sensor. Workload-control health is the intact
shared abort route (`AbortRouteHealthy`), not proof of server drain.

**Transitions.** Owners publish `None` while a command is in flight. Guard
readers use `hold_through_transition=True`: the last verified readback stays
valid within its normal freshness window, so a normal command does not trip
the guard but a hung one still does. (Found in integration: without it the
guard aborted whenever it sampled inside a command window.) Guard readers
also accept a start-up backlog of up to 64 frames.

## Start and control cycle

`start()`: owners → GPU entry ceiling (logged intent) → fan floor 12 → read the
CPU state → guard (bound to run ID, trial plan and GPU context) → first
heartbeat → arm request admission. The config must be exactly the one bound
by the durable trial plan (digest, ceilings, fan minimum).

`tick()`: heartbeat → join owner evidence into the policy snapshot → policy
step → apply changed limits, protective first (lower clocks, more fan) and
the rest after. Any exception, refusal or policy abort latches the shared
abort. GPU commands are quantized (25 MHz) to fit the owner's budget.

**Owned requests** (`attach_dispatcher`): each is registered with the guard,
durably admitted, dispatch-intent logged, then the entry hook applies and
verifies the GPU entry ceiling while holding the control lock across the
guard's start authorization. The next tick re-arms the ramp (`REARM`).

`close()`: latches abort, so every owner leaves its safe state (GPU 200–500,
CPU minimum, fan 12); returns exit codes, `None` for anything still live.

## Emergency reductions without a disk

Goal v2 requires durable intent before increases and admissions. The GPU
setter now sends its one emergency reduction even when the recorder is
unreachable (e.g. the supervisor died with it) and logs best-effort. A
recorder refusal before spawn no longer blocks that emergency either. An
uncertain *driver* outcome (failed, timed-out or unreaped command) still
forbids it (`_driver_uncertain`), as before.

## Fake integration tests

`tests/test_resident_supervisor.py` (fake GPU setter, fake cpufreq and fan
sysfs trees, file-driven fake thermal sources, dummy local LLM server):

- entry ceiling before the guard, control ticks apply CPU caps, safe state on close;
- GPU ramp 1200→1400 MHz in 25 MHz commands without tripping the guard;
- owned request: entry ceiling re-applied after the dispatch intent and before
  start, `REARM` on the next tick, completion through the guard;
- guard thermal abort (GPU 86 °C) reaches every owner: GPU 500, CPU minimum, fan 12;
- missed heartbeats abort the run;
- supervisor process SIGKILL: orphaned owners reach CPU minimum and fan 12;
  the GPU emergency is sent without a durable intent;
- configuration not bound by the trial plan is refused.

`tests/test_limit_owner.py`: evidence validation, CPU raise intents, drift
detection, trial ceiling, fan floor, lost channel, separate guard feed.

## Open items before live use

- Live factories: hardware-enabled `LoggedGpuClockSetter` with a trusted
  ownership reader, `LenovoGb10CpuMaxima(allow_live_sysfs=True)`,
  `LenovoDgxFanFloor(allow_live_sysfs=True)`, and the live
  `guard_host_source`. None is wired yet.
- Service entrypoint, privilege split (root owners, unprivileged API) and
  installation under `/opt/spark-energy`.
- GPU owner 128-command budget: fine for bounded trials, not for the
  long-running service (see doc/38).
- A GPU increase window: the guard compares measured clocks with the last
  verified request until the new evidence arrives (tens of ms); a false abort
  there is fail-safe but should be checked against live traces.
