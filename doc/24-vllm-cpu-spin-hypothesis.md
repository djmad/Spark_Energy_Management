# vLLM CPU spin-wait hypothesis — read-only, not a diagnosis

The operator reports that a typical busy state consumes 4–5 CPU cores while
LLM/GPU activity is high. Before attributing all of that CPU heat to useful
tokenization or compensating only with clock caps, check whether the inference
stack has avoidable waiting work. This is an investigation branch, **not** a
request to patch or restart the running LLM.

On 26 September 2026, the live `vllm_node` container reported package version
`0.1.1.dev7+g8c1d1c297.d20260911`. Its installed
`/usr/local/lib/python3.12/dist-packages/vllm/distributed/device_communicators/shm_broadcast.py`
had SHA-256
`f3e7a3f3ff713fccbd535e0d41650ee05bdf8677e83d4b90519910645022c30b`.
The `SpinCondition` reader constructor defaults `busy_loop_s` to 1 second;
its `wait()` calls `sched_yield()` until that interval has passed since the
last read. The local reader call site does not override the default. These are
facts about the installed source, **not proof that the active process executes
that branch**. No source file was copied, edited or executed as a test.

A separate [GB10 field investigation](https://artifacts.nacyot.com/vllm-spin-wait-gb10/)
measured substantial CPU heat associated with this wait path on a different
machine/software build. It is primary field evidence for that setup, not a
Lenovo PGX measurement or a justified patch for this host. A targeted search
of [NVIDIA's DGX Spark hardware guide](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
and [Lenovo's PGX product guide](https://lenovopress.lenovo.com/lp2321-thinkstation-pgx)
did not provide authoritative component definitions or internal refresh timing
for the seven ACPI path names. Community boot logs show the same names, but
that does not qualify their physical mapping here.

Read-only live checks found two active and zero waiting vLLM requests at one
instant. A 5-second `pidstat` window averaged 1.2% CPU for the API process and
2.0% for its engine process. A later 3-second engine window averaged 33%, with
one one-second sample at 89%. These short, transient samples do **not**
establish persistent 4–5-core spin, nor do they rule it out under the
operator's typical queue. The active tensor-parallel topology and reader-path
execution were not established. The CPU percentages are process usage, not
whole-system power.

A later passive 5-second per-thread `pidstat` sample of engine PID 51808
averaged 101.4% process CPU, with 100.0% attributed to its main thread; no
other engine thread averaged above 1%. A nearby read of the existing vLLM
metrics endpoint reported two running and two waiting requests. This is
evidence of roughly one busy core during a nonempty queue, **not** proof that
the core was spinning, that it persisted at that level, or that it explains
the reported 4–5 CPU cores. The queue read and CPU sample were not an atomic
measurement. No traffic was generated and no process was modified.

Next evidence, before any vLLM change: during a separately approved, bounded
normal workload window, sample only per-process/per-thread CPU utilization,
queue/active counts, pinned temperatures and accepted clocks at synchronized
timestamps. If CPU remains high during decode or idle, use a non-mutating
stack/profile observation to attribute it to the actual wait path. Compare
against a separate matched window only after the workload gate and thermal
guard are qualified. Do not apply another system's source patch, restart the
LLM, or weaken the 1800 MHz/93°C safety envelope based on this note.
