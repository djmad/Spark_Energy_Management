"""Sustained combined-load block under the installed service's own control.

Keeps ``--jobs`` concurrent synthetic LLM requests running against the local
vLLM (unowned load from the service's point of view, like real users) and
duty-cycled CPU load on selected cores, for ``--minutes``. It stops all of its
load within about a second when energy_control's readiness file disappears
(service abort or stop), because the service cannot cancel unowned work.
Synthetic prompts only; no content is logged. Run by the root main agent under
the hardware claim.
"""
import argparse
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import sys
import socket
from threading import Event, Lock, Thread
import time
from multiprocessing import get_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from energy_control.trial_runner import MODEL, request_body  # noqa: E402
from moe_prompts import NAMES as MOE_DOMAINS, crisscross_request_body, moe_request_body  # noqa: E402

READINESS = Path("/run/spark-energy/entry-ceiling")


def cpu_worker(cpu, duty, stop):
    os.sched_setaffinity(0, {cpu})
    period = 0.1
    while not stop.is_set():
        busy_until = time.monotonic() + period * duty
        while time.monotonic() < busy_until:
            pass
        time.sleep(period * (1 - duty))


OPEN = set()  # Active connections; shut down on stop so blocked reads end at once.
OPEN_LOCK = Lock()


def shutdown_open_connections():
    with OPEN_LOCK:
        connections = list(OPEN)
    for connection in connections:
        try:
            if connection.sock is not None:
                connection.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def llm_worker(index, words, max_tokens, stop, stats, prompts=None):
    """``prompts``: None for the synthetic filler, else a list of domain names
    this worker cycles through (starting at its own index)."""
    turn = index
    import random
    rng = random.Random(index * 7919 + int(time.time()))
    while not stop.is_set():
        if prompts == "crisscross":
            domain = "crisscross"
        else:
            domain = None if not prompts else prompts[turn % len(prompts)]
        turn += 1
        # Long read timeout: 12 concurrent 20k-token prefills can take > 30 s to
        # the first token (run 8: 23 client timeouts). Stop latency does not
        # depend on it: the main thread shuts the sockets down on stop.
        connection = HTTPConnection("127.0.0.1", 8000, timeout=600)
        with OPEN_LOCK:
            OPEN.add(connection)
        try:
            if domain is None:
                request = request_body(words, max_tokens)
            elif domain == "crisscross":
                request = crisscross_request_body(MODEL, max_tokens, rng=rng)
            else:
                request = moe_request_body(MODEL, domain, max_tokens)
            request["stream_options"] = {"include_usage": True}
            body = json.dumps(request).encode()
            sent = time.monotonic()
            connection.request("POST", "/v1/chat/completions", body=body,
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                stats["errors"] += 1
                stop.wait(2)
                continue
            first = tokens = None
            while not stop.is_set():
                line = response.readline(65537)
                if not line or line.strip() == b"data: [DONE]":
                    break
                if first is None and b'"content":"' in line and b'"content":""' not in line:
                    first = time.monotonic()
                if b'"usage"' in line and b'"completion_tokens"' in line:
                    try:  # counts only; no content is kept
                        usage = json.loads(line[len(b"data: "):]).get("usage") or {}
                        tokens = usage.get("completion_tokens")
                    except ValueError:
                        pass
            if not stop.is_set():
                stats["completed"] += 1
                if domain is not None and first is not None and tokens:
                    ended = time.monotonic()
                    with OPEN_LOCK:
                        entry = stats["domains"].setdefault(domain, [])
                        entry.append((first - sent, tokens / max(ended - first, 1e-3)))
        except Exception:
            if not stop.is_set():
                stats["errors"] += 1
            stop.wait(1)
        finally:
            with OPEN_LOCK:
                OPEN.discard(connection)
            connection.close()  # On stop this closes the stream: vLLM aborts it.


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--minutes", type=float, default=15)
    parser.add_argument("--jobs", type=int, default=12)
    parser.add_argument("--words", type=int, default=19000)
    parser.add_argument("--max-tokens", type=int, default=10000)
    parser.add_argument("--cpus", type=int, nargs="*", default=[5, 6, 7, 8, 9])
    parser.add_argument("--cpu-duty", type=float, default=0.5)
    parser.add_argument("--prompt-set", choices=("filler", "moe", "crisscross"), default="filler",
                        help="moe: 20 domain-diverse long-form prompts (scripts/moe_prompts.py)")
    parser.add_argument("--domain", choices=MOE_DOMAINS,
                        help="with --prompt-set moe: every job on this one domain")
    args = parser.parse_args()
    prompts = None
    if args.prompt_set == "moe":
        prompts = [args.domain] if args.domain else list(MOE_DOMAINS)
    elif args.prompt_set == "crisscross":
        prompts = "crisscross"   # 5 random domains per request, task switch per paragraph
    if not READINESS.exists():
        raise SystemExit("energy_control is not running (no readiness file)")
    stop = Event()
    cpu_stop = get_context("spawn").Event()
    stats = {"completed": 0, "errors": 0, "domains": {}}
    cpu = [get_context("spawn").Process(target=cpu_worker, args=(c, args.cpu_duty, cpu_stop),
                                        daemon=True) for c in args.cpus]
    for p in cpu:
        p.start()
    llm = [Thread(target=llm_worker, args=(i, args.words, args.max_tokens, stop, stats, prompts),
                  daemon=True) for i in range(args.jobs)]
    for t in llm:
        t.start()
    started, reason = time.monotonic(), "duration reached"
    try:
        while time.monotonic() - started < args.minutes * 60:
            if not READINESS.exists():
                reason = "energy_control readiness file vanished (abort or stop)"
                break
            time.sleep(1)
    except KeyboardInterrupt:
        reason = "interrupted"
    stop.set()
    shutdown_open_connections()  # Unblock reads waiting for prefill; vLLM aborts them.
    cpu_stop.set()
    for p in cpu:
        p.join(3)
    stopped_at = time.monotonic()
    for t in llm:
        t.join(max(0.0, 5.0 - (time.monotonic() - stopped_at)))
    domains = {name: {"requests": len(v),
                      "ttft_s_mean": round(sum(t for t, _ in v) / len(v), 2),
                      "decode_tok_s_mean": round(sum(r for _, r in v) / len(v), 2)}
               for name, v in sorted(stats.pop("domains").items())}
    summary = {"stopped": reason, "elapsed_s": round(stopped_at - started),
               "workers_joined_s": round(time.monotonic() - stopped_at, 2),
               "workers_alive": sum(t.is_alive() for t in llm), "prompt_set": args.prompt_set,
               **stats, **({"domains": domains} if domains else {})}
    print(json.dumps(summary), flush=True)
    from energy_control.markers import write_marker
    write_marker("sustained-load", "completed" if reason == "duration reached" else "stopped",
                 **summary)


if __name__ == "__main__":
    main()
