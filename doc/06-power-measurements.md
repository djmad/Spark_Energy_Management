# Power measurement baseline and next experiment

> Historical measurement proposal. [Current safety contract](10-current-safety-contract.md)
> imposes <=1800 MHz GPU and a 93°C test abort on future experiments.

## Operator-reported observations — 2026-09-25

The operator reports GPU matrix multiplication with approximately 100 GB memory
using `burnin.py`, together with CPU stress from `python3 cpu-burn-10.py`:

| Measurement | Reported approximate value | Evidence classification |
| --- | ---: | --- |
| GPU worst case observed by operator | 70 W | Operator report; sensor scope and sampling to document |
| CPU | 30 W | Operator estimate/measurement; method to document |
| Whole system, including “Connex” network cards | 140 W | Operator report; input measurement point to document |

These are useful starting observations, not certified worst-case bounds or
configured limits. We have not independently rerun the loads. Confirm NIC model
before relabeling “Connex” as a particular ConnectX product.

If all three readings were simultaneous and comparable, 140 - 70 - 30 = 40 W is
the unallocated balance. Do not attribute that entire difference to network cards:
memory, fans, board devices, conversion loss and overlapping sensor accounting
can affect it. Whole-system AC input and GPU-reported rail power are different
measurement boundaries. Do not impose 140 W as a hard limit without headroom
and supply/cable ratings; a steady 140 W reading does not bound a transient peak.

## What the source scripts actually do

### Updated primary failure observation

**Operator correction:** the reported trigger is applying 100% GPU load with
unrestricted GPU clocks. Cold/idle operation is the common lead-in, not an
established necessary cause. Prevent unbounded-clock admission regardless of
temperature. Never run an uncapped comparison to reproduce the reported lock;
all hardware trials remain at or below the 1800 MHz hard maximum. Neither a
cool sensor nor a low instantaneous measured clock proves an enforced ceiling.

The operator reports **90 W+ GPU power spikes on cold/idle-to-load entry** as
the main instability issue. This is a transient observation, separate from the
earlier approximately 70 W figure; neither is a certified power bound. Meter,
sampling bandwidth, rail scope and correlation with failure remain to be
recorded. Do not fit a numerical electrical transient model from these reports
alone or treat low temperature as permission to boost immediately.

Prioritize a verified entry clock envelope before dispatching any new prefill,
then a measured gradual ramp under sustained work. Temperature PID and shared
copper heat capacity remain necessary but cannot alone prevent a fast electrical
transient. Clock limiting is a mitigation, not a guaranteed wattage limit.
The dispatcher now has an offline pre-dispatch entry-verification hook, after
durable dispatch intent and before guard authorization/upstream start. It is
not yet wired to live actuators; production must retain the entry envelope
through dispatch and account for clients bypassing the gateway.

The entry hook now uses a held context rather than a one-shot boolean callback.
`UnifiedController.prefill_entry` takes fresh policy input under its control
lock, applies/readbacks the entry proposal and retains that lock through guard
authorization and transport dispatch. A fake-actuator integration starts from
1700 MHz and observes 1200 MHz with the policy lock held when upstream start
is called. The independent guard must not use this lock; other actuator owners
must be fenced. Dispatch must be bounded, and a local dispatch return does not
prove when the engine actually begins prefill. Engine phase integration and
measured ramp qualification remain necessary. The two focused tests pass;
no real request or clock command was issued.

The engine-side ledger can now produce a run/request/engine-bound execution
receipt only after the trusted all-worker batch-completion hook observes an
owned batch. Enqueue and batch submission do not produce this receipt; another
request's completed batch cannot release a waiting request. Cancellation fences
receipt availability. Four ledger tests pass. This is groundwork for keeping
entry limits until actual execution rather than socket dispatch: the receipt
is not yet consumed by live control, and the engine hook is not installed.
One completed batch is not proof that all chunked prefill finished, nor a
standalone permission to increase frequency.

`PrefillHold` now connects those receipts to `UnifiedController` in offline
integration. Requests registered before dispatch retain the entry ceiling
through later ticks until their own run/engine-bound first-completed-batch
evidence arrives. Busy unrelated work cannot release a pending request. Wrong
identity, stale/future evidence or an engine epoch change latches a fault;
missing receipts retain the hold under the independent trial deadline. After
release, ordinary thermal checks and busy-dwell/slew limits still apply.
Four focused prefill tests pass. This is opt-in integration, not installed
engine instrumentation or a guarantee against chunked-prefill/recompute spikes.

