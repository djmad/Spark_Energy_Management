# Primary objective: control the idle-to-prefill clock transition

> Historical draft. [Current safety contract](10-current-safety-contract.md)
> supersedes the 2 GHz baseline and any higher clock experiment.

Updated user direction, 25 September 2026. This supersedes the earlier proposal
to keep 2000 MHz as the permanent operating maximum and to retain elevated
clock limits during a burst grace period. This is a design revision only: no
controller deployment, hardware writes, workload generation or crash tests occurred.

## Working hypothesis and desired behavior

The operator identifies aggressive NVIDIA clock ramping when a model receives
a prompt as the central problem: a spike during prompt processing before output
tokens appear. For text inference, that GPU stage is prefill; CPU tokenization
and multimodal encoding are separate stages and should receive separate markers
where relevant. The association is the operator's observation, not yet an
independently established electrical/thermal crash cause.

The desired sequence is:

```text
idle: ceiling 2000 MHz
  -> admit prefill only with baseline ceiling already established
  -> sustained busy GPU + safe temperatures/slopes/readbacks
  -> small clock-ceiling increase -> observe -> next increase
  -> stop increasing at qualified maximum or any limiting condition
  -> first valid 0% utilization / workload completion: ceiling back to 2000 MHz
```

Treat 2000 MHz as the default maximum permission to the driver, not automatically
as a fixed minimum=maximum lock. This lets hardware idle below the ceiling while
preventing an immediate jump above it. If a jump from a low idle clock to 2000
MHz itself causes the problem, this policy is insufficient: compare a bounded
lower initial cap versus a fixed-clock experiment under measurement. Holding
2000 MHz alone does not eliminate the power change when compute work begins.
Do not assume a frequency ceiling is a power-slew limiter.

Use the hardware's supported clock interface; do not implement a custom NVIDIA
driver. Verify nearest-step behavior and distinguish requested ceiling, accepted
setting and actual frequency. The observed 1969 MHz value must not be relabeled
as direct proof of an exact 2000 MHz lock.

## Ramp rules

1. Baseline ceiling must exist before a request, not be applied reactively after
   utilization rises. Broker readiness and inference admission depend on it.
2. Use a configurable near-saturation threshold and busy dwell rather than exact
   equality to 100%. Candidate experiment: >=95% for 1 s, then a step no larger
   than 50 MHz every 500 ms. These are unqualified test proposals. Sampling faster
   does not necessarily yield fresher driver utilization measurements.
3. Each increase also requires fresh sensors, safe temperature/slope/projected
   headroom, cooling readiness, sufficient memory, no fault/throttling warning,
   and healthy durable logging during commissioning. Utilization is a workload
   signal, never an instruction to increase power irrespective of temperature.
4. Ramp up slowly using monotonic elapsed time; never catch up multiple missed
   steps after a stalled loop. Quantize to supported behavior without exceeding
   the permitted ceiling; a repeated ineffective step must not become a sudden jump.
5. At the first valid 0% observation, reset the ceiling immediately to
   min(2000, current thermal/fault ceiling), without the upward slew restriction.
   Reset busy dwell and ramp progress. A brief zero may lower performance during
   a continuing request; that is preferable to leaving the next burst unguarded.
   A protective reset must never raise a currently derated clock ceiling.
6. Near-zero but nonzero utilization must not keep an elevated cap indefinitely:
   add a short configurable low-load timeout. Missing/stale utilization inhibits
   increases and enters conservative control; it is not fabricated as zero.
7. Explicit workload completion rearms before the next request. A new prefill
   while decoding is already busy also rearms before forwarding that new work.
   This can reduce existing-request throughput; measure the tradeoff. Support
   batching with a shared preparation epoch so concurrent arrivals cannot race
   admission against a clock increase. Uncovered/direct inference paths invalidate
   the prefill-protection guarantee and must be reported.
8. Emergency cooling, memory guards and immediate downward caps always outrank
   ramping. Keep fans elevated after activity ends until residual heat clears.

