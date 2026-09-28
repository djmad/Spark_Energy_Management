# Idle LLM stop qualification — failed/unverified command, LLM stopped

The operator reserved LLM slots and explicitly authorized LLM start/stop,
warning startup can take minutes. This check exercised only the emergency stop
adapter with an idle queue, not a thermal load, independent guard, or GPU drain.

The new, fsynced record is `idle-stop-qualification-20260926.jsonl`. It records
the exact pinned container identity and pre-stop observations: zero active and
waiting requests, hottest observed temperature 54.5°C, GPU 34°C at 968 MHz,
and fan floor 12. No CPU/GPU frequency or fan command was issued.

One Docker KILL command targeted the full pinned container ID. The command
wait exceeded 1 second; the runner recorded `stop_unverified` after 1.018s and
exited with failure. **No second stop or retry was issued.**

Subsequent independent read-only checks found:

- The full container ID could no longer be inspected (no such object).
- No `vllm_node` container appeared in the Docker all-container name query.
- Original init PID 48583 no longer existed.
- NVIDIA listed only `/usr/bin/sunshine` (PID 7022), no vLLM compute process.
- GPU was 34°C in the follow-up observation.

These observations support that the LLM stopped, but do not turn a timed-out
command into a successful synchronous acknowledgement or prove GPU drain.
Container auto-removal is the likely explanation for disappearance; its flag
was not captured before the action, so that cause is not proven. The adapter
incorrectly assumed stopped-container metadata would remain inspectable.
Future preflight now reads `HostConfig.AutoRemove` and refuses auto-removing
containers until a disappearance-verification path is separately qualified.

The LLM is currently down. No restart or recreation was attempted. Since the
container is absent, recovery requires the existing selected-model launcher
rather than `docker start` on the removed ID. No host model/source files were
deleted by this work; the removed container's writable layer is not available
through Docker inspection. Do not repeat this qualification automatically.

This is hardware-side adapter evidence, not a successful system qualification.
Next work must account for container lifecycle semantics and independently
enforce the <=1800 MHz startup ceiling before loading the model again.

## Recovery-path inspection

The existing dashboard launcher delegates to `Spark_Dashboard/vllm_catalog.py`,
which checks the RAM guard, resolves the selected recipe, invokes its build-only
step, then starts the recipe on loopback port 8000 as `vllm_node`. Its underlying
`spark-vllm-docker/launch-cluster.sh` explicitly uses `docker run ... --rm`.
This establishes auto-removal as a behavior of the normal launch path, consistent
with the observed container disappearance (the removed container's own flag was
not captured). Do not disable this globally or replace the user's launch policy
merely to accommodate our verifier.

Read-only selected-recipe resolution returned `gemma4-26b-a4b-nvfp4`, checkpoint
ready, zero missing checkpoints. This identifies the current selected recipe,
not retrospective proof of which recipe created the removed container. The
legacy CPU guard, maximum-fan service and user RAM guard remain active.

Recovery must establish a <=1800 MHz ceiling before loading/warmup and maintain
independent temperature/memory monitoring through the potentially minutes-long
startup. It must handle an automatically removed container on emergency stop,
retain the launched process/container identity, and not interpret HTTP timeout
alone as a failed boot. No launcher/build/download/restart has been executed yet.

Inspected source hashes:

- `scripts/start-selected-vllm.sh`: `a1a53664b3fba7ca8faaa8c118369be0d1fab9bf6d80dfab5475ea1d85c0afd9`
- `vllm_catalog.py`: `7c8889a2401edf1dc21bc2b10485fad9e06d1de9d4cd2a413efd69a4daf62570`
- `launch-cluster.sh`: `f75c30d8f64fc563247259969b913b3f2dc8b9ad91856a4779287dfb7ddf0e57`

## Service-manager log follow-up

