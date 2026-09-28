# Handoff evidence — 26 September 2026

The legacy writers were replaced by the installed `energy_control.service`
following [doc/41](41-handoff-runbook.md). Executed by the root main agent
session under the claim `/run/spark-energy/agent-command` (taken 16:23:21
CEST, kept for the following live trials).

## Known state after the handoff (17:18 CEST)

| Item | State |
| --- | --- |
| Owner | `energy_control.service`: enabled, active, 0 restarts in the 10 min observation |
| GPU | Locked 200–1200 MHz by the service's GPU owner (setter acknowledged; measured idle 1170–1183 MHz); lock re-asserted every 60 s |
| CPU maxima | Owned by the service; follow demand between the entry ratio (slow 1984 / fast 2639 MHz) and hardware maximum (2808 / 3900 MHz); minimum unchanged, `conservative` |
| Fan floor | Owned by the service; staged 2–6 at idle, both fans spinning (≈3700–7500 RPM) |
| Temperatures | Hottest ACPI zone ≈ 40 °C, GPU 37–38 °C, idle LLM |
| Legacy units | `spark-cpu-thermal-guard`, `dgx-fan-max`, `dgx-fan-control` masked (→ `/dev/null`), inactive; `nv-cpu-governor` masked as before |
| Legacy unit files | Backed up root-only in `/var/lib/spark-energy/legacy-units-backup/20260926T1624/` (hashes below) |
| Boot ordering | `Spark_Dashboard/scripts/start-selected-vllm.sh` waits for `/run/spark-energy/entry-ceiling` with the current boot ID while `energy_control` is enabled |
| Service evidence | Rotating runs in `/var/lib/spark-energy/service-runs/` (16 × 16 MiB) |
| Installed code | `/opt/spark-energy` (manifest `MANIFEST.sha256`), previous copies kept as `/opt/spark-energy.prev-*` |
| Untouched | vLLM (`vllm_node`), `hostapp-ram-guard`, `hostapp-resource-supervisor`, `spark-vllm-bridge`, NVIDIA daemons |

Backup hashes (sha256): `dgx-fan-max.service` b67f142a…c439c6,
`spark-cpu-thermal-guard.service` 78f7e91e…ab7c82dc,
`10-cap-mode.conf` e6f2e797…3cfd8d.

## Timeline

- 16:23 claim, read-only baseline (LLM idle, zones ≤ 38.5 °C, GPU 35 °C,
  CPU maxima at hardware maximum, 23 GiB available).
- 16:24 legacy units stopped and disabled; unit files moved to the backup
  (a unit file in `/etc/systemd/system` cannot be masked); units masked;
  `handoff_probe`: all known writers fenced; `energy_control` enabled.
- 16:24–17:45 start attempts and runs failed on live-only defects (below). Each
  failure left the owners' safe state — GPU 200–500 MHz (driver acknowledged,
  measured 500 MHz), CPU hardware minimum, fan floor 12 — so the GPU was never
  uncapped. Each defect was fixed, tested and reinstalled.
- 17:08 service running; 17:08–17:18 observation without abort or restart.

## Live defects found and fixed

Fake tests could not show these; each now has a regression test.

1. **Ownership timestamp.** The GPU setter requires an ownership observation
   after command completion; the polled reading predated it. Readings are
   now synchronous and cheap (mask symlinks, cgroups, driver epoch) plus a
   1 s authoritative `systemctl show` probe.
2. **Queued evidence freshness.** Guard readers checked freshness on every
   queued frame, faulting on start-up backlog. Freshness now applies to the
   newest frame only; identity/ordering to all.
3. **Host sampler backlog.** Same defect in the policy-side host sampler;
   bounded 64-frame drain, freshness on the newest; plus a 2 s warm-up with
   guard heartbeats after start.
4. **Guard first reply.** The guard answers only after its sampler starts;
   reply timeout raised to 2 s. Guard and owner faults are now reported to the
   journal.
5. **Deferred cpufreq update.** `cppc_cpufreq` applies new maxima
   asynchronously; immediate readback showed old values (false mismatch; the
   emergency minimum reported unverified although it landed). Writes are now
   awaited (bounded 0.5 s) before readback.
