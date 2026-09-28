"""Supervised live trials (goal v2, work-plan 6), run by the root main agent only.

Each trial passes through the owners' safe state on both ends: the installed
service is stopped (GPU 200-500 MHz, CPU minimum, fan 12), a trial-bound
commissioning supervisor runs with a durable trial plan and the same live
owners, then the service is restarted. The plan's duration bounds the guard.
Requires the operator's hardware grant (AGENTS.md) and this session's claim.
Synthetic prompts only; no request or response content is logged.
"""
import argparse
from dataclasses import dataclass
from functools import partial
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from threading import Event
from time import monotonic, sleep, strftime

from .broker import Config, config_fingerprint
from .recorder import CommissioningRecorder
from .service import TraceWriter, wait_until_cool
from .supervisor import ResidentSupervisor
from .trial_plan import TrialProposal
from .limits import GPU_HARD_MAX_MHZ

CLAIM = Path("/run/spark-energy/agent-command")
COMMISSIONING_RUNS = Path("/var/lib/spark-energy/commissioning-runs")
TRIAL_TRACES = Path("/var/lib/spark-energy/trial-traces")
MODEL = "nvidia/Gemma-4-26B-A4B-NVFP4"


def log(message):
    print(f"trial: {message}", file=sys.stderr, flush=True)


def synthetic_prompt(words):
    """Deterministic filler to create a heavy prefill; never user content."""
    base = ("thermal copper heat sink fan clock ceiling power supply spike prefill decode "
            "entry ramp guard owner recorder evidence trace model twin calibration ").split()
    return " ".join(base[i % len(base)] for i in range(words))


def request_body(words, max_tokens, *, nonce=None):
    # ignore_eos makes vLLM generate exactly max_tokens: a known, sustained decode.
    # A random opening defeats vLLM's prefix cache: identical synthetic prompts
    # were 99 % cache hits, so earlier trials tested decode, not prefill.
    nonce = nonce or secrets.token_hex(8)
    return {"model": MODEL, "stream": True, "max_tokens": max_tokens, "temperature": 0.0,
            "ignore_eos": True,
            "messages": [{"role": "user", "content": f"Run {nonce}. Summarise the following "
                          "list of words in one long essay: " + synthetic_prompt(words)}]}


def token_upper_bound(request):
    """Conservative upper bound for our own synthetic prompts (not user input)."""
    text = request["messages"][0]["content"]
    # Measured 1.05 tokens per word for this filler (3 000 words -> 3 161 tokens).
    return (int(1.3 * len(text.split())) + 64, request["max_tokens"])


def prompt_cap_for(words):
    return int(1.3 * (words + 16)) + 128


def entry_trial_plan(mhz, repetition, *, words, max_tokens, jobs=1, duration_s=1800,
                     fan_floor=12, cpu_fast_max_mhz=None, cpu_entry_ratio=None,
                     cpu_slow_max_mhz=None):
    # Identification trials hold a lower fan floor (minimum = preferred = floor);
    # the controller may still raise it near a limit, and the trace records it.
    # CPU-impact trials cap the fast cores and start them at that cap.
    cpu = {k: v for k, v in (("cpu_fast_max_mhz", cpu_fast_max_mhz),
                             ("cpu_slow_max_mhz", cpu_slow_max_mhz),
                             ("cpu_entry_ratio", cpu_entry_ratio)) if v is not None}
    config = Config(gpu_max_mhz=mhz, gpu_entry_mhz=mhz, fan_min_state=fan_floor,
                    fan_preferred_state=fan_floor, **cpu)
    prompt_cap = prompt_cap_for(words)
    proposal = TrialProposal(3, repetition, duration_s, 0, jobs, 0, mhz, mhz, fan_floor,
                             prompt_cap, max_tokens, admission_cap=jobs,
                             reserved_token_cap=jobs * (prompt_cap + max_tokens),
                             cpu_fast_max_mhz=config.cpu_fast_max_mhz,
                             cpu_slow_max_mhz=config.cpu_slow_max_mhz,
                             config_digest=config_fingerprint(config))
    return proposal, config


