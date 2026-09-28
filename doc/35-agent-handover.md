# Agent handover — 26 September 2026

> **Superseded status (26 September 2026):** [goal v2](../goal.md) replaces the paused goal below. It is set but not started; begin work only on the operator's explicit start instruction. The implementation notes in this handover still apply.

**Final user direction:** save the goal and stop goal work. The goal is now
being paused at the user's request; resume only with explicit instruction.
See `../goal.md` for the full stored objective and superseding constraints.
No further implementation or tests were performed after this handover request.

## User intent and operating contract

Deliver one `energy_control.service` coordinating CPU slow/fast maxima, GPU
maximum and modular additive fan control, with unprivileged graph/parameter API
and a constrained privileged broker. The goal was explicitly resumed earlier.
The latest user request is to write this handover, not to run another trial.
Do not mark the goal complete: deployment and hardware qualification are missing.

- Lenovo ThinkStation PGX / GB10 is development hardware. CPU/GPU share a copper
  thermal mass and two fans; the plant must model heat capacity and fan extraction.
- **1800 MHz is the hard development GPU ceiling**, not a final optimized value.
- Primary reported instability: unrestricted GPU clocks combined with sudden
  100% load, often cold/idle-to-prefill. Do not reproduce uncapped failure.
- Enforce entry limits before dispatch; gradual normal ramp-up/down. Emergency
  requests a **500 MHz GPU maximum** and cancels owned workloads.
- **93°C is a hard abort boundary**, with earlier predictive action. Never
  promise protection against unobserved peaks.
- Keep vLLM resident. Normal stop cancels owned active/queued prompts; it does
  not stop/restart the model or invoke emergency capping by itself.
- No engine modifications/terminal receipt instrumentation are required for
  the explicit HTTP-observed path. Retain uncertainty about server/GPU drain;
  socket closure is not proof of it. Do not recreate this as an external blocker.
- GPU matrix burn-in requires a separate dedicated go and remains excluded.
  Never combine it with the resident LLM. No archived script was executed here.
- RAM protection is a separate project; do not manage or stop it.
- User authorized replacing/stopping the legacy CPU PID **when successor,
  handoff and rollback are ready**. No overlapping actuator owners.
- Existing fan module is sufficient; only additive floor state 0–12 is exposed.
  Firmware retains full cooling authority. Fan-max stop removes its floor;
  legacy CPU service restart applies GPU 250–500 MHz in ExecStartPre.
- Prefer fewer meaningful hardware blocks, not many tiny stress experiments.

## Workspace rules

Working directory: `<project>`.
Read `AGENTS.md`, `README.md`, and `doc/01-findings.md` before implementing.
Archive is immutable evidence, never an installation/automatic execution source.
Use `apply_patch`; preserve user changes. No subagents unless explicitly asked.
Do not copy credentials, environment files, prompt bodies or broad logs.
Keep requested, measured, accepted and hardware-maximum clocks distinct.
Validate fake actuators/replay first. No qualified unified service is installed.

## Last verified runtime (point-in-time, not current guarantees)

- `spark-cpu-thermal-guard.service`: loaded, active/running.
- `dgx-fan-max.service`: loaded, active/exited.
- `energy_control.service`: not found.
- Container `acf72b23162f`, name `vllm_node`: running, observed up 19 minutes.
- `GET http://127.0.0.1:8000/health`: HTTP 200. No prompts submitted.
- Three-second read-only host-sampler diagnostic: 54 guard-side reads (includes
  cached frames), maximum sensor age ~0.236327 s, maximum read ~0.000336 s,
  sampler child reaped. No actuator proofs supplied.
- Read-only guard/source smoke exited 2 and observed abort because requested
  GPU cap/proof was absent. Its reason was `GPU requested limit outside hard
  envelope`; this was an expected missing-proof refusal, **not a measured GPU
  overclock or actual thermal emergency**. Zero owned loads/actuator callbacks.
- No recent real GPU/CPU/fan writes, model lifecycle operations or stress loads.
  Historical 200–1200 MHz setter acknowledgement is not fresh enforcement proof.

## Verified software checkpoint

The last full regression completed **466 tests successfully**. It preceded
the new GPU recorder-channel work described below. Subsequent focused checks:

- GPU recorder channel: 3 tests passed, including a spawned GPU owner using
  the supervisor's one run/log for normal 1200 and emergency 500 fake commands.
- GPU setter: 11 tests passed after allowing the narrowly typed recorder client.
- Latest headless scenario: 30 s queue, 5 CPU cores, 12 waiting/4 active jobs,
  injected prefill at 10 s; synthetic maximum cap 1800 MHz, no abort. This is
  synthetic, not thermal calibration or measured stability.

Run `python3 -m unittest discover -s tests -v` and a headless scenario after
integrating changes. The suite has a known warning from legacy explicit-fork
fake tests; new process paths use spawn.

## Important implementation pieces

### Resident requests and cancellation

- `http_llm_transport.py`: opt-in loopback streaming HTTP transport. Retains
  raw socket for cancellation even after HTTPConnection detaches it; disables
  auto-reconnect. Connect timeout .5 s; request deadline 180 s. No body logs.
- `process_http_transport.py`: spawned socket-owning workers observe a shared
  run abort event independently of policy. `GuardHttpCancellation` sets that
  event and returns false (no claim of server drain).
- `request_gateway.py`: durable admission/dispatch before start, optional
  entry-cap context, explicit `http_observed` completion alongside strict mode.
- `admission.py` / `resident_abort.py`: normal stop closes admission/cancels
  owned work; reports client counts separately from server-unverified evidence.
- `guard_ownership_process.py`: independent heartbeat/safety/deadline guard,
  explicit HTTP client-done IPC releases client slots but retains owned IDs
  (bound 4096). Unresolved ownership prevents verified-clean disarm. Strict mode
  still requires its stronger terminal evidence. Optional context-managed
  sampler factory constructs resources in the guard child with bounded startup
  and cleanup.
- Queued cancellation was fixed: a canceled process request completes locally
  even if start is skipped. HTTP mode does not ask the guard to falsely verify
  a never-started client assertion; uncertainty is retained conservatively.

### GPU owner, evidence and logging

- `gpu_command.py`: fixed nvidia-smi command, durable intent/outcome, ownership
  checks, bounded wait and uncertainty latch. Shared abort event fences normal
  writes. `apply_emergency()` requests fixed 200–500 through the **same writer**;
  refuses overlap and retries after uncertain driver failure. A mere normal
  fence does not poison a known-completed writer before its one emergency try.
- `gpu_owner_process.py`: isolated single-writer loop. Trusted factory creates
  child-local setter/recorder client; bounded binary cap commands, max 128 normal
  commands per session. Abort/EOF/invalid frame leads to the same owner's one
  emergency operation. Exit 0 means setter acknowledgement, not measured cap
  enforcement or a clean commissioning run. No live factory is supplied.
- `gpu_evidence_channel.py`: private bounded datagrams, immutable context,
  freshness/order checks and invalidation. Original completion time preserved.
  Owner invalidates before new commands and refreshes evidence ~100 ms while
  idle. Reader retains ordering history across null frames. Not numeric cap
  readback or authentication independent of the private socket topology.
- `gpu_recorder_channel.py`: NEW bounded private socket RPC to the supervisor's
  existing `CommissioningRecorder`. Whitelisted bind/ready/GPU intent/outcome/
  abort only; no paths/listener/arbitrary methods. Client poisons on failure,
  never retries. Server must run in recorder's creator process (e.g. thread).
  This fixes the prototype's separate-child-run-ID problem; recorder file
  handles must not be inherited across processes.
- Tests: `test_gpu_recorder_channel.py` includes `remote_logged_setter`, a
  fake-only context-managed child factory, and a real spawned owner/shared-log
  check. Do not use its fake ownership callback for hardware.

### CPU, fan and combined abort

- `cpu_workload_process.py`: independently owns one launched new-session test
  group; waits for GO, supports normal STOP, abort event, deadline and EOF.
  Verifies termination/reaps child. Real launcher not selected; launch/initial
  registration hang window still requires containment/qualification.
- `cpu_frequency.py`, `fan.py`: pinned Lenovo adapters. Live sysfs writes require
  explicit boolean opt-in. Fan fake-root redirection into `/sys` is rejected.
- `unified_actuation.py`: serialized CPU/GPU/fan transaction ordering; now
  retains last committed proposal and checks CPU policies, floor and GPU evidence
  before subsequent writes. Drift aborts rather than overwrites another writer.