6. **Slope prediction.** The max-pairwise slope turned 5 °C sensor steps into
   20 °C/s and aborted at 60 °C. Now a least-squares slope over 2 s, applied
   only within 10 °C of each fixed limit (ACPI ≥ 83, GPU ≥ 75 °C); the
   93/85 °C limits are unchanged.
7. **Fans stopping.** At floor 0 (firmware automatic) the fans stop at idle
   and 0 RPM reads as a failed fan. The service floor minimum is now 2.
8. **EC busy.** The fan EC transport rejects requests while busy
   (`preflight failed: -16; original request not submitted`); one EBUSY
   faulted the fan owner. The fan adapter now retries EBUSY/EAGAIN/EINTR/EIO/
   ETIMEDOUT/EREMOTEIO with bounded backoff (≈ 0.35 s), like the legacy helper;
   the service guard's heartbeat deadline is 2 s.
9. **EC contention.** Every `cur_state` read is an uncached EC transaction;
   guard, policy and fan owner together caused 1.1 s EC timeouts that stalled
   temperature sampling ("independent safety acquisition failed"). Service
   collectors now take fan health from the driver's cached RPM telemetry on a
   background thread (no floor reads; last good value held ≤ 3 s), and the
   fan owner reads the EC floor only every 5 s (evidence max age 12 s, one
   failed periodic read tolerated). Note also: when a floor write fails, the
   driver itself restores firmware-automatic mode.
10. **Clock settling after a reduction.** After a normal GPU cap reduction the
    measured clock needs a moment to follow; the guard compared it at once
    with the new, lower request and aborted. A measured clock above the
    request is now tolerated for 1 s after setter completion (a lost lock stays
    high far longer); the 1800 MHz hard check stays strict.
11. **Power-balance mapping at low load.** The balancer mapped watts back to a
    cap ratio against the *current* removable power; at 5 % GPU load that is a
    fraction of a watt, so any small relief request became a full cut to
    500 MHz (this caused the reduction in item 10). Watts now map against at
    least the nominal span; at or above nominal load nothing changes. The
    service journal logs every GPU cap change with the policy mode and reason.

## Rollback (tested path pieces; not executed)

1. `systemctl disable --now energy_control` (owners leave the safe state).
2. `systemctl unmask spark-cpu-thermal-guard dgx-fan-max dgx-fan-control`.
3. Copy the backed-up unit files and drop-in back into `/etc/systemd/system/`,
   `systemctl daemon-reload`, `systemctl enable --now dgx-fan-max spark-cpu-thermal-guard`.
   The guard's `ExecStartPre` re-applies the 250–500 MHz GPU lock.

## Open follow-ups

- The GPU maximum stays 1200 MHz until the entry-ceiling trials (work plan 6).
- Unowned LLM requests are protected reactively (idle fallback, re-arm on
  queue growth); preventive protection needs the request gateway (doc/15).