At the operator's direction, read the existing manager's bounded vLLM log view:
`GET http://127.0.0.1:8799/api/services/vLLM/logs?lines=25`.
Its source is
`~/Documents/HostApp/tools/logs/vllm_launch.log`.
The tail contains successful metrics responses, “Cluster stopped”, and a
launcher subprocess exit status of 137. File modification time was
2026-09-26 12:24:58 +0200. This is consistent with the explicit KILL above;
137 alone does not establish OOM, thermal failure, or a separate failed boot.
The last manager start-request marker found is from the previous day; log
ordering alone does not establish a new launch. Follow-up Docker inventory
still shows no container, and loopback port 8000 refuses connections.
No manager start/stop endpoint was invoked in this follow-up.

The offline adapter now has an auto-removal verification path: bind the
original full container ID and unified cgroup before stopping; require successful
Docker inventories showing neither that ID nor a same-name replacement; require
the original PID absent and its cgroup absent or recursively unpopulated.
Docker errors, invalid inventories and remaining processes do not count as
successful stops. This remains fake-tested, not live-qualified, and does not
prove residual GPU work drained. The real qualification script's auto-removal
refusal remains in place pending integration. No second stop was issued.

## Later operator-authorized manager-API recovery

The operator requested using the service manager API, not a separate launcher.
Exactly one `POST /api/services/vLLM/start` was sent to loopback port 8799.
The manager returned success and reported background model loading. No recipe,
model selection, sequence count, start script or service installation was changed.

Before that request, the driver acknowledged a `200,1200` MHz lock; intent and
result were synchronously logged in `manager-recovery-clock-20260926.jsonl`.
This is setter acknowledgement, not numeric accepted-cap readback. The setting
remains in place. The 1800 MHz hard ceiling was never intentionally exceeded.
CPU guard PID 2818/restart count zero, maximum fan service and RAM guard were
active before startup; none was stopped or reconfigured by this recovery.

An operational watch (`analysis/recovery_watch.py`) used isolated sensor
acquisition, an 80°C absolute cutoff, a two-second projection against 88°C,
8 GiB memory cutoff and the 1800 MHz hard measured-clock boundary. It is not a
qualified production guard. An initial idle watch exited before any start,
without a bound container; its exact failing sample was not recorded, so its
cause cannot be established retrospectively. The very short difference window
was identified as a sensitivity problem in code review. The revised watch uses
at least a one-second difference for prediction and records failing readings.
Its second log is `manager-recovery-watch2-20260926.jsonl`.

During the single API-start attempt the hottest CPU-proxy sensor rose from
53.1°C to 66.8°C in approximately 1.02 seconds. The predictive rule subsequently
tripped with an observed maximum of 67.9°C, GPU 35°C at 1144 MHz, about 86.4 GiB
available memory, and healthy fan floor 12. The watch issued one KILL targeting
its bound full container ID; Docker acknowledged it. Follow-up inventory found
no vllm_node container. The manager log ended in exit 137, consistent with this
protective stop. This was not a demonstrated thermal crash, actual 93°C breach,
proof of residual GPU drain, or successful LLM startup qualification.

LLM is down again. No automatic retry is scheduled. Review the observed
CPU-side startup transient and uncalibrated linear projection before another
attempt; do not silently relax the safety threshold. The watcher does not
provide complete production failure containment (including parent death,
blocking disk I/O, or startup failure before binding a container identity).
No inference request, CPU stress workload or matrix burn-in was submitted.

## Trace review and operator CPU-startup clarification

The second watch saved 50 temperature records over 49.025 seconds (including
pre-launch observation). Logged maxima were CPU proxy 67.9°C, GPU 35°C and
measured GPU clock 1144 MHz; minimum logged available memory was 86.440 GiB.
The final two regular CPU-proxy samples imply approximately 13.482°C/s using
record timestamps. This is an approximate trace statistic, not the exact
derivative used by the watch: acquisition timestamps and the predictor's
reference sample were not retained in that version. The trace cannot prove
that temperatures would actually have reached the projected boundary.

Offline follow-up now retains the exact sensor, acquisition timestamps,
reference temperature, time difference, rise rate, prediction horizon and
projected temperature in an abort decision. The stop signal precedes abort
logging; a newly added fake disk-failure test verifies that ordering. Six
focused recovery/container/abort tests passed. No further hardware attempt
or threshold relaxation accompanied this change. Regular synchronous logging
can still block supervision: this is not a qualified independent guard.