The qualified hard maximum is a separate setting from the 2000 MHz baseline.
Initially both are 2000 MHz. Later commissioning may authorize a bounded range
above it; the 3003 MHz hardware rating is not a selected safe operating target.
Do not automatically raise the hard maximum because a short trial passed.

[NVIDIA documents GPU utilization](https://docs.nvidia.com/deploy/nvidia-smi/)
as activity over a sampling period, which is why a post-load utilization check
cannot protect the very first transition by itself.

## Durable crash flight recorder

The user explicitly requests disk logs for commissioning, where crashes may
occur. This is separate from the four memory-only graph series and their compact
one-day retention. Do not reduce crash evidence to 600 graph buckets.

Proposed location: `/var/log/spark-energy/commissioning/<run-id>/`, root-owned,
restricted permissions, never workload-writable. Implement a bounded append-only
structured stream (JSONL is sufficient initially), plus a run manifest. Record:

- UTC and monotonic timestamps, boot/run IDs, sequence and clock-source quality.
- Source/config hashes, kernel/driver/firmware identity, approved experiment
  maximum, baseline, requested/accepted/observed clocks and verification quality.
- Per-zone and GPU temperatures, slopes, GPU utilization/power with source age,
  CPU measured clocks/caps/load, fan RPM/state and memory available/pressure.
- Request arrival, admission, actual prefill start/end when instrumentation allows,
  first output token and completion/cancel markers. Label proxy-arrival estimates
  separately; do not invent internal engine timestamps. Record prompt token counts
  and opaque local correlation IDs, never prompt text, tokens, passwords or bodies.
- Every control decision, constraint, intended write, result/readback, API/driver
  latency, missed deadlines, sensor error, logging queue loss and watchdog event.
- Relevant kernel GPU/Xid/thermal/memory events with bounded collection and source
  timestamps. Record independent power-meter samples when available.

Current offline implementation is narrower than this full proposal:
`CommissioningRecorder` writes a single bounded 16 MiB run file and syncs each
event. It now accepts a fixed-code `decision` record containing candidate
GPU/CPU maxima, additive fan floor and mode; its `scope=candidate` explicitly
does **not** claim the settings were applied. Arbitrary reason text is rejected
to keep prompt and credential material out of the record. Actual actuator
application still needs a separate synced intent and verified outcome. A live
controller must choose when to write decisions, measure recorder overhead, and
abort further experimental increases/admissions if the bounded file fills.
Request-phase and kernel-event capture are not yet integrated.
`energy_control/decision_logging.py` can translate a shadow-policy proposal to
the fixed schema without persisting its free-form reasons. The run catalog
and direct run inspector treat any abort, `ABORT` decision or unverified outcome
as unclean even if an older malformed log has a later clean end marker. The
recorder no longer writes a clean marker on ordinary context exit: callers
must explicitly request it after verifying quiescence and resolving intents.
An unsafe or pending run rejects that request and still closes uncleanly.
`energy_control/commissioning_sample.py` now builds a fake-tested sample from
a coherent host readout, the slope-bearing safety snapshot and a separate
numeric GPU-limit reading. It checks sensor identity/value and proof age rather
than filling an accepted cap from measured/application/OEM clocks. A sample
contains its own acquisition-completion monotonic and UTC timestamps, distinct
from the recorder's append/sync timestamps. Replay uses acquisition monotonic
time when present, avoiding a false thermal delay from disk latency. This
assembly is not wired to live commissioning; root-side epoch binding, phase
instrumentation and continuous guard operation remain necessary.

Candidate sample interval during experiments: 100–250 ms for inexpensive CPU/GPU
observations, subject to measured overhead and native sensor refresh rate; fan
reads stay at the established safe cadence and include age. A slow hardware
sensor cannot gain time resolution from repeated reads.

Before every upward clock change or releasing a new prefill admission epoch,
append the exact intent and synchronize it to disk with `fdatasync`/equivalent.
Only proceed after the bounded durable-write acknowledgement. Record result and
readback immediately afterward, syncing the outcome too. A surviving intent
without a result means **attempted/unknown**, not proof the write completed.
Do not reset the permitted delay budget by endlessly retrying a blocked sync.

Continuous samples use a bounded writer queue and a proposed <=250 ms sync
interval. Record the last acknowledged durable sequence and sync latency. An
append/flush into userspace or kernel page cache is not a durability guarantee.
Isolate disk I/O from the emergency control path. If disk is full, slow, lost or
the queue overflows, inhibit further experimental increases/admissions, preserve
an error marker where possible and apply conservative control. Protective clock
reductions and fan requests must never wait for logging.

Use preallocated/bounded segments, e.g. 16 MiB each, with an initial 256 MiB run
budget and 1 GiB total commissioning budget. These are configurable proposals.
Account for manifests and kernel-event capture in the budget. Preserve the latest
unclean run until reviewed; do not silently rotate away its last evidence. If
space cannot be reclaimed from reviewed runs, refuse a new experiment. Test the
actual write/sync cost so the recorder does not perturb the experiment excessively.

Persist file/directory metadata when creating or rotating segments. On recovery,
accept the valid prefix and report an incomplete trailing record. A clean-run
end marker is synced. Missing end marker means unclean termination, not necessarily
a machine crash; use boot IDs and watchdog/kernel evidence to distinguish cases.
The current inspector also excludes semantically invalid complete rows from
that prefix and marks the run corrupt; merely well-formed JSON cannot certify
a clean experiment.

[systemd journaling](https://www.freedesktop.org/software/systemd/man/252/journald.conf.html)
provides persistent storage and sync controls, but configuring a normal service
log alone is not a substitute for the experiment's explicit durable intent protocol.
Inspect existing persistence before any commissioning changes; do not silently
reconfigure global journald during design work.

## After a crash or unexpected restart

Start with the conservative baseline (or lower protective cap), close experiment
admission and flag the incomplete run. Never automatically resume the failing
frequency step or continue searching upward. Collect previous-boot kernel/journal
evidence and available pstore data without resetting the GPU first. The earlier
inspection found pstore empty; its availability is not guaranteed.

Produce a timeline of the last durable sample, last durable intent, last readback,
last heartbeat and next boot. Report the uncertainty interval and missing tail.
A complete host freeze can prevent the last sample or log write, and storage
hardware may not honor durability as expected. Local logs cannot guarantee the
exact instant of failure or prove that the last clock change caused it. Optional
independent heartbeat/power capture can narrow that interval; no remote logging
destination is configured without an explicit choice.

The read-only `python3 -m analysis.run_review RUN_ID` helper now reviews one
root-owned run under `/var/lib/energy-control/runs` (or an explicitly selected
trusted `--parent`). It emits only a compact JSON summary, not raw prompts or
broad journal content: last durable event/time, pending-intent count,
terminal-verification marker, tail integrity and a possible stop interval.
It separately summarizes the last sensor sample (acquisition versus record
time, temperatures/slopes, clock fields, fan, memory, queue and scoped power),
last intent, last outcome, last policy decision and abort marker when present.
There is **no durable guard-side heartbeat yet**; the reviewer reports that
field as unknown rather than inventing a timestamp. If the recorded and
current boot IDs differ, it uses `/proc/stat`'s one-second `btime` value as a conditional
upper bound; if they match or wall-clock ordering conflicts, it reports no
next-boot UTC bound. A system-clock step or inaccurate boot timestamp can
invalidate that interval. An unclean marker alone does **not** identify a
hardware crash, and this helper does not collect kernel evidence or authorize
another run. No production run directory is installed yet.

Commissioning aims to stop on warning signals before a crash. A crash is evidence
to investigate, not a successful training sample for an unattended optimizer.
Before live testing, verify recorder ordering, kill/restart recovery and bounded
storage with fake actuators and simulated sensor traces. No intentional kernel
panic, power cut or uncontrolled load is needed to test the software contract.
