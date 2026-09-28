# Superseded goal (preserved 26 September 2026)

> **Historical record, not instructions.** Replaced by [goal v2](../goal.md) on 26 September 2026. Preserved verbatim below; its PAUSED/CREATE-ONLY wording no longer applies. Original `goal.md` SHA-256: `f6a56d7063749b9ae7a047826dd4a7445499b487f19d68a469ab525f2f5fb043`.

---

# Spark Energy Management goal

Status: PAUSED at the user's request. Resume only on explicit instruction.

## Current instructions superseding historical wording below

- Keep vLLM resident; cancel owned active/queued test prompts for normal stop.
- Emergency also requests a 500 MHz GPU maximum; hard development ceiling remains 1800 MHz.
- GPU matrix-multiplication burn-in is excluded until a separate dedicated go. Do not overlap it with the LLM.
- RAM protection is a separate project and remains untouched.
- No engine modifications or terminal-receipt instrumentation are required for HTTP-observed testing. Preserve server-drain uncertainty honestly.
- Replace the legacy CPU controller only when the unified successor, ownership handoff and rollback are ready.
- Prefer fewer meaningful hardware test blocks. No hardware qualification or deployment has been completed.

## Handover

Read [agent handover](doc/35-agent-handover.md), especially its LAST EDIT section: `energy_control/gpu_owner_session.py` is unfinished and untested. See [delivery audit](doc/27-readiness-audit.md) for remaining requirements.

## Full stored objective

The original creation/pause language below is historical; the goal was subsequently resumed and is now paused again at the user's request. Later constraints above govern where they differ.

MAINTASK: Protect the hardware and deliver energy_control to stabilize the Spark during cold/idle-to-load transitions and sustained combined workloads.

CREATE ONLY; leave PAUSED until explicitly resumed. No implementation, hardware changes or tests during creation.

Platform: Lenovo ThinkStation PGX / GB10. Build on Spark_Energy_Management's model/TUI. Inspect supplied CPU, LLM and GPU burn-in scripts in Archive; preserve originals and verify workload targets, memory use and termination.

MANDATORY LIMITS supersede earlier settings:
- Hardware GPU maximum <=1800 MHz, including startup/idle. Former 2000 MHz and higher defaults are obsolete for hardware.
- Existing CPU PID is the baseline; regulate separate slow/fast CPU maxima within hardware bounds.
- 93°C is the hard test-abort boundary, not the target. Use lower targets and predictive early aborts for sensor delay/residual heat. Terminate test loads at any relevant reading >=93°C or earlier if a breach is predicted. Do not promise protection against unobserved peaks.
- Independent guard aborts owned loads on unsafe temperatures, stale critical telemetry, actuator failure or insufficient memory. Verify process termination and cancellation of active/queued test LLM requests. Preserve firmware and memory protection.
- Set only additive MINIMUM fan cooling. Maximum cooling always remains 100%. One writer per actuator.
- Smooth normal ramp-up/down without excessive backoff; emergency reductions/aborts take priority. Investigate crashes after cooling without assuming their cause or deliberately seeking crashes.

WORK:
1. Inventory controls, archived loads and CPU PID; record versions/configuration/hashes without secrets.
2. Extend model/TUI to 0–20 loaded CPU cores and 0–20 queued LLM jobs. Distinguish queued jobs, active requests and utilization. Typical load: 4–5 CPU cores at 100%, full LLM/GPU activity and 10+ waiting jobs.
3. Before hardware trials, implement/test the independent guard and bounded durable commissioning logs. Capture run/boot IDs, timestamps, workload phases, temperatures/slopes, requested/accepted/measured clocks, fan floor/RPM, memory, utilization, scoped power and decisions. Sync intent before increases/load admission. Preserve incomplete-run evidence, report crash-time uncertainty and never automatically repeat failed experiments.
4. Learn thermal coupling, response times, sensor/actuator delays and fan response through staged bounded tests. Fit model parameters/uncertainty and validate against separate traces.
5. Progress from small isolated loads to typical/combined workloads, cold-to-prefill and new prefill during decoding. High-power matrix-multiplication burn-in is final qualification; verify script identity and stop/drain the LLM beforehand as requested.
6. Optimize coordinated CPU/GPU PID and ramps for responsiveness, reduced spikes and useful throughput. Include anti-windup, filtered derivatives, slew limits, hysteresis, workload anticipation and protective overrides.
7. Deliver energy_control with startup/resume/reset protection, failure containment, migration/rollback and modular fan adapters. Qualify Lenovo first; other platforms require separate capability discovery and hardware commands.
8. Provide unauthenticated read-only graph/telemetry API: memory-only 15m/60m/1d history, <=600 buckets, prompt eviction/downsampling. Keep commissioning logs on disk.
9. Provide password-confirmed parameter API/CLI for GPU/CPU slow/CPU fast maxima, PID/ramp settings and fan curves. Network API unprivileged; root broker independently enforces limits and exposes no arbitrary commands.

DONE WHEN: calibrated model with uncertainty; reproducible hardware qualification with predeclared durations/criteria and sustained-load results; tested guard/log recovery; working service/adapters/APIs and documentation. Report measured stability and limitations. Remain PAUSED until resumed.