Cold-entry integration is now also tested in the operator-approved
`setter_monitor` mode across policy and actuator safety checks. Fresh matching
setter evidence permits fake dispatch without claiming numeric cap readback;
missing/stale evidence, a different owner epoch, or a measured clock above the
requested cap prevents dispatch even at 30°C. This validates the software gate,
not a live enforcement or transient-power measurement. Four dispatch tests pass,
including the five setter-mode cases in one test block.

### Additional operator observations — 2026-09-26

These are historical operator reports, not new tests or calibrated limits:

| Workload | Reported response |
| --- | --- |
| GPU matrix multiplication | Maximum observed power demand; even otherwise-idle CPUs throttle |
| 10 slow-core processes | No throttling observed |
| 10 fast-core processes | Slight throttling |
| All 20 CPU processes | CPU falls to approximately 78% |

The meaning of 78% (frequency/cap ratio, utilization or throughput) is awaiting
clarification. Do not use 0.78 as a PID limit or infer its MHz denominator.
Affinity, instruction mix, ambient conditions, duration, requested limits and
actual frequency traces were not supplied with these observations. Firmware
throttling and deliberate legacy-PID cap reductions remain distinct explanations.
The reports motivate separate fast/slow CPU demand inputs and shared CPU/GPU
thermal/power constraints, not numerical fitting from these four descriptions.
Matrix burn-in remains excluded without a dedicated operator go.

### Archived script behavior

`burnin.py` defaults to 100 **decimal GB**, 16384-square bf16 matrices and an
unbounded duration. It allocates A/B/C sets and then repeatedly multiplies them.
It prints `nvidia-smi power.draw` roughly every five seconds, which is inadequate
for characterizing short electrical transients. Reference matrices and temporary
comparison buffers add memory beyond the nominal allocation. Allocation precedes
the timed multiplication phase and can itself produce a transition spike. The
OOM handler does not make a 100 GB run safe alongside a resident LLM.

Despite its filename, `cpu-burn-10.py` currently has `WORKERS = 20`. It runs
integer arithmetic in 20 Python worker processes without explicit CPU affinity.
It is not the earlier pinned vecfp workload and does not establish the maximum
possible CPU power for all instruction mixes. Both files were read and archived,
not executed or modified.

## Measurement campaign proposal

Synchronize monotonic host timestamps with a known external input-power meter;
record device model, measurement location (AC wall/DC input), accuracy, averaging
window and sampling rate. Capture GPU telemetry alongside it but retain its
distinct scope. If fast spike diagnosis is needed, use suitable transient capture;
a slow smart-plug sample cannot rule spikes out.

Record supply/cable ratings, firmware/kernel/driver, ambient temperature, fan
states/RPM, actual clock limits and measured clocks, CPU caps, memory availability,
NIC link speeds/traffic and workload arguments/hashes. State whether the reported
GPU/CPU/system maxima were simultaneous. Use separate phases:

1. Idle with model resident; idle without a model only in a separate planned run.
2. Real LLM first-request/prefill and repeated idle-to-inference transitions.
3. CPU integer workload; separately, a controlled vector workload if justified.
4. Combined LLM inference/CPU load, then controlled network traffic, with memory headroom.
5. Real LLM long-prefill/decode and burst tests, with and without proposed ramping.

For each phase record baseline/mean/p95/maximum, sampling interval, rise rate,
temperature peaks, throughput and errors. Qualify a conservative entry ceiling
and any higher step within the hard 1800 MHz maximum with the durable crash
recorder already verified. GPU matrix-multiplication burn-in is excluded from
this goal and requires a separate dedicated authorization. Abort on early
warning rather than use machine crashes as the target or automatically repeat
a failed step.
Keep original stress sources intact and create a bounded harness later with
duration, headroom preflight, workload ownership and independent abort controls.

This measurement work is planned, not started. The controller design can proceed
with clock/thermal/memory envelopes now and add a calibrated system-power budget
once these measurements exist.