- The sudoers rule and dashboard toggle remain; masking fences them.
- `/opt/spark-energy.prev-*` copies can be pruned after review.
12. **Policy-side sampler staleness.** Under 12 heavy requests plus CPU load one
    host collection pass exceeded 0.5 s and the policy's host sampler latched
    permanently faulted ("isolated host sample unavailable"). A stale frame
    now means "no new frame" (bound 1 s, like the guard's sensor-age limit)
    and the supervisor skips a tick, aborting only after > 1 s without one.
13. **Prediction from noisy P-cluster spikes.** The guard aborted with
    "projected temperature breach: acpi_TS1P" while the 1 Hz trace never
    exceeded ≈ 74 °C: P-core cluster sensors spike by several °C within a
    fraction of a second. The projection now starts from the 2 s
    least-squares trend value instead of the raw last sample (a sustained
    rise still moves trend and slope); the 93/85 °C hard limits still act on
    every raw sample, and abort reasons report raw value, trend and slope.

14. **Guard acquisition tolerance under real prefill (27 September 00:28).**
    Entry trial 1200 r2 (unique prompts, all four 20k prefills done, one
    request still decoding) aborted with "independent safety acquisition
    failed (3 consecutive)": three failed 0.1 s periodic checks, i.e. only
    ~0.3 s of tolerance, while host frames under real prefill arrive
    0.2–0.7 s apart (`nvidia-smi` several hundred ms instead of 40–80 ms
    idle). The cause was not recorded. Fix: the abort reason now carries the
    exception; the periodic path tolerates a gap of up to 1 s (time-based)
    only while the last good snapshot kept ≥ 10 °C to every abort limit,
    otherwise the 3-check rule stays. Worst-case blindness ≈ 2 s (1 s sample
    age + 1 s grace) ≈ 4 °C at the fastest measured rise. Command-path
    checks (register, start, disarm) still fail at once.
15. **Guard teardown message.** `supervisor.close()` sets the shared abort
    route first so every owner runs its emergency action; the guard then
    logs "workload control unhealthy (owned=N)" and exits 2 because HTTP
    completions are never engine-drain evidence. Expected at every trial
    end, not a fault (seen after 1200 r1, which passed).
16. **Dashboard status gap during trials.** Trials stop the service, the only
    writer of `/run/spark-energy/status.json`. The trial runner now
    publishes the same status (mode `trial:<name>:<mode>`) at 1 Hz from its
    own readout.

17. **Spike-driven GPU spills at low fan (27 September 03:40).** In the
    floor-2 identification run the policy cut the GPU from 1800 to its
    25 % spill bound (1475 MHz) whenever a P-cluster sensor spiked
    (raw 72–76 °C, GPU ≤ 51 °C) and climbed back in 25 MHz steps: 83 cap
    changes in 390 s exhausted the trial's 128-command GPU budget and the
    run stopped ("GPU owner session command budget exhausted"; the service
    has no budget, but the needless cuts would still happen). Cause: far
    below target the CPU PID's integral is unwound at full headroom, so the
    derivative term alone requests relief once the 2 s trend rises faster
    than ≈ 6 °C/s. Fix (`simulation/model.py`, `pid_band_c` 12 °C): a PID
    may request relief only within 12 °C of its target (CPU ≥ 78 °C, GPU
    ≥ 63 °C trend); the predicted-breach check, guard band and 93/85 °C
    guard aborts are unchanged. Regression test replays the spike (GPU held
    at 1800 MHz; without the band it drops to 1475 MHz). Floor 2 re-run in
    chain 3.
    **Follow-up (04:05):** the floor-2 re-run completed (the band keeps the
    derivative quiet below 78 °C) but still showed 68 GPU cap changes down
    to 1475 MHz: the headroom PID's proportional band starts relief at
    ≈ target − 1/kp ≈ 77 °C, and floor-2 P-cluster spikes reach 79 °C, so
    the balance still spilled 25 % of the GPU span for a CPU that was
    11 °C below target. Second fix: **no GPU spill on behalf of the CPU
    while the CPU is below its target** — CPU caps alone relieve it there
    (they cost ≈ 3–7 % LLM throughput, doc/45); at or above target the 25 %
    spill remains. Regression test (79–80 °C oscillation, GPU held at
    1800 MHz; with the target lowered to 78.5 °C the spill reappears).
    Installed 04:10, effective from the next service start.

18. **Agent interference during a live trial (27 September ≈ 04:15).**
    While chain 3 ran CPU-impact trial 2600/2000, the agent ran the full
    unittest suite several times and the installer (which runs the suite,
    then moves `/opt/spark-energy` aside). The load stretched the trial
    policy's telemetry interval past 1 s ("policy thermal interval
    invalid"); the trial runner closed to the safe state and restarted the
    service (no thermal event). Rule adopted: no full suite or install while
    a trial or chain runs — install only as a chain's first step. The
    interrupted step was re-run in chain 4. Also: fast-core reservation
    during LLM work lowered 0.35 → 0.10 (doc/45); the CPU cost per watt
    stays 0.6 until the CPU twin node is fitted (with the synthetic CPU node
    a cost of 0.3 over-relieved the CPU in the hot-ambient scenario).

19. **Single-sample projected breach (27 September 05:18).** Sustained
    run 7 (12 unique jobs + 50 % duty on P0) aborted after 72 s: "projected
    temperature breach: acpi_TS1P raw 79.9 C trend 83.2 C +9.1 C/s". The
    trend had just entered the 10 °C prediction band and one spike slope
    projected past 93 °C; the P-cluster step tests show real rises ≤ ≈ 5 °C/s
    and firmware holding the P-clusters near 78–80 °C. Fix (guard and
    policy): a projected breach must persist 1 s before it aborts, unless
    the trend basis is within 3 °C of the limit (then immediately, as
    before); raw 93/85 °C hard limits unchanged. Tests cover the live
    sample (no abort), a sustained rise (abort after 1 s) and the near-limit
    case (immediate). Installed 05:25; sustained run repeated as run 8 (05:26).

20. **Policy interval abort on slow frames (27 September 06:26).** Under
    12 jobs + P0 load the policy's thermal frames arrived late (a > 1 s gap
    between ticks); a likely contributor is the policy collector's
    synchronous vLLM `/metrics` poll (0.5 s timeout) on top of `nvidia-smi`
    (not measured directly: the 1 Hz service trace cannot resolve the
    collector cadence). The supervisor tolerated ≤ 1 s without a frame
    but then rejected the next frame when > 1 s had passed since the last
    tick, so a 1.3 s gap aborted the service (safe state, restart; SQ run 9
    failed 10 s before its end). Fixes: vLLM queue gauges from a background
    poll (`BackgroundQueueTelemetry`, as for fan RPM); policy ticks tolerate
    gaps up to 3 s (actuators hold, PIDs integrate at most 1 s; the policy's
    telemetry-time check uses the same cap); the independent guard keeps its
    own 1 s sensor-age rules. Installed 06:31 with the operator broker wiring
    and the status API (doc/47).

