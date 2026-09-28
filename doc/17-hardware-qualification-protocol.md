# Draft Lenovo hardware qualification protocol — NOT authorized to run

This is a predeclared sequence for later operator review, not a load runner or
approval to change hardware. All **load stages (1–7)** require the
[operator-approved GPU verification basis](28-operator-approved-gpu-verification.md)
at **<=1800 MHz**; an effective numeric lock getter is no longer mandatory.
The older numeric-reader requirements below describe the current implementation,
which must be adapted explicitly rather than fed fabricated readback values.
Stage 0 is observation of the existing system only; it
authorizes no new load or setting change.
The present Lenovo collector cannot do that. GPU matrix-multiplication burn-in
is excluded from this project and from every stage below; it requires separate
authorization. The archived scripts are evidence, not automatic test commands.
Two 120-second read-only aggregate windows have now been observed under the
pre-existing workload; see [passive baseline](21-passive-baseline.md). They
provide only partial Stage-0 context, not the sensor mapping or response
identification required to pass Stage 0 or open any load stage.
The seven zone-index/ACPI-path identities are now pinned in the read-only
collector ([sensor identity note](22-acpi-sensor-identity.md)); their physical
component mapping and timing remain unqualified.
Two further [passive ACPI value-change windows](23-passive-acpi-cadence.md)
show frequent changes in six zones but cannot establish firmware-internal
freshness or a maximum sensor delay. The seventh (`acpi_TGPU`) was nearly
constant. Acquisition age must not be mistaken for value age; this is an
additional Stage-0 qualification gap before a 93°C abort can be trusted.

## Gates that apply before every load stage (1–7)

1. The old and new CPU/GPU/fan writers have an explicit single-owner handoff;
   firmware cooling remains authoritative. The initial fan floor is maximum
   additive cooling, never a fan-speed ceiling. Neither a restart of the old
   CPU unit nor its 250–500 MHz GPU startup lock is an implicit handoff.
2. The closed request gateway owns *all* test admissions, including model load,
   local callers and the Docker relay. Its abort path has demonstrated terminal
   acknowledgement for active and queued requests; owned local processes have
   demonstrated bounded stop and `/proc` quiescence. The existing memory guard
   remains active, with at least 12 GiB available before admission.
3. The independent guard receives fresh mapped CPU-proxy and GPU temperatures,
   fan floor/RPM health, CPU/GPU actuator health and the verified numeric GPU
   limit. It has a separate execution path from potentially blocking EC calls.
   A simulated sensor loss, stale reading, actuator failure, disk-full event and
   cancelled request must each stop the fake stage before hardware use.
4. A locked, private recorder has written and synced `run_start`. The prior-run
   catalog has no unreviewed run. Every admission or upward limit change has a
   synced intent before the action, followed by readback/outcome evidence.
5. A human approves the exact next stage, workload identities, duration and
   abort method. No stage automatically starts the next stage or retries a
   failed, incomplete or crashed run.

The offline run catalog now requires a separate root-owned `review.json`
receipt for every clean run before opening another session. That receipt binds
the exact `events.jsonl` bytes by SHA-256 and can be created only by an
explicit local review call; a clean terminal marker alone does not advance
the sequence. The receipt records a bounded reviewer identifier, not a prompt
or free-form log. This is a mechanical acknowledgement of review, **not**
human approval of the next stage or recovery authority for an aborted run.
The current prototype exposes no operator-facing review CLI or live harness;
the human review and exact next-stage approval must still be designed and
qualified before hardware use.

These gates are not currently met. In particular, there is no production GPU
limit reader, live LLM gateway or qualified actuator handoff.

## Proposed bounded stages

These durations/repetitions are draft ceilings for review, not targets to push
through an unsafe trend. Finish each run with verified workload quiescence and
cooldown observation; inspect its record before authorizing another. "Idle"
means naturally idle, not a deliberate attempt to reproduce a crash or force
a power cycle. Queue length and active request count are separate quantities.

| Stage | Owned demand | Per-run ceiling | Repetitions | Purpose |
| --- | --- | ---: | ---: | --- |
| 0. Read-only baseline | No new load; current services unchanged | 120 s | 2 | Map sensors, timing, existing CPU PID, fan and power telemetry; no takeover |
| 1. Qualified idle | Model resident; 0 CPU test cores, 0 active/queued test requests | 120 s | 2 | Verify boot/idle cap, owner epochs, fan floor and recorder stability |
| 2. Small CPU steps | 1, then 2, then 4 owned busy cores; no test LLM request | 30 s per step | 2 | Estimate CPU-to-sensor gain and delay without archived 20-worker burn-in |
| 3. Small LLM steps | 1 active bounded request, 0 waiting; then 1 active + 1 waiting | 30 s per step | 3 | Measure first-prefill slope, time to first token and cancellation |
| 4. Moderate coupling | 4 busy cores, 1 active LLM, up to 4 waiting | 60 s | 3 | Estimate cross-heating and fan delay |
| 5. Typical queue | 4–5 busy cores, up to 4 active LLM requests, 10–12 waiting | 120 s | 3 | Compare throughput and thermal peaks with the known typical mix |
| 6. New prefill during decode | Stage-5 demand with one additional bounded request admitted during decode | 120 s | 10 arrivals, one per reviewed run | Verify predictive entry ceiling and avoid an idle-to-prefill spike |
| 7. Sustained combined load | Same qualified mix as stage 5; no intensity increase | 10 min | 2 | Check slower heat stores, limit persistence and control drift |