def vllm_counters():
    """Finished/aborted requests, generated tokens and preemptions (diagnostics only)."""
    try:
        connection = HTTPConnection("127.0.0.1", 8000, timeout=2)
        connection.request("GET", "/metrics")
        text = connection.getresponse().read(512 * 1024).decode()
        connection.close()
    except Exception:
        return {}
    totals = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for key, prefix in (("finished", "vllm:request_success_total"),
                            ("generated_tokens", "vllm:generation_tokens_total"),
                            ("preemptions", "vllm:num_preemptions_total"),
                            ("prompt_tokens", "vllm:prompt_tokens_total"),
                            ("prompt_tokens_cached", "vllm:prompt_tokens_cached_total"),
                            ("ttft_s_sum", "vllm:time_to_first_token_seconds_sum"),
                            ("ttft_count", "vllm:time_to_first_token_seconds_count"),
                            ("prefill_s_sum", "vllm:request_prefill_time_seconds_sum"),
                            ("decode_s_sum", "vllm:request_decode_time_seconds_sum")):
            if line.startswith(prefix + "{") or line.startswith(prefix + " "):
                if key == "finished" and 'finished_reason="abort"' in line:
                    totals["aborted"] = totals.get("aborted", 0) + float(line.split()[-1])
                totals[key] = totals.get(key, 0) + float(line.split()[-1])
    return totals


@dataclass
class TrialResult:
    name: str
    run_id: str
    aborted: bool
    reasons: tuple
    request_faults: tuple
    peak_gpu_w: float
    peak_gpu_mhz: float
    peak_gpu_c: float
    peak_acpi_c: float
    trace: str
    codes: dict
    vllm_delta: dict = None