21. **Headroom integral drained by performance limits (27 September,
    TH run 10).** With lowered test targets (GPU 50 °C) the GPU ceiling
    cycled 625–1800 MHz while the GPU sat 43–45 °C. Offline replay of the
    trace through the policy supervisor reproduced it: the anti-windup
    back-calculation pulled the GPU PID integral toward the *served* cap,
    which the entry ceiling and the ramp hold below the PID's request; a
    load-start derivative kick (39 → 45 °C) made the served cap lower still,
    and within 20 s the integral fell from 1.0 to ≈ 0, after which the P term
    alone requested ≈ 60 % relief 6 °C below the target. Fixes:
    (a) back-calculation only toward the PID's own saturation (raw > 1) or up
    to an actuator held above the request — a downstream performance limit
    freezes the integral instead of draining it; (b) GPU derivative filter
    3 s (was 1 s) against integer-sensor steps; a real 2 °C/s rise is still
    cut within 1 s. Synthetic regression (integer sensor, 2 s trend): old
    code 815–894 MHz, fixed ≥ 1650 MHz (mean ≥ 1740). Production targets
    never reached this regime (GPU ≈ 52 °C against 75 °C).

22. **GPU derivative gain (27 September, after TH run 12).** With defect 21
    fixed the loop regulated at its 50 °C test target but dithered the
    ceiling ≈ 125 MHz (worst 650 MHz in 30 s). `analysis/tune_gpu_loop.py`
    closes the loop on the calibrated twin with the live sensing path
    (integer GPU sensor, 2 s trend, 12 jobs, a prefill every 90 s): only the
    current kd 0.08 produced sustained > 100 MHz windows; kd 0.04 regulated
    without. Default `gpu_kd` 0.08 → 0.04 (a real 2 °C/s rise is still cut
    within 1 s; tests unchanged). **Live result (TH run 13): no measurable
    change** (worst 700 MHz, 507 of 788 windows > 100 MHz, as run 12): the
    live dither is disturbance-driven (request-mix power steps of several
    watts move the integer GPU sensor 1–2 °C), which the noise-free twin does
    not contain. kd 0.04 is kept (tests pass, twin-preferred, live-neutral).

