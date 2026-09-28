"""LLM throughput versus GPU and fast-core clocks (twin input, no device I/O).

Per generated token the engine spends GPU time (kernels) and CPU time
(scheduling, sampling, Python engine loop). A serial model

    1 / decode_rate = A / gpu_mhz + B / cpu_fast_mhz + D    (s per token)

(D: clock-independent time, e.g. memory) is linear in (A, B, D) and is
fitted by least squares over every trial: the
entry series (CPU clock moved by the policy) and the CPU-impact trials (CPU
cap fixed). Prefill is modelled as rate = k * gpu_mhz. The effective clocks
are trace means over the decode phase (all four requests running, after the
first 60 s). Reads the durable trial logs and 4 Hz trial traces.
"""
import argparse
import glob
import json
from math import sqrt

FAST_CPUS = tuple(range(5, 10)) + tuple(range(15, 20))
SERIES_LOG = "/var/lib/spark-energy/entry-series-w19000-o10000-j4-unique.jsonl"
IMPACT_LOG = "/var/lib/spark-energy/cpu-impact-runs.jsonl"


def trial_rows(paths):
    rows = []
    for path in paths:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except OSError:
            continue
        for line in lines:
            r = json.loads(line)
            if r.get("event") == "result" and (r.get("vllm_delta") or {}).get("decode_s_sum"):
                rows.append(r)
    return rows


def decode_clocks(trace_dir, *, skip_s=60.0):
    """GPU MHz, busiest fast-core MHz and (if traced) aggregate decode tok/s
    while all four requests decode. The busiest fast core stands for vLLM's
    engine thread; the mean over mostly idle cores says nothing about it."""
    rows = [json.loads(line) for path in sorted(glob.glob(f"{trace_dir}/*.jsonl"))
            for line in open(path, encoding="utf-8")]
    if not rows:
        return None
    t0 = rows[0]["mono_ns"]
    busy = [r for r in rows if (r.get("active_jobs") or 0) >= 4
            and (r["mono_ns"] - t0) / 1e9 >= skip_s]
    busy = busy or [r for r in rows if (r.get("active_jobs") or 0) > 0]
    if not busy:
        return None
    gpu = sum(r["gpu_mhz"] for r in busy) / len(busy)
    fast = [max(r["cpu_mhz"][i] for i in FAST_CPUS) for r in busy
            if len(r.get("cpu_mhz") or []) >= 20]
    counted = [r for r in busy if r.get("vllm_gen_tokens") is not None]
    rate = None
    if len(counted) >= 2 and counted[-1]["mono_ns"] > counted[0]["mono_ns"]:
        rate = ((counted[-1]["vllm_gen_tokens"] - counted[0]["vllm_gen_tokens"])
                / ((counted[-1]["mono_ns"] - counted[0]["mono_ns"]) / 1e9) / 4)
    return gpu, (sum(fast) / len(fast) if fast else None), rate


RUNS_DIR = "/var/lib/spark-energy/commissioning-runs"


def request_rates(run_id, *, ttft_s, tokens=10000, runs_dir=RUNS_DIR):
    """Per-request decode tok/s from admission and completion events.

    One of four requests often decodes ~3x slower from the start (vLLM,
    doc/45); per-request rates keep it from distorting clock comparisons.
    """
    try:
        lines = open(f"{runs_dir}/{run_id}/events.jsonl", encoding="utf-8").read().splitlines()
    except OSError:
        return []
    admitted, rates = {}, []
    for line in lines:
        e = json.loads(line)
        if e.get("action") != "admit_workload":
            continue
        if e["kind"] == "intent":
            admitted[e["seq"]] = e["mono_ns"]
        elif e["kind"] == "outcome" and e.get("intent_seq") in admitted:
            decode_s = (e["mono_ns"] - admitted[e["intent_seq"]]) / 1e9 - ttft_s
            if decode_s > 0:
                rates.append(tokens / decode_s)
    return sorted(rates)


