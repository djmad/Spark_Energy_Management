# 57 — vLLM `/metrics` poller leak; energy_control stops reading vLLM (1 October 2026)

A fix request came from another agent session on this machine (1 October
2026). Its diagnosis: vLLM's HTTP front end answered `/metrics` and `/health` in about 11 s,
so every vLLM consumer with a short timeout saw vLLM as down. The cause was
`energy_control`, which had 292 threads after 285 in-process run restarts.

## Cause (confirmed)

- `run_service` created a `TraceWriter` with a `VllmTokenCounters` thread
  (1 Hz `/metrics`, 0.5 s timeout) once per run, after the supervisor started.
- Nothing closed the writer: not the run's `finally`, and not the
  "trace disabled" branch. So every run that started leaked one poller thread
  and one open trace file until the process exited.
- With vLLM slower than 0.5 s, each poll abandoned its connection. The abandoned
  connections filled vLLM's accept queue (Recv-Q about 2000 of 2048) and slowed
  it further, so the failure fed itself.
- The sampler process polled the vLLM queue gauges as well (`vllm_queue=True`),
  once per process, not per run.

## Operator decision

> achtung, das vllm ist nicht immer verfügbar.. und es ist in keiner weise
> mehr relevant für die energiesteuerung. ausschliesslich relevant ist die nun
> verwendete gpu auslastung

The fix therefore goes further than the requested close: the service reads
nothing from vLLM. Since 28 September load detection is GPU utilisation only
(doc/55 §9), and the queue signals only fed `prefill_rearm` (off by default)
and an early idle on request completion. Utilisation idle covers that after
1 s.

## Change

- `energy_control/service.py`:
  - **Sampler:** `service_collector` without the vLLM queue poll.
  - **Policy input:** `_signals` carries no queue fields (active jobs, prefill
    arrival and workload done keep their defaults: none, false, false).
  - **Trace writer:** no token counters. It is closed in the run's `finally`
    and when tracing is disabled (`_close_trace`).
- Unchanged:
  - The owned trial tools (`trial_runner`, `observer`, `passive_probe`) still
    read vLLM, because their trials send prompts to it. They are not the
    service.
  - In service mode the supervisor already refuses owned LLM requests.
- Test `test_service.test_runs_never_poll_vllm_and_close_their_trace` covers
  the leak:
  - It runs two runs in a row, one ending normally and one with tracing
    disabled.
  - Each run must close its writer, with no vLLM poller and no new `vllm*`
    thread.
  - `main`, `run_service` and `_signals` must not name the vLLM pollers.
  - It fails on the previous code, which shows "the service must not poll
    vLLM" and a writer that was never closed.
- Suite: 760 tests OK; headless queue scenario OK.
- Docs:
  - `AGENTS.md` records the operator rule.
  - `INSTALL.md` no longer describes vLLM as an input.
  - The release notes have an Unreleased entry.

## Deployed (build 20261001T160203)

The change was installed while the hardware claim was held. `energy_control`
was restarted at 16:02 under the running LLM load:
- The first start failed, "GPU clock did not settle under the entry ceiling"
  (defect 35).
- The in-process retry ran after 27 s.
- The cap ramped 1700 → 2500 MHz in 10 s.

Verification right after the start:
- **Threads:** the main process has 7 threads (8 after the 15:14 boot with the
  old code). The sampler process has 2.
- **vLLM connections:** none of the service's 8 processes opened a connection
  to port 8000 within 20 s.
- **vLLM:** accept queue on 127.0.0.1:8000 at Recv-Q 0, `/metrics` in 4 ms.
- **Status:** fresh.

## Open: abort storm on 29 September – 1 October (not changed here)

The journal of the boot from 29 September 10:00 to 1 October 13:22 explains
the 285 run restarts. The pattern repeated every hour:
- **Policy aborts:** 2–11 per hour, all "predicted abort-limit breach
  (ACPI 93 C / GPU 85 C)". That is 146 policy aborts and 241 safe-state
  entries, with the GPU maximum at 2500 MHz under LLM.
- **Guard aborts:** 61 projected temperature breaches (TGPU, GPU), and 221
  "workload control unhealthy" (owned=0) aborts. That check reports the shared
  abort latch, so these 221 follow the policy aborts and are not separate
  causes.
- **Failed starts:** 447 failures with "GPU clock did not settle under the
  entry ceiling" (defect 35). After each abort, the restart under load failed
  2–4 times on average, up to 36 per hour, before a run started.

Each abort holds the GPU at 200–500 MHz until the next start succeeds. Two
questions need their own analysis:
- why the zone loops do not derate before the 93 °C ACPI or 85 °C GPU
  projection at 2500 MHz;
- how to make the start under load succeed the first time (defect 35).