23. **Boot: CPU baseline missing after reboot (27 September, 11:08).** The
    operator's warm reboot was the first boot without the legacy guard: all
    20 cpufreq policies came up with the `performance` governor, and the CPU
    owner refused every start ("CPU min/governor differs from qualified
    baseline", which requires `conservative` and minimum = hardware
    minimum). systemd retried 5 times and stopped at the start limit
    (11:11:17); no entry-ceiling file, so the Spark_Dashboard gate kept vLLM
    from starting. The GPU stayed locked at the owners' 200–500 MHz emergency
    request from each failed start's safe state (measured 448 MHz; the
    3003 MHz reported by `clocks.max.sm` is the hardware maximum, not a
    cap — an agent status message misread it as "unrestricted" and was
    corrected). Recovery 11:16: governor set to the baseline, service
    started (entry 1700 / max 1800). Fix: `LenovoGb10CpuMaxima.establish_baseline()`
    sets governor and minimum on every policy before any owner starts,
    called from the service entrypoint (logged); verified live by setting all
    policies to `performance` and restarting ("CPU baseline established: 20
    change(s), governor performance -> conservative"). Also: the guard client
    now reports a channel closed mid-request as "guard ownership channel
    closed" (RuntimeError) instead of a raw ConnectionResetError. 631 tests
    OK. Cold-boot (power-cycle) verification follows.

24. **Cold boot: guard saw the pre-lock vendor clock (27 September,
    11:24, operator power cycle).** Defect 23's fix worked ("CPU baseline
    established: 20 change(s)"), but the first service start aborted: the
    guard's strict "GPU measured clock above hard envelope" check (which
    detects a lost lock) evaluated a frame that still showed the vendor boot
    clock right after the entry lock was applied. The second start (after
    the safe state's emergency lock) succeeded at 11:25:56; the
    Spark_Dashboard gate held vLLM until the entry-ceiling file existed
    (vLLM started ≈ 11:26). Fix: after applying the entry lock the
    supervisor waits (≤ 5 s) for a telemetry frame sampled after the lock
    with the measured clock ≤ entry + 50 MHz before it arms the guard; an
    unsettled clock still fails the start into the safe state. Unit tests;
    not reproduced live (that would need unrestricted clocks with vLLM
    resident) — the next boot confirms it. 633 tests OK, installed 11:33.

25. **Purging a fenced package removed its mask (27 September, 11:44).**
    `dpkg --purge nv-cpu-governor` (operator clean-up; the package was only a
    one-shot "governor = performance" setter and already deinstalled) also
    removed `/etc/systemd/system/nv-cpu-governor.service → /dev/null`. The GPU
    owner's ownership check requires every known legacy writer unit to be
    masked, so the running service aborted (11:44:38) and every restart failed
    with the opaque "GPU owner startup unacknowledged". The GPU stayed capped
    by the lock persisting from the last run (1768 MHz measured under 1800 with
    vLLM busy); guard and dynamic control were down ≈ 2 min. Recovery 11:46:
    `systemctl mask nv-cpu-governor` (a mask on a non-existent unit is valid
    and survives reboots) and a service restart. The strict rule stays — only
    a mask stops a re-installed package from enabling and starting the unit —
    and **every unit in `handoff_probe.UNITS` must stay masked even after its
    files or package are removed**. Diagnostics added: `LiveOwnership.diagnose()`
    names the unmasked unit (with the re-mask command) in the startup error
    and once in the journal when ownership is lost (634 tests OK on an idle
    machine; install pending an idle machine — two timing-sensitive
    integration tests fail under vLLM load).

26. **GPU maximum raised to 2000 MHz; a second 1800 bound (27 September,
    12:20–12:45).** Operator decision: hard envelope 2200 MHz (end goal
    "productive 2200, maybe only 2100"), production maximum **2000 MHz**
    ("known as working"), entry ceiling unchanged at 1700 MHz. The envelope
    now has a single source (`energy_control/limits.py`, imported by broker,
    safety, GPU owner and session, evidence, recorder, trial plan/runner,
    lifecycle, unified actuation, passive probe, the simulation Settings and
    the TUI); boundary tests derive from it. The first restart with max 2000
    aborted ("require minimum < baseline <= maximum <= 1800 MHz"): the
    simulation `Settings` the live policy uses had its own 1800 bound, and no
    test built the policy above 1800. Config restored to 1800 within a minute
    (GPU at the emergency lock meanwhile), bound moved to the shared constant,
    regression test builds the policy at 1800/2000/2200. Production max 2000
    active since 12:45. Steps above 2000 use the boot-bound qualification
    override `/run/spark-energy/qualification.json` (root, current boot ID,
    tmpfs), so a maximum under test never survives an unexpected reboot.
    636 tests OK.

27. **CPU PID integral pinned near zero: proportional-only plateau ≈ 9 °C
    below target (found 27 September, 13:20; analysis only, not fixed).**
    - **Symptom.** Under the operator's 20-worker CPU load the controller held
      TS1P at ≈ 81 °C (median, max 82–83) with fast caps 2.5–3.1 GHz and slow
      caps 1.9–2.5 GHz. The same plateau appeared at fan floor 6 (11:39–11:44),
      11–12 (12:16–12:19) and 8 (12:19–12:27). A plateau that does not move
      with the fan is the signature of a proportional-only loop:
      `T ≈ target − cap/Kp = 90 − 0.65/0.075 ≈ 81 °C`.
    - **Mechanism** (`simulation/model.py` `PID.track`, tracking call in
      `energy_control/policy.py`):
      1. Back-calculation toward the PID's own saturation drains the integral
         to 0 whenever the P term alone exceeds 1, that is, more than 13.3 °C
         below target. This happens at every load start from cool.
      2. Conditional integration is blocked whenever `raw > applied` below
         target, and the applied value almost always sits below raw.
         Reductions apply at once, but recoveries are slew-limited, so with
         sensor ripple the cap follows the lower envelope of the PID output.
         Flooring the cap to integer MHz removes the remaining margin.
      3. Each ripple excursion of `raw` above 1 back-calculates the integral
         down again.

      Net effect: the integral can fall but practically never rise.
    - **Reproduction** with the real `PID` class on a synthetic P-cluster
      plant (4 Hz, ±1.5 °C ripple, production slew and quantisation). Current
      semantics settle 12 °C below target and never reach it. With the
      retired guard's integrator semantics (same gains; conditional
      integration on the PID's own output, no back-calculation, integral
      starting at 1) they settle 1.2–1.4 °C below target, reached in 8–13 s.
    - **Comparison.** The retired guard used the same gains with the fan at
      12 and held its hottest zone at ≈ 91 °C (target 93 °C, staircase
      ceiling from 88 °C; doc/48 §0).
    - **Cost.** Estimated −10 to −25 % P-core clock under sustained CPU load,
      to be measured.
    - **Fix.** Shipped in Stage A2 (27 September, 14:34; doc/49): the
      conditional integrator together with the guard-aware setpoint for
      defect 28. Live vecfp A/B: +7.0 % throughput, P clock +6.7 %. The GPU PID shares the class and is
      reviewed in the same change (TH ceiling dither, runs 12/13).