Stages 5–7 are contingent on model identification and holdout-trace validation
from earlier *separate* runs. Do not infer a safe step from synthetic TUI
temperatures. The model-resident queue stages need exact prompt/token bounds
and a qualified transport before they can be approved; prompt content must not
enter commissioning logs. CPU steps need a new bounded 0–20-core workload
adapter, not the archived fixed-20-worker script. If a stage fails, stop the
sequence and investigate; do not automatically shorten and retry it.

`energy_control.trial_plan.validate_trial_proposal` is an offline, non-executing
schema for these draft per-run ceilings. It rejects GPU proposals above
1800 MHz, separate CPU fast/slow ceilings outside Lenovo hardware bounds,
fan minimum state outside 0–12, demand outside a named stage, nonpositive/excessive duration or
repetition, and missing LLM token bounds. Its broad 8192-input/1024-output
token ceilings are *software schema limits only*, not approved experiment
sizes. Stage 6 requires its declared peak active count to be exactly one above
the declared decoding baseline, but cannot prove the request actually arrives
during decoding. It does not verify previous-stage completion, exact prompt
identity, human approval, sensor freshness, owner handoff, guard behavior or
effective GPU cap. No runner imports this module to start work, and a valid
proposal is not permission to run. The independent root safety envelope must
retain the 93°C abort and 1800 MHz limit regardless of proposal contents.
An offline `CommissioningRunSession` now refuses lifecycle arming until its
recorder has synced this proposal as the second log record; Stage 0 cannot arm
the workload gateway. The log inspector revalidates that record, including
field set and stage envelope. The proposal now carries a SHA-256 fingerprint
of the full broker `Config`, including PID gains, ramp rates and fan curve. A
session's root-side committed-config reader must return that exact policy
within a bounded check before arming; matching just the frequency ceilings is
not enough. This still does not prove the device accepted the settings—the
separate numeric limit, actuator and guard gates remain mandatory. It closes
post-hoc plan insertion into the
record. The offline guarded CPU commissioning step now requires the recorder's
validated, durable non-read-only proposal and aborts before any write if a
requested slow/fast maximum exceeds it or the recorder's proposal changes.
This does not yet connect the step to the root session or prove exclusive live
CPU ownership. The fake guard can now enforce an optional non-resetting run
deadline even while policy heartbeats arrive. A proposal-bound fake dispatcher refuses
more simultaneous owned requests than active-plus-waiting slots and rejects
requests whose trusted token-measure callback reports more than the declared
input/output caps. It also retains run-wide admission and pessimistic
prompt-plus-maximum-output token reservations after requests finish, so a
sequence of short jobs cannot silently exceed the predeclared total. The
draft schema caps total admissions at 2/5/16/17/200 for stages 3–7; these
are software ceilings for review, not approved hardware loads. Neither
component is connected to a live run, and these checks do **not** establish
the actual vLLM active/waiting distribution, admissions made through a
bypassing client, tokenizer correctness, CPU-core occupancy, or workload
identity. A production harness needs those checks, a mandatory proposal-bound
guard deadline, and a qualified transport before any physical test can use the
proposal as an operating envelope.

## Stop and pass criteria

The independent guard must close admission and terminate/verify owned work at
any relevant observed temperature **>=93°C**, or earlier when its conservative
sensor-delay/residual-heat projection reaches 93°C. It must also abort on stale
or missing critical telemetry, GPU accepted limit missing/stale/>1800 MHz,
unverified actuator result, fan failure, memory below 12 GiB, recorder failure,
request cancellation failure, process quiescence failure, driver reset or owner
change. Hardware/firmware protection is never weakened. Sampling cannot rule
out peaks between observations, so a run below 93°C is not proof that no
unobserved peak occurred.

A run passes only if it completes its predeclared interval, all owned work is
verified terminal, no independent abort/fault/reset occurs, the clock envelope
is proven throughout, and the final record is clean. A temperature excursion
that trips the guard is a **failed** run even if the workload exits cleanly.
Record maximum and 95th-percentile temperatures, maximum positive slopes,
command-to-readback delay, fan floor/RPM response, requested/accepted/measured
clocks as separate series, available-memory minimum, queue and active-request
counts, scoped GPU-reported power and any measured wall-input power. Report
median/variance of first-token latency and useful token throughput. Keep trace
uncertainty, sensor mapping and next-boot crash-time intervals explicit.

Fit the coupled thermal model on designated identification runs and validate
it on later untouched runs. Publish residual plots, peak-error bounds and
parameter uncertainty before changing PID gains. Compare the existing CPU PID
baseline with the new coordinated controller in separate runs under matched
workload and ambient conditions—never by running two writers together. No
successful short run is a guarantee of long-term stability.