The operator reports that LLM startup necessarily activates at least one CPU
performance core for calculations the GB10 GPU cannot perform. Treat this as
a startup workload requirement, not an unexpected competing workload. Exact
core affinity, utilization duration and the claimed operation/offload reason
have not been independently measured. The observed CPU-proxy transient is
consistent with CPU startup work but does not identify a specific core.

Consequences for the unified controller:

- Model launch/warmup as combined GPU work plus at least one busy performance
  core; do not model it as a GPU-only admission event.
- Establish a bounded CPU-fast startup ceiling as well as the GPU ceiling
  before the existing manager start request. Keep CPU-slow separately bounded.
- Integrate this envelope with the CPU PID's sole actuator owner; do not race
  an extra sysfs writer against the existing 500 ms legacy PID loop.
- Track per-class requested maxima and per-core utilization during startup,
  then recover CPU headroom gradually with thermal margin. Startup duration
  and safe CPU ceiling remain uncalibrated; no new numeric limit is inferred
  from this short trace.
- Keep model-loading readiness distinct from request prefill, and retain
  independent hard/predictive aborts. Do not disable CPU protection merely
  because this workload is expected.

## Offline coordinated startup envelope implemented

`Observation.model_loading` and `PolicyInput.model_loading` now keep the
existing supervisor inside its CPU/GPU entry ceilings for the entire model
loading phase. A one-tick CPU arrival event is insufficient for a boot lasting
minutes. Loading requires an explicit active CPU-demand signal, requests the
maximum additive fan floor, clears GPU busy-dwell accumulation and never raises
an already-derated ceiling. Thermal protection remains higher priority.
After loading ends, existing CPU recovery and GPU busy-dwell/ramp limits apply;
the PID instances are not restarted. All outputs remain unqualified proposals.

The focused policy suite passed 15 tests, including a synthetic two-minute
loading block at 100% GPU utilization, separate CPU class ceilings, bounded
post-readiness recovery, invalid/missing CPU workload signals and a 93°C abort.
This validates control logic, not thermal behavior or per-core heat parameters.

Live integration still must assert loading before the existing manager API
request, establish both actuator ceilings under a single owner, and clear it
only on fresh readiness for the same model/container generation. The field
is not a public API permission or a replacement for a readiness verifier.
The current host CPU PID has not been modified or replaced; no live startup
envelope or new LLM start was applied in this implementation step.

The commissioning record/replay path now preserves optional `model_loading`
evidence and records STARTUP decisions with the fixed `model_loading` reason
code. Once a replay sees explicit lifecycle evidence, losing it latches an
abort rather than releasing startup ceilings. Old traces lacking this field
retain legacy behavior; they do not qualify protected model startup. The
combined recorder, replay, decision-log and policy block passed 49 tests.

The host/safety sample assembler now carries this lifecycle field explicitly.
The policy itself (not just replay) distinguishes unknown from explicit false:
after lifecycle tracking starts, omitted evidence latches abort, and a config
edit or a later ready signal cannot clear that latch. Legacy samples with no
lifecycle tracking remain insufficient to qualify protected startup.

Read-only inspection of the installed CPU PID confirms there is no dynamic
startup-envelope input: it computes its ratio internally each 500 ms and
reasserts policy maxima. A separate sysfs cap write would be overwritten.
Do not use that as a startup mechanism. The next live integration requires
either a reviewed single-owner successor handoff or an explicit envelope input
inside the current owner's control loop, with independent supervision and
readback before manager admission. Neither has been deployed.

`GuardOwnershipProcess` now defaults to a fresh-interpreter spawn instead of
fork. A focused test starts it while a parent thread is alive, registers and
authorizes fake owned work, then verifies the child aborts on channel closure.
Unserializable callbacks fail closed without silently falling back to fork.
Callback resources are retained until child exit so synchronization handles
survive spawn setup. Older closure-based fake tests explicitly select the
legacy fork seam; this does not qualify that seam for multithreaded deployment.
Child-local live resource construction and the real abort path still require
integration and hardware qualification.