28. **The CPU target of 90 °C sits on the guard's immediate-projection
    boundary (latent; found 27 September, 13:20).**
    - **The rule.** The guard aborts at once when a zone's 2 s
      least-squares trend is ≥ 93 − 3 = 90 °C and trend + 2 s × slope
      ≥ 93 °C.
    - **Why 90 °C conflicts.** A controller that really holds the CPU control
      signal (the maximum of those trends) at 90 °C trips this with any rise
      ≥ 1.5 °C/s. At 0.25 s sampling, one +5 °C P-cluster sample step raises
      the trend by ≈ 2 °C and the slope by ≈ 1.7 °C/s.
    - **Replay.** The guard's exact projection rule was replayed on the
      retired guard's full-load traces (fan 12, 500 ms samples, hottest-zone
      median shifted), per 5-minute steady segment:

      | Hottest-zone median | Immediate projected aborts | Notes |
      | --- | --- | --- |
      | 90 °C | 85–133 | first within 1–17 s |
      | 89 °C | 20–50 | |
      | 88 °C | none (all three runs) | raw peaks ≤ 90.5 °C |

    - **Why it is hidden.** Defect 27 masks this today: the plateau is
      ≈ 81 °C.
    - **Consequence.** With the guard unchanged, the highest usable CPU
      target under full load is ≈ 88 °C.
    - **Resolution** (doc/49, Stage A2). The operator keeps target 90 °C as
      the cap. The effective setpoint has a ceiling of 87 °C, below the
      no-confirmation zone, and backs off on near-misses. The ripple twin
      shows no guard trips in 3 h.