def observations(rows, clocks=decode_clocks):
    out = []
    for r in rows:
        v = r["vllm_delta"]
        n = v.get("ttft_count") or 1
        measured = clocks(r["trace"])
        if measured is None or measured[1] is None:
            continue
        gpu, cpu, busy_rate = measured
        # Per-request averages include vLLM tail requests that decode alone at
        # ~13 tok/s for minutes (not clock-related, doc/45); the traced
        # all-four-running rate is preferred when available.
        out.append({"name": r["name"], "gpu_mhz": gpu, "cpu_fast_mhz": cpu,
                    "cap_mhz": r.get("cpu_fast_max_mhz"),
                    "decode_tok_s": busy_rate if busy_rate else (
                        10000 * n / v["decode_s_sum"] if v.get("decode_s_sum") else None),
                    "decode_source": "traced-4-active" if busy_rate else "vllm-per-request",
                    "trace_4_active_tok_s": busy_rate,
                    "prefill_tok_s": v["prompt_tokens"] / v["prefill_s_sum"]
                    if v.get("prefill_s_sum") else None,
                    "ttft_s": v["ttft_s_sum"] / n if v.get("ttft_s_sum") else None,
                    "request_tok_s": request_rates(r["run_id"], ttft_s=v["ttft_s_sum"] / n)
                    if v.get("ttft_s_sum") and not r.get("aborted") else []})
    return out


def _solve(matrix, vector):
    """Gaussian elimination with partial pivoting (small dense systems)."""
    n = len(vector)
    m = [row[:] + [v] for row, v in zip(matrix, vector)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-18:
            raise ValueError("clock variation insufficient to identify the model")
        m[col], m[pivot] = m[pivot], m[col]
        for r in range(col + 1, n):
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    x = [0.0] * n
    for r in reversed(range(n)):
        x[r] = (m[r][n] - sum(m[r][c] * x[c] for c in range(r + 1, n))) / m[r][r]
    return x


def fit_decode(obs, *, intercept=True):
    """Least squares for 1/rate = A/gpu + B/cpu (+ D)."""
    rows = [([1 / o["gpu_mhz"], 1 / o["cpu_fast_mhz"]] + ([1.0] if intercept else []),
             1 / o["decode_tok_s"]) for o in obs if o["decode_tok_s"]]
    k = 3 if intercept else 2
    if len(rows) < k + 1:
        raise ValueError(f"need at least {k + 1} trials with distinct clock ratios")
    normal = [[sum(x[i] * x[j] for x, _ in rows) for j in range(k)] for i in range(k)]
    target = [sum(x[i] * y for x, y in rows) for i in range(k)]
    coef = _solve(normal, target)
    predicted = [1 / sum(c * v for c, v in zip(coef, x)) for x, _ in rows]
    measured = [1 / y for _, y in rows]
    rmse = sqrt(sum((p - m) ** 2 for p, m in zip(predicted, measured)) / len(rows))
    return {"A_s_mhz_per_token": coef[0], "B_s_mhz_per_token": coef[1],
            "D_s_per_token": coef[2] if intercept else 0.0,
            "rmse_tok_s": rmse, "trials": len(rows)}


def fit_prefill(obs):
    pts = [(o["gpu_mhz"], o["prefill_tok_s"]) for o in obs if o["prefill_tok_s"]]
    k = sum(g * p for g, p in pts) / sum(g * g for g, _ in pts)
    rmse = sqrt(sum((k * g - p) ** 2 for g, p in pts) / len(pts))
    return {"tok_s_per_mhz": k, "rmse_tok_s": rmse, "trials": len(pts)}


def cpu_share(decode, gpu_mhz, cpu_mhz):
    """Fraction of per-token time spent on the CPU side at these clocks."""
    t_gpu = decode["A_s_mhz_per_token"] / gpu_mhz
    t_cpu = decode["B_s_mhz_per_token"] / cpu_mhz
    return t_cpu / (t_gpu + t_cpu + decode.get("D_s_per_token", 0.0))


def main(argv=None):
    parser = argparse.ArgumentParser(description="LLM throughput model from trial logs")
    parser.add_argument("--logs", nargs="+", default=[SERIES_LOG, IMPACT_LOG])
    args = parser.parse_args(argv)
    obs = observations(trial_rows(args.logs))
    for o in obs:
        # Healthy decode: mean of the two fastest requests (slow vLLM requests
        # excluded, doc/45).
        if len(o["request_tok_s"]) >= 2:
            o["decode_tok_s"] = sum(o["request_tok_s"][-2:]) / 2
            o["decode_source"] = "fastest-2-requests"
    decode, prefill = fit_decode(obs), fit_prefill(obs)
    report = {"observations": obs, "decode": decode, "prefill": prefill,
              "cpu_share_of_token_time": {
                  f"gpu{g}_cpu{c}": round(cpu_share(decode, g, c), 3)
                  for g in (1200, 1800) for c in (1378, 2600, 3900)}}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