def _systemctl(action):
    """Supervised stop/start. Trials restart the service often; clear systemd's
    start-limit counter first (it hit 'start-limit-hit' after six trials) and
    retry a failed start once. The unit's own limit still guards unattended
    restart storms."""
    unit = "energy_control.service"
    if action == "start":
        subprocess.run(["/usr/bin/systemctl", "reset-failed", unit], check=False,
                       timeout=30, stdin=subprocess.DEVNULL)
    result = subprocess.run(["/usr/bin/systemctl", action, unit], check=False,
                            timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0 and action == "start":
        sleep(20)
        subprocess.run(["/usr/bin/systemctl", "reset-failed", unit], check=False,
                       timeout=30, stdin=subprocess.DEVNULL)
        result = subprocess.run(["/usr/bin/systemctl", action, unit], check=False,
                                timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        raise ServiceRestartError(f"systemctl {action} {unit} failed ({result.returncode})")


class ServiceRestartError(RuntimeError):
    """Harness problem after a trial; never a thermal or electrical step failure."""


def run_entry_trial(mhz, repetition, *, words=19000, max_tokens=10000, jobs=4,
                    cold_gpu_c=45.0, cold_acpi_c=55.0, service_control=_systemctl,
                    fan_floor=12, cool_after_s=0.0, kind="entry", cpu_fast_max_mhz=None,
                    cpu_entry_ratio=None, cpu_slow_max_mhz=None):
    """Cold/idle-to-prefill at entry ceiling ``mhz`` (entry = max for this trial)."""
    from .collector import VllmTokenCounters, service_collector
    from .host_sampler import HostSafetySampler, HostSamplerProcess
    from .live import boot_id, driver_epoch, live_cpu_factory, live_fan_factory, live_gpu_factory
    from .guard_host_source import guard_host_source
    if os.geteuid() != 0 or not CLAIM.exists():
        raise PermissionError("root and this session's hardware claim required")
    if not 200 < mhz <= GPU_HARD_MAX_MHZ:
        raise ValueError(f"entry ceiling must stay within the {GPU_HARD_MAX_MHZ} MHz hard limit")
    if not 1 <= jobs <= 4:
        raise ValueError("1..4 concurrent owned jobs")
    if not 2 <= fan_floor <= 12 or not 0 <= cool_after_s <= 900:
        raise ValueError("fan floor 2..12 and cool-down hold 0..900 s")
    proposal, config = entry_trial_plan(mhz, repetition, words=words, max_tokens=max_tokens,
                                        jobs=jobs, fan_floor=fan_floor,
                                        cpu_fast_max_mhz=cpu_fast_max_mhz,
                                        cpu_entry_ratio=cpu_entry_ratio,
                                        cpu_slow_max_mhz=cpu_slow_max_mhz)
    cpu_tag = (f"-p{cpu_fast_max_mhz}" if cpu_fast_max_mhz else "") + (
        f"s{cpu_slow_max_mhz}" if cpu_slow_max_mhz else "")
    name = (f"{kind}-{mhz}{cpu_tag}-f{fan_floor}-j{jobs}-r{repetition}-"
            f"{strftime('%Y%m%dT%H%M%S')}")
    log(f"{name}: stopping energy_control (safe state)")
    service_control("stop")
    source = HostSamplerProcess(collector_factory=partial(service_collector, vllm_queue=True))
    source.start()
    supervisor = recorder = None
    try:
        thermal = HostSafetySampler(source)
        stop = Event()
        deadline = monotonic() + 600
        log(f"waiting for cold idle: GPU <= {cold_gpu_c} C, ACPI <= {cold_acpi_c} C, util < 20 %, no vLLM requests")
        while True:
            if not wait_until_cool(thermal, stop, dwell_s=5.0):
                raise RuntimeError("stopped while cooling")
            readout = thermal.last_readout
            if (readout.gpu.temperature_c <= cold_gpu_c and readout.gpu.utilization_pct < 20
                    and max(c for _, c in readout.acpi_temperatures) <= cold_acpi_c
                    and not readout.active_jobs and not readout.queued_jobs):
                break
            if monotonic() > deadline:
                raise RuntimeError("machine did not reach cold idle within 10 min")
            sleep(2)
        COMMISSIONING_RUNS.mkdir(mode=0o700, parents=True, exist_ok=True)
        recorder = CommissioningRecorder(COMMISSIONING_RUNS, boot_id=boot_id())
        recorder.write_trial_plan(proposal)
        epoch, owner = driver_epoch(), f"trial-{recorder.run_id[:12]}"
        supervisor = ResidentSupervisor(
            recorder, config,
            gpu_factory=partial(live_gpu_factory, driver_epoch=epoch, owner_epoch=owner),
            cpu_factory=partial(live_cpu_factory, slow_max_mhz=config.cpu_slow_max_mhz,
                                fast_max_mhz=config.cpu_fast_max_mhz),
            fan_factory=live_fan_factory, policy_thermal=thermal, driver_epoch=epoch,
            owner_epoch=owner, guard_deadline_s=2.0,
            guard_source_factory=partial(guard_host_source, collector_factory=service_collector))
        dispatcher = supervisor.attach_dispatcher(
            partial(HTTPConnection, "127.0.0.1", 8000, timeout=5), token_upper_bound,
            request_deadline_s=min(1800, proposal.duration_s - 60))
        trace = TraceWriter(TRIAL_TRACES / name, period_s=0.25, keep_days=10**6,
                            token_counters=VllmTokenCounters())
        supervisor.start()
        supervisor.warm_up()
        if fan_floor < 12:
            # Identification: start at the target floor at once (normal staging
            # would step down one state per 15 s); safety escalation still applies.
            supervisor.policy.supervisor.fan_state = fan_floor
        log(f"{name}: run {recorder.run_id}, entry {mhz} MHz applied; submitting {jobs} owned "
            f"requests (~{words} words in, {max_tokens} tokens out each)")
        try:
            before = vllm_counters()
            for _ in range(jobs):
                dispatcher.submit(request_body(words, max_tokens))
        except Exception as exc:
            raise RuntimeError(f"owned request submission failed: {type(exc).__name__}: {exc}; "
                               f"dispatcher faults {dispatcher.faults()}") from exc
        started = monotonic()
        observe_s = proposal.duration_s - 30
        from .service import STATUS_PATH, CpuDemand, status_payload, write_readiness
        status_path, next_status = STATUS_PATH, 0.0
        from .live import QueueSignals
        queue, demand = QueueSignals(), CpuDemand()
        done_at = None
        while monotonic() - started < observe_s and supervisor.state == "RUNNING":
            if done_at is None and monotonic() - started >= 5 and dispatcher.join_workers(0):
                done_at = monotonic()  # Load finished; optional cool-down hold follows.
            if done_at is not None and monotonic() - done_at >= cool_after_s:
                break
            tick = monotonic()
            readout = thermal.last_readout
            limits = supervisor.tick(gpu_util_pct=readout.gpu.utilization_pct,
                                     cpu_util_pct=readout.cpu_util_pct,
                                     cpu_demand_active=demand(readout.cpu_util_pct),
                                     **queue(readout.active_jobs, readout.queued_jobs))
            trace.write(thermal.last_readout, supervisor.applied, limits.mode)
            if status_path is not None and tick >= next_status:
                # The dashboard's single reader keeps working while the service
                # is stopped for a trial; publishing never affects the trial.
                next_status = tick + 1.0
                try:
                    board = trace._board_temperatures()
                    payload = status_payload(thermal.last_readout, supervisor.applied, limits,
                                             config, recorder.run_id,
                                             {"nvme": board.get("nvme_c"),
                                              "wifi": board.get("wifi_c")})
                    payload["mode"] = f"trial:{name}:{payload['mode']}"
                    write_readiness(status_path, payload)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    log(f"status export disabled: {type(exc).__name__}: {exc}")
                    status_path = None
            sleep(max(0.0, 0.25 - (monotonic() - tick)))
        dispatcher.close_admission()
        dispatcher.cancel_owned_requests()
        dispatcher.join_workers(5)
        trace.close()
        aborted = supervisor.state != "RUNNING"
        reasons, faults = supervisor.reasons, dispatcher.faults()
        codes = supervisor.close()
        after = vllm_counters()
        delta = {key: after.get(key, 0) - before.get(key, 0) for key in after}
        rows = [json.loads(line) for line in
                (TRIAL_TRACES / name).glob("*.jsonl").__next__().read_text().splitlines()]
        result = TrialResult(name, recorder.run_id, aborted, reasons, faults,
                             max(r["gpu_w"] or 0 for r in rows), max(r["gpu_mhz"] for r in rows),
                             max(r["gpu_c"] for r in rows),
                             max(max(r["acpi_c"].values()) for r in rows), str(TRIAL_TRACES / name),
                             codes, delta)
        log(f"{name}: {result}")
        return result
    finally:
        if supervisor is not None and supervisor.state != "CLOSED":
            supervisor.close()
        if recorder is not None:
            recorder.close()
        source.close()
        log("restarting energy_control")
        service_control("start")


SERIES_DIR = Path("/var/lib/spark-energy")


def series_log(label):
    return SERIES_DIR / f"entry-series-{label}.jsonl"


def _durable_line(path, row):
    """Append and fsync one line; an intent without a result marks a failed step."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def previous_failures(path):
    """Steps with an intent but no result (e.g. a power loss) or a failed result."""
    if not path.exists():
        return set()
    open_intents, failed = {}, set()
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        key = (row["mhz"], row["repetition"])
        if row["event"] == "intent":
            open_intents[key] = row
        elif row["event"] in ("result", "correction"):
            # A correction is a reviewed reclassification (e.g. a harness error
            # after a trial that passed), never an automatic retry.
            open_intents.pop(key, None)
            if row["passed"]:
                failed.discard(row["mhz"])
            else:
                failed.add(row["mhz"])
    return failed | {mhz for mhz, _ in open_intents}


MAX_CACHE_RATIO = 0.2


def cache_ratio(counters):
    """Share of prompt tokens served from vLLM's prefix cache during a trial."""
    prompt = (counters or {}).get("prompt_tokens")
    if not prompt:
        return None
    return round(counters.get("prompt_tokens_cached", 0) / prompt, 3)


def run_entry_series(steps, repetitions, *, label, **trial_options):
    """Qualify the entry ceiling in bounded steps; stop at the first failure.

    Never repeats a failed step automatically: a step that failed before (or
    left an intent without a result) ends the series before it is retried.
    """
    log_path = series_log(label)
    failed_before = previous_failures(log_path)
    passed = []
    for mhz in steps:
        if any(mhz >= failed for failed in failed_before):
            log(f"series: {mhz} MHz at or above a previously failed step; stopping")
            break
        for repetition in range(1, repetitions + 1):
            _durable_line(log_path, {"event": "intent", "mhz": mhz, "repetition": repetition,
                                       "utc": strftime("%Y-%m-%dT%H:%M:%S%z")})
            try:
                result = run_entry_trial(mhz, repetition, **trial_options)
                # Entry qualification asks about cold-start power/thermal stability.
                # An abort caused only by an owned-request fault (no guard/safety
                # reason) is a recorded workload anomaly, not a step failure.
                workload_only = bool(result.reasons) and all(
                    reason.startswith("dispatcher:") for reason in result.reasons)
                ok = ((not result.aborted or workload_only)
                      and result.peak_gpu_mhz <= mhz + 50)
                cached = cache_ratio(result.vllm_delta)
                _durable_line(log_path, {"event": "result", "mhz": mhz,
                                           "repetition": repetition, "passed": ok,
                                           "workload_anomaly": workload_only,
                                           "prefix_cache_ratio": cached,
                                           **result.__dict__})
                if cached is None or cached > MAX_CACHE_RATIO:
                    # A cache-served prompt is no prefill test: neither pass nor fail.
                    _durable_line(log_path, {"event": "harness_error", "mhz": mhz,
                                               "repetition": repetition,
                                               "error": f"prefix cache ratio {cached}"})
                    log(f"series: {mhz} MHz r{repetition} prefix cache ratio {cached}; "
                        "stopping for review")
                    return passed
            except ServiceRestartError as exc:
                _durable_line(log_path, {"event": "harness_error", "mhz": mhz,
                                           "repetition": repetition,
                                           "error": f"{type(exc).__name__}: {exc}"})
                log(f"series: harness error after {mhz} MHz r{repetition}; stopping for review")
                return passed
            except Exception as exc:
                _durable_line(log_path, {"event": "result", "mhz": mhz,
                                           "repetition": repetition, "passed": False,
                                           "error": f"{type(exc).__name__}: {exc}"})
                ok = False
            if not ok:
                log(f"series: {mhz} MHz repetition {repetition} failed; series ends")
                return passed
        passed.append(mhz)
        log(f"series: {mhz} MHz passed {repetitions} repetitions")
    return passed


IDENTIFY_LOG = SERIES_DIR / "identification-runs.jsonl"
CPU_IMPACT_LOG = SERIES_DIR / "cpu-impact-runs.jsonl"


def run_cpu_impact(mhz, fast_caps, slow_caps=None, **trial_options):
    """LLM throughput vs fast-core cap at a fixed GPU clock and fan 12.

    One trial per cap; stops at the first abort or harness error and never
    repeats a step (an intent without a result is left for review).
    """
    # Fast-only caps move vLLM's busy threads to the uncapped E-cores (live,
    # 27 September 2026); slow caps measure the whole CPU's influence.
    slow_caps = list(slow_caps) if slow_caps else [None] * len(fast_caps)
    if len(slow_caps) != len(fast_caps):
        raise ValueError("one slow cap per fast cap")
    results = []
    for index, (cap, slow) in enumerate(zip(fast_caps, slow_caps), 1):
        _durable_line(CPU_IMPACT_LOG, {"event": "intent", "mhz": mhz, "cpu_fast_max_mhz": cap,
                                       "cpu_slow_max_mhz": slow,
                                       "utc": strftime("%Y-%m-%dT%H:%M:%S%z")})
        result = run_entry_trial(mhz, index, kind="cpuimpact", cpu_fast_max_mhz=cap,
                                 cpu_slow_max_mhz=slow, cpu_entry_ratio=1.0, **trial_options)
        _durable_line(CPU_IMPACT_LOG, {"event": "result", "mhz": mhz, "cpu_fast_max_mhz": cap,
                                       "cpu_slow_max_mhz": slow,
                                       "prefix_cache_ratio": cache_ratio(result.vllm_delta),
                                       **result.__dict__})
        results.append(result)
        if result.aborted:
            log(f"cpu impact: {cap} MHz aborted {result.reasons}; stopping")
            break
    return results


def run_identification(mhz, fan_floors, *, cool_after_s=300.0, **trial_options):
    """Heat-up (4-job load) and cool-down curves at fixed clock, one per fan floor.

    Data collection for the twin (copper capacity, fan capacity), not a
    pass/fail qualification; stops at the first abort for review.
    """
    results = []
    for floor in fan_floors:
        _durable_line(IDENTIFY_LOG, {"event": "intent", "mhz": mhz, "fan_floor": floor,
                                     "utc": strftime("%Y-%m-%dT%H:%M:%S%z")})
        try:
            # Repetition 1 each: the fan floor distinguishes the runs (stage 3
            # allows at most 3 repetitions; a run index of 4 was refused live).
            result = run_entry_trial(mhz, 1, fan_floor=floor, cool_after_s=cool_after_s,
                                     kind="identify", **trial_options)
        except Exception as exc:
            _durable_line(IDENTIFY_LOG, {"event": "result", "mhz": mhz, "fan_floor": floor,
                                         "error": f"{type(exc).__name__}: {exc}"})
            log(f"identify: fan floor {floor} failed to run; stopping")
            break
        _durable_line(IDENTIFY_LOG, {"event": "result", "mhz": mhz, "fan_floor": floor,
                                     **result.__dict__})
        results.append(result)
        if result.aborted:
            log(f"identify: fan floor {floor} aborted ({result.reasons}); stopping for review")
            break
    return results


def main(argv=None):
    try:
        os.nice(-10)  # Same priority as the service: the guard must not starve.
    except OSError:
        pass
    parser = argparse.ArgumentParser(description="Supervised live energy_control trials")
    sub = parser.add_subparsers(dest="kind", required=True)
    entry = sub.add_parser("entry", help="cold/idle-to-prefill at an entry ceiling")
    entry.add_argument("--mhz", type=int, required=True)
    entry.add_argument("--repetition", type=int, default=1)
    for command in (entry,):
        command.add_argument("--words", type=int, default=19000)
        command.add_argument("--max-tokens", type=int, default=10000)
        command.add_argument("--jobs", type=int, default=4)
    series = sub.add_parser("series", help="entry-ceiling qualification series")
    series.add_argument("--from-mhz", type=int, default=1200)
    series.add_argument("--to-mhz", type=int, default=1800)
    series.add_argument("--step-mhz", type=int, default=100)
    series.add_argument("--repetitions", type=int, default=2)
    series.add_argument("--words", type=int, default=19000)
    series.add_argument("--max-tokens", type=int, default=10000)
    series.add_argument("--jobs", type=int, default=4)
    identify = sub.add_parser("identify", help="fan-floor identification runs for the twin")
    identify.add_argument("--mhz", type=int, required=True)
    identify.add_argument("--fan-floors", type=int, nargs="+", default=[12, 8, 4, 2])
    identify.add_argument("--cool-s", type=float, default=300.0)
    identify.add_argument("--words", type=int, default=19000)
    identify.add_argument("--max-tokens", type=int, default=10000)
    identify.add_argument("--jobs", type=int, default=4)
    impact = sub.add_parser("cpuimpact", help="LLM throughput vs fast-core cap")
    impact.add_argument("--mhz", type=int, required=True)
    impact.add_argument("--fast-caps", type=int, nargs="+", default=[3900, 2600, 1378])
    impact.add_argument("--slow-caps", type=int, nargs="+", default=None,
                        help="E-core caps paired with --fast-caps (default: unchanged)")
    impact.add_argument("--words", type=int, default=19000)
    impact.add_argument("--max-tokens", type=int, default=10000)
    impact.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args(argv)
    from .markers import write_marker
    try:
        return _run(args)
    except BaseException as exc:
        write_marker(f"trial-{args.kind}", "failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _run(args):
    from .markers import write_marker
    if args.kind == "identify":
        results = run_identification(args.mhz, args.fan_floors, cool_after_s=args.cool_s,
                                     words=args.words, max_tokens=args.max_tokens, jobs=args.jobs)
        print(json.dumps([r.__dict__ for r in results], default=str, indent=2))
        write_marker("trial-identify", "completed" if len(results) == len(args.fan_floors)
                     and not any(r.aborted for r in results) else "stopped",
                     runs=[(r.name, r.aborted, r.reasons) for r in results])
        # Non-zero on any abort so chained blocks stop for review.
        return 1 if len(results) < len(args.fan_floors) or any(r.aborted for r in results) else 0
    if args.kind == "cpuimpact":
        results = run_cpu_impact(args.mhz, args.fast_caps, args.slow_caps, words=args.words,
                                 max_tokens=args.max_tokens, jobs=args.jobs)
        print(json.dumps([r.__dict__ for r in results], default=str, indent=2))
        write_marker("trial-cpuimpact", "completed" if len(results) == len(args.fast_caps)
                     and not any(r.aborted for r in results) else "stopped",
                     runs=[(r.name, r.aborted, r.reasons) for r in results])
        return 1 if len(results) < len(args.fast_caps) or any(r.aborted for r in results) else 0
    if args.kind == "series":
        if not 1200 <= args.from_mhz <= args.to_mhz <= GPU_HARD_MAX_MHZ or not 25 <= args.step_mhz <= 200:
            raise SystemExit(f"series must stay within 1200..{GPU_HARD_MAX_MHZ} MHz in 25..200 MHz steps")
        label = f"w{args.words}-o{args.max_tokens}-j{args.jobs}-unique"
        passed = run_entry_series(range(args.from_mhz, args.to_mhz + 1, args.step_mhz),
                                  args.repetitions, label=label, words=args.words,
                                  max_tokens=args.max_tokens, jobs=args.jobs)
        print(json.dumps({"passed_steps": list(passed)}))
        write_marker("trial-series", "completed", passed_steps=list(passed))
        return 0
    result = run_entry_trial(args.mhz, args.repetition, words=args.words,
                             max_tokens=args.max_tokens, jobs=args.jobs)
    print(json.dumps(result.__dict__, default=str, indent=2))
    write_marker("trial-entry", "stopped" if result.aborted else "completed",
                 name=result.name, reasons=result.reasons)
    return 1 if result.aborted else 0


if __name__ == "__main__":
    sys.exit(main())