29. **The guard had no acquisition grace near the limits (27 September,
    16:42; doc/51).**
    - **What happened.** At the new 86 °C CPU operating point, within 10 °C
      of the 93 °C abort, one late host frame (over 1 s) reset the 2 s slope
      window. Three 0.1 s checks without a snapshot then aborted a healthy
      pure-CPU run (85–86 °C, projection 86 °C).
    - **Measurements.** Sensor reads at the service's nice −10 under full
      load were fast: `nvidia-smi` p95 84 ms, a full read at most 105 ms.
      The slow frames are rare outliers.
    - **Fix.** A 0.6 s grace at 5–10 °C margin (1.0 s from 10 °C unchanged,
      none within 5 °C). The guard is blind for at most 1.6 s, which at the
      fastest regulated rise (~2 °C/s) is ~3.2 °C, inside the 5 °C margin.
      The sampler now logs acquisitions slower than 0.5 s with per-part
      timings.

30. **A CPU command outlived the guard's evidence window (27 September,
    16:59; doc/51).**
    - **What happened.** At a sudden 20-core step the guard aborted with
      "CPU actuator unhealthy". The owner's own checks passed and its sysfs
      readback was fast (at most 13 ms). But cppc applies new maxima
      through kernel work items that run late under full load, and the
      policy sent one CPU command per tick while the caps ramped. So a
      verified command could outlive the 0.5 s evidence window, which was
      shorter than the owner's own 0.5 s apply wait plus writes.
    - **Fix.** CPU commands are shaped: 25 MHz steps, raises only past
      50 MHz, reductions immediate, the class maximum reached exactly. The
      owner's apply wait is now 1.0 s and the guard's CPU evidence window
      1.5 s. A hung owner is still caught within 1.5 s, and the temperature
      limits are independent of this evidence. The owner logs commands
      slower than 0.3 s.
    - **Verification.** LLM + 20 workers passed with no event (17:08–17:14).

31. **CPU owner evidence expired again under saturation (27 September,
    17:38; doc/52).**
    - **What happened.** In the first 2200 MHz ladder run, the guard
      aborted with "CPU actuator unhealthy" 61 s after the 20-worker step.
      The system was saturated: trace rows came 1.0–1.4 s apart.
    - **What was ruled out.** No slow CPU command was logged (over 0.3 s)
      and there was no kernel message. The GPU was healthy.
    - **Cause.** Not found, because no diagnostics existed for the gap.
    - **Change: diagnostics only.** The CPU and fan owners log evidence
      gaps longer than their publish interval + 0.4 s, and readbacks slower
      than 0.3 s. The guard logs the latched reason and the age once an
      owner's evidence becomes invalid.
    - **Result.** The reviewed repeat passed with no gap logged. Open until
      the next occurrence names the gap.

32. **Vendor watch flagged the GPU during ramps (27 September, 17:47;
    doc/52).**
    - **What happened.** The measured GPU clock lags a ramping cap by
      1–3 s, so REARM and ramp cycles held a deficit over 100 MHz for 5 s
      and raised the flag.
    - **Fix.** A deficit counts only once the cap has been steady for 3 s
      (a change of more than 50 MHz restarts the window). Reporting only;
      control is unaffected.

33. **A CUDA teardown tripped the GPU owner (27 September, 18:14 and at
    every burn-in end; doc/53).**
    - **What happened.** When vLLM stopped, or a 100 GB burn-in exited,
      `/proc/driver/nvidia` reads blocked for 0.5–0.7 s.
      `LiveOwnership` stamped its reading at the start of the call. The
      slow driver-epoch read then aged it past the setter's 0.5 s window,
      and the GPU owner tripped the shared abort without logging why.
    - **Fix.** The reading is stamped after its checks; slow checks are
      logged. The GPU owner logs the failed-read exception, and "control
      cycle failed" now carries the exception text.
    - **33b (19:01, end of the 1700 MHz worst-case step).** The
      ownership check took 0.54 s, and the setter evidence failed its 0.5 s
      freshness ("GPU setter evidence timeline invalid"). The in-process
      safe state held and re-armed without a process restart. Fix (pending
      install after the ladder): GPU ownership and setter evidence are
      valid for 1.5 s (`GPU_EVIDENCE_MAX_AGE_S`), like the CPU evidence
      (defect 30).