- `test_combined_abort_processes.py`: actual guard heartbeat loss cancels dummy
  HTTP, terminates sleeping CPU child, and causes fake GPU emergency. A blocked
  fake GPU command does not delay HTTP/CPU cancellation. No hardware workload.

### Live read-only guard source

- `host_sampler.py`: isolated host collector, slope caching/age propagation;
  `SetterBackedSafetySampler` joins GPU evidence without fabricating CPU/fan/
  workload health. Requested/accepted/measured fields remain separate.
- `guard_host_source.py`: child-local context-managed sampler warmup/cleanup,
  optional evidence input, read-only diagnostic. Does not provide other actuator
  ownership proofs. `python3 -m energy_control.guard_host_source` is the bounded
  read-only smoke described above, not a commissioning command.

## LAST EDIT — unfinished and untested

> **Resolved 26 September 2026 (goal v2, step 1):** the session was finished
> and fake-tested; see [doc/38](38-goal-v2-progress.md). The review list below
> is kept as history.

`energy_control/gpu_owner_session.py` was **just created** before the user asked
for handover. It has not been imported/tested or integrated into the controller.
Do not treat it as ready for hardware.

Its intent is a supervisor-side lifecycle wrapper around the already-tested
owner, private recorder RPC and evidence channel. It offers `start`, `apply`,
`read`, `close`, and `exitcode`; accepts the main recorder, trusted factory,
shared abort event and driver/owner epochs. Factory signature is recorder
socket, run ID, boot ID, abort event. It reconstructs a SetterAttempt only after
acknowledgement plus matching evidence, latches failures, and requests emergency
on close without forcibly killing uncertain driver work.

Review/test before extending:

1. Startup failure cleanup and partial resource allocation; logger/thread/socket
   lifetimes when process spawn fails, owner hangs, or parent stops reading.
2. It currently has only **one evidence receiver** consumed by its own `read()`.
   The independent guard needs a separate private evidence feed/cache; do not
   share a consuming datagram socket between guard and policy. Concurrent calls
   to its reader also need proper ownership/serialization.
3. Evidence backlog expires/faults if not consumed regularly. Wire continuous
   observation, not only reads after occasional cap commands.
4. Two-second command/RPC observation limits can be exceeded by combined disk
   and driver latency; preserve uncertainty/no retry rather than claiming success.
5. `close()` can return false while exact child/logger resources remain live;
   caller must observe/reconcile them, not restart or discard ownership.
6. Readiness is IPC readiness only. It does not establish the initial cap,
   competing-writer fencing, reset epoch, lease or hardware qualification.
7. Run the full regression after adding focused session tests. The last full
   count of 466 predates recorder-channel and session additions.

## Next meaningful work / remaining delivery

Use `doc/27-readiness-audit.md` for the full objective audit and
`doc/34-resident-http-transport.md` for detailed integration evidence/limitations.
The former was corrected to remove stale engine-modification and model-start
prerequisites from the resident-model path.

Immediate integration path:

1. Finish/test the GPU owner-session wrapper and separate evidence delivery to
   policy and guard, using one run/log/lease.
2. Compose one resident-run supervisor with independent guard, CPU-load owner,
   isolated request dispatcher and coordinated CPU/GPU/fan policy. Bind config,
   workload budgets, durable events and all epochs throughout.
3. Implement real CPU/fan/GPU handoff observations and rollback. The callback
   interfaces currently accept trusted proof but no qualified deployment source
   establishes that proof. Do not substitute booleans or service status alone.
4. Review and perform an explicit bounded live handoff/admission block only
   after complete fake/replay integration. Keep existing protections until the
   replacement is actually ready. Leave vLLM and RAM guard resident.
5. Collect staged separate identification/holdout traces, fit heat capacity,
   coupling/fan response/delays, tune coordinated PID/ramping, and qualify useful
   sustained combined workloads within the hard limits.
6. Finish the single installed service, privilege separation/auth provisioning,
   API/CLI, startup/reset protection, migration/rollback and operational docs.

Unfinished is not blocked: no user action or engine patch is presently required
to continue safe software integration. No hardware stability, calibrated model,
or production deployment has been claimed or achieved.