34. **The GPU loop did not see the ACPI GPU zone (27 September, 18:15;
    doc/53).**
    - **What happened.** Under the matrix burn-in at 2200 MHz (66 W), TGPU
      rose 3–4 °C/s to a projection over 93 °C while the nvidia sensor
      read 68 °C. TGPU was also used as a CPU proxy.
    - **Fix.** A guard-aware zone loop on TGPU (the cluster loop, setpoint
      86 °C) bounds the GPU cap, with a last-resort band and a ramp that
      slows with TGPU headroom. The policy's breach mirror covers TGPU, and
      TGPU is no longer a CPU proxy.
    - **Twin check.** 0 guard events in the burn-in (the old loop had
      1126).

35. **Lowering the entry ceiling below the previous lock failed the start
    (27 September, 18:19).**
    - **What happened.** "GPU clock did not settle under the entry
      ceiling"; the second start settled.
    - **Now avoided.** The entry ceiling changes live (no restart). The
      start check itself is unchanged.
    - **Still open.** The same message recurs at some in-process re-arms
      after an abort; the next in-process attempt settles. The settle
      diagnostics (requested, acknowledged and measured clock over the
      wait) are not yet logged.

36. **The GPU cap stayed at 2200 MHz at idle (27 September, 20:40; found by
    the operator: "der GPU-Schutz ist im Moment nicht aktiv, Cap 2200
    aktiv").**
    - **What happened.** After the GPU-only burn-in the service stayed in
      RUN with cap 2200 and vLLM stopped. The idle fallback required GPU
      utilisation < 5 %, but GB10 reads 8–11 % with nothing running
      (display and driver housekeeping). The only other return to the
      entry ceiling is REARM on a vLLM prefill, so without vLLM the next
      cold load would have started at 2200 MHz, against the PSU rule.
    - **Mitigation.** Live override `gpu_max_mhz` 1700 until the fix.
    - **Fix (build 20260927T204957).** The idle threshold is a setting,
      `idle_util_threshold`, default 20 % (operator: "Leerlaufschwelle 20 %
      ist gut"), live-tunable 5–50 % and validated below the busy threshold.
      It replaces the hard-coded 5 % in the idle timer, the completion
      check and the GPU workload detection.
    - **Verified live.** At idle (11 % utilisation) the mode is COOLDOWN
      and the cap is 1700 MHz without the override, with maximum 2200.

37. **TSOC fed TGPU spikes into the CPU proxy (27 September, found in the
    TGPU loop check at 2200 MHz, 20:55:29).**
    - **What happened.** With the retuned zone loop the GPU-only burn-in at
      2200 MHz still had six cuts of 100 MHz or more. The first one, 2200 →
      1900 MHz in a single tick, came with CPU cluster cuts as well. TGPU
      jumped 78.7 → 86.0 °C in 1 s, and the CPU projection reached
      96.55 °C while the four CPU zones read 52–65 °C.
    - **Cause.** TSOC is the firmware maximum of all SoC zones, TGPU
      included. Under GPU load it equals TGPU (1099 of 1114 samples); it
      exceeds every exposed zone only by sampling skew (0.27 % of 53,201
      samples, at most 1.1 °C). Defect 34 removed TGPU from the CPU proxy,
      but TSOC still carried it. The CPU setpoint backed off, the CPU
      last-resort band cut, and the balance spilled the relief to the GPU.
      The TGPU zone loop itself held its 2 %-per-tick spike step.
    - **Fix (pending install after the thermal step measurement).** TSOC is
      no CPU proxy (`policy.SOC_MAX_ZONE`); the four CPU zones are read
      individually. The independent guard keeps TSOC at the ACPI abort.
      Regression test `test_tsoc_mirroring_a_tgpu_spike_is_no_cpu_proxy`.

End-of-run markers: long runs (sustained load, trial series, identification)
write a JSON marker to `/var/lib/spark-energy/markers/` on any exit; the agent
watches markers and service abort lines instead of polling.
