#!/usr/bin/env python3
"""Standalone, read-only Spark Energy dashboard (GB10 thermal and power).

Serves one self-contained page plus three small JSON endpoints. The only data
source is the status file that energy_control publishes on tmpfs
(``/run/spark-energy/status.json``); this server never touches hardware,
never runs commands and never writes anything (one hardware talker).

Endpoints (GET only, everything else is 404 or 405):

    GET /                        the page (dashboard/index.html)
    GET /api/cooling[?since_ms=] tiered in-memory history of the status file
    GET /api/energy/status       latest status payload + freshness state and age
    GET /healthz                 liveness

Run:
    python3 -m dashboard.server [--host 127.0.0.1] [--port 8790] [--status-file PATH]
    python3 dashboard/server.py  (same options)

Python 3.12 standard library only.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

DEFAULT_STATUS_FILE = Path("/run/spark-energy/status.json")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
PAGE_FILE = Path(__file__).resolve().with_name("index.html")

STATUS_MAX_AGE_S = 5.0          # older publications count as stale
STATUS_MAX_BYTES = 262_144      # the status file is a few kB; refuse anything huge
POLL_SECONDS = 1.0              # sampler cadence (energy_control publishes at ~1 Hz)
ENERGY_ZONES = ("TSOC", "TS0E", "TS0P", "TS1E", "TS1P", "TGPU", "TUNC")

# Graph history, memory only. Two hours are kept: raw 1 s for 3 min, 5 s means to
# 15 min, 30 s means to 2 h (about 180 + 144 + 210 = 534 rows). Coarser steps are
# multiples of finer ones so buckets nest. No tier holds more than 600 buckets.
WINDOW_MS = 2 * 60 * 60 * 1000
TIERS: Tuple[Tuple[int, int], ...] = ((3 * 60 * 1000, 1000), (15 * 60 * 1000, 5000), (WINDOW_MS, 30000))
MAX_BUCKETS_PER_TIER = 600
KEYS = (*ENERGY_ZONES, "gpuT", "nvme", "wifi", "rpm1", "rpm2", "floor",
        "gpuMhz", "gpuCap", "gpuW", "gpuUtil", "pMhz", "eMhz",
        "capSlow", "capFast", "cpuUtil", "cpuW",
        "p0Mhz", "p1Mhz", "e0Mhz", "e1Mhz", "capP0", "capP1", "capE0", "capE1", "vendor")
FIELDS = ("t", *KEYS)

# Fallback limits when the status carries none. Mirror energy_control/limits.py.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from energy_control.limits import ACPI_ABORT_C, CPU_TARGET_QUALIFIED_MAX_C
except Exception:  # standalone copy without the controller package
    ACPI_ABORT_C, CPU_TARGET_QUALIFIED_MAX_C = 96.0, 92.0
try:  # energy-conserving cooler twin, integrated by the sampler (pure, no device I/O)
    from energy_control.cooler_twin import CoolerTwin, twin_from_status
except Exception:  # standalone copy without the controller package: the page falls back
    CoolerTwin = twin_from_status = None
finally:
    sys.path.pop(0)
GPU_ABORT_C, GPU_TARGET_C = 85.0, 75.0

for _max_age, _step in TIERS:  # keep the bucket bound honest if someone edits the tiers
    assert _max_age // _step <= MAX_BUCKETS_PER_TIER, "a history tier exceeds 600 buckets"


# ---------------------------------------------------------------------------
# Status file (the single data source)
# ---------------------------------------------------------------------------
def finite(value: Any, digits: int = 1, low: float = -1e9, high: float = 1e9) -> Optional[float]:
    """Finite number within [low, high], rounded; bools/strings/NaN become None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return int(round(number)) if digits == 0 else round(number, digits)


def read_status(path: Path, now_ns: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], str, Optional[float]]:
    """Return (payload, state, age_s); state is fresh, stale, missing or invalid."""
    try:
        with Path(path).open("rb") as handle:
            raw = handle.read(STATUS_MAX_BYTES + 1)
    except FileNotFoundError:
        return None, "missing", None
    except OSError:
        return None, "invalid", None
    if len(raw) > STATUS_MAX_BYTES:
        return None, "invalid", None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, "invalid", None
    if not isinstance(payload, dict):
        return None, "invalid", None
    utc_ns = payload.get("utc_ns")
    if isinstance(utc_ns, bool) or not isinstance(utc_ns, int):
        return payload, "invalid", None
    age_s = ((time.time_ns() if now_ns is None else now_ns) - utc_ns) / 1e9
    if abs(age_s) > STATUS_MAX_AGE_S:
        return payload, "stale", round(age_s, 1)
    return payload, "fresh", round(age_s, 2)


def section(payload: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    value = payload.get(name) if isinstance(payload, dict) else None
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def compact_history(rows: List[Dict[str, Any]], keys: tuple, tiers: tuple, now_ms: int) -> List[Dict[str, Any]]:
    """Bound a time series: raw rows near now, weighted bucket means further back.

    ``tiers`` is ((max_age_ms, step_ms), ...) ascending; the first tier keeps raw
    samples. The optional row key ``n`` carries the number of merged samples.
    """
    window = tiers[-1][0]
    output: List[Dict[str, Any]] = []
    bucket_key = None
    acc: Dict[str, List[float]] = {}

    def flush() -> None:
        if bucket_key is None:
            return
        weight_t, sum_t = acc.pop("__t")
        merged: Dict[str, Any] = {"t": int(round(sum_t / weight_t)), "n": int(round(weight_t))}
        for key in keys:
            weight, total = acc.get(key, (0.0, 0.0))
            merged[key] = round(total / weight, 1) if weight > 0 else None
        output.append(merged)

    for row in sorted(rows, key=lambda item: item["t"]):
        age = now_ms - row["t"]
        if age > window or age < -5000:
            continue
        step = next((step for max_age, step in tiers if age <= max_age), tiers[-1][1])
        if step <= tiers[0][1]:
            flush()
            bucket_key, acc = None, {}
            output.append(row)
            continue
        key = (step, row["t"] // step)
        if key != bucket_key:
            flush()
            bucket_key, acc = key, {"__t": [0.0, 0.0]}
        weight = float(row.get("n") or 1)
        acc["__t"][0] += weight
        acc["__t"][1] += weight * row["t"]
        for name in keys:
            value = row.get(name)
            if value is not None:
                slot = acc.setdefault(name, [0.0, 0.0])
                slot[0] += weight
                slot[1] += weight * value
    flush()
    return output


def cooling_row(payload: Dict[str, Any], t_ms: int) -> Dict[str, Any]:
    """One history row from an energy_control status payload (bounded, rounded numbers only)."""
    gpu, cpu, fan = section(payload, "gpu"), section(payload, "cpu"), section(payload, "fan")
    zones, board = section(payload, "zones_c"), section(payload, "board_c")
    caps = cpu.get("caps_mhz") if isinstance(cpu.get("caps_mhz"), dict) else {}
    rpm = fan.get("rpm") if isinstance(fan.get("rpm"), list) else []
    row: Dict[str, Any] = {"t": t_ms}
    for name in ENERGY_ZONES:
        row[name] = finite(zones.get(name), 1, -20, 150)
    row.update({
        "gpuT": finite(gpu.get("temp_c"), 1, -20, 150),
        "nvme": finite(board.get("nvme"), 1, -20, 150),
        "wifi": finite(board.get("wifi"), 1, -20, 150),
        "rpm1": finite(rpm[0] if len(rpm) > 0 else None, 0, 0, 30000),
        "rpm2": finite(rpm[1] if len(rpm) > 1 else None, 0, 0, 30000),
        "floor": finite(fan.get("floor"), 0, 0, 64),
        "gpuMhz": finite(gpu.get("measured_mhz"), 0, 0, 10000),
        "gpuCap": finite(gpu.get("cap_mhz"), 0, 0, 10000),
        "gpuW": finite(gpu.get("power_w"), 1, 0, 2000),
        "gpuUtil": finite(gpu.get("util_pct"), 1, 0, 100),
        "pMhz": finite(cpu.get("p_mhz"), 0, 0, 10000),
        "eMhz": finite(cpu.get("e_mhz"), 0, 0, 10000),
        "capSlow": finite(caps.get("slow"), 0, 0, 10000),
        "capFast": finite(caps.get("fast"), 0, 0, 10000),
        "cpuUtil": finite(cpu.get("util_pct"), 1, 0, 100),
        # Estimated by energy_control (no CPU power sensor on GB10).
        "cpuW": finite(cpu.get("est_power_w"), 0, 0, 500),
    })
    # Per-cluster requested vs measured clocks: under load they match 1:1 unless
    # the vendor firmware limits the CPU.
    clocks = section(payload, "clocks")
    cluster_caps = cpu.get("cluster_caps_mhz") if isinstance(cpu.get("cluster_caps_mhz"), dict) else {}
    for name in ("P0", "P1", "E0", "E1"):
        entry = clocks.get(name) if isinstance(clocks.get(name), dict) else {}
        key = name[0].lower() + name[1]
        row[f"{key}Mhz"] = finite(entry.get("measured_mhz"), 0, 0, 10000)
        row[f"cap{name}"] = finite(entry.get("requested_mhz", cluster_caps.get(name)), 0, 0, 10000)
    row["vendor"] = sum(1 for value in clocks.values()
                        if isinstance(value, dict) and value.get("vendor_throttle") is True)
    return row


class History:
    """Tiered cooling history of the status file, shared by every browser (memory only)."""

    def __init__(self, status_file: Path = DEFAULT_STATUS_FILE):
        self.status_file = Path(status_file)
        self._lock = threading.Lock()
        self._rows: List[Dict[str, Any]] = []
        self._latest: Dict[str, Any] = {"state": "missing", "age_s": None, "payload": None}
        self._last_utc_ns: Optional[int] = None
        self._last_t_ms = 0
        self._appended = 0
        self._twin = CoolerTwin() if CoolerTwin is not None else None
        self._twin_state: Optional[Dict[str, Any]] = None

    def poll_once(self, now_ms: Optional[int] = None) -> bool:
        """Read the status file once; append a row for each new publication. True if appended."""
        payload, state, age_s = read_status(self.status_file)
        with self._lock:
            self._latest = {"state": state, "age_s": age_s, "payload": payload if isinstance(payload, dict) else None}
        if state != "fresh" or payload is None:
            return False
        utc_ns = payload["utc_ns"]
        t_ms = utc_ns // 1_000_000
        if utc_ns == self._last_utc_ns or t_ms - self._last_t_ms < 500:
            return False
        self._last_utc_ns, self._last_t_ms = utc_ns, t_ms
        row = cooling_row(payload, t_ms)
        twin_state = twin_from_status(self._twin, payload) if self._twin is not None else None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        with self._lock:
            self._twin_state = twin_state
            self._rows.append(row)
            self._appended += 1
            if self._appended % 10 == 0 or self._rows[0]["t"] < now_ms - WINDOW_MS:
                self._rows[:] = compact_history(self._rows, KEYS, TIERS, now_ms)
        return True

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # never let the sampler die on odd input
                print(f"sampler: {type(exc).__name__}: {exc}", file=sys.stderr)
            stop.wait(POLL_SECONDS)

    def twin(self) -> Optional[Dict[str, Any]]:
        """Latest energy-conserving cooler twin state (None until the first sample)."""
        with self._lock:
            return dict(self._twin_state) if self._twin_state else None

    def snapshot(self, since_ms: Optional[int] = None) -> Dict[str, Any]:
        """Same shape as the Spark dashboard's /api/cooling response."""
        now_ms = int(time.time() * 1000)
        with self._lock:
            rows = [row for row in self._rows if since_ms is None or row["t"] > since_ms]
            latest = dict(self._latest)
        payload = latest.get("payload") or {}
        limits = section(payload, "limits")
        mode = payload.get("mode") if isinstance(payload.get("mode"), str) else None
        text = lambda key: payload.get(key) if isinstance(payload.get(key), str) else None
        return {
            "as_of_ms": now_ms,
            "window_ms": WINDOW_MS,
            "tiers": [list(tier) for tier in TIERS],
            "incremental": since_ms is not None,
            "fields": list(FIELDS),
            "rows": [[row.get(field) for field in FIELDS] for row in rows],
            "status": {
                "available": latest.get("state") == "fresh",
                "state": latest.get("state"),
                "age_s": latest.get("age_s"),
                "source": "energy_control status.json",
                "mode": mode,
                # energy_control keeps publishing while it starts or holds its safe state.
                "controller": ("safe_state" if mode == "SAFE_STATE" else "starting" if mode == "STARTING" else "running"),
                "reason": text("reason"),
                "run_id": text("run_id"),
            },
            "limits": {
                "acpi_abort_c": finite(limits.get("acpi_abort_c"), 1) or ACPI_ABORT_C,
                "gpu_abort_c": finite(limits.get("gpu_abort_c"), 1) or GPU_ABORT_C,
                "cpu_target_c": finite(limits.get("cpu_target_c"), 1) or CPU_TARGET_QUALIFIED_MAX_C,
                "gpu_target_c": finite(limits.get("gpu_target_c"), 1) or GPU_TARGET_C,
                "gpu_entry_mhz": finite(limits.get("gpu_entry_mhz"), 0),
                "gpu_max_mhz": finite(limits.get("gpu_max_mhz"), 0),
                "cpu_fast_max_mhz": 3900,
                "cpu_slow_max_mhz": 2808,
            },
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def page_csp(html: str) -> str:
    """Restrictive CSP for the self-contained page: its inline scripts by hash, nothing external."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode("ascii") + "'"
        for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    # style-src needs 'unsafe-inline': the SVG views colour their elements with style attributes.
    return ("default-src 'none'; " + (f"script-src {hashes}; " if hashes else "")
            + "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


JSON_CSP = "default-src 'none'; frame-ancestors 'none'"


class Handler(BaseHTTPRequestHandler):
    """GET-only handler; routes are fixed, no client-supplied paths reach the filesystem."""

    server_version = "spark-energy-dashboard"
    sys_version = ""
    history: History = None  # set by make_server
    status_file: Path = DEFAULT_STATUS_FILE
    page: bytes = b""
    csp: str = JSON_CSP

    def _send(self, code: int, body: bytes, ctype: str, csp: str = JSON_CSP, extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":  # a HEAD reply carries no body
            self.wfile.write(body)

    def _json(self, code: int, obj: Any, extra: Optional[Dict[str, str]] = None) -> None:
        self._send(code, json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8"),
                   "application/json", JSON_CSP, extra)

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        url = urlsplit(self.path)
        if url.path == "/":
            self._send(200, self.page, "text/html; charset=utf-8", self.csp)
        elif url.path == "/api/cooling":
            query = parse_qs(url.query, keep_blank_values=True)
            if set(query) - {"since_ms"}:
                return self._json(400, {"error": "unknown_parameter"})
            since_ms = None
            if "since_ms" in query:
                raw = query["since_ms"][-1]
                if not re.fullmatch(r"-?\d{1,16}", raw):
                    return self._json(400, {"error": "bad_since_ms"})
                since_ms = int(raw)
            self._json(200, self.history.snapshot(since_ms))
        elif url.path == "/api/energy/status":
            payload, state, age_s = read_status(self.status_file)
            self._json(200, {"state": state, "age_s": age_s, "status": payload if isinstance(payload, dict) else None,
                             "twin": self.history.twin() if state == "fresh" else None})
        elif url.path == "/healthz":
            self._json(200, {"ok": True, "name": "spark-energy-dashboard"})
        else:
            self._json(404, {"error": "not_found"})

    def _refuse(self) -> None:
        self._json(405, {"error": "read_only"}, {"Allow": "GET"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _refuse

    def log_message(self, *args: Any) -> None:  # quiet: no request logs
        pass


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, status_file: Path = DEFAULT_STATUS_FILE,
                history: Optional[History] = None) -> ThreadingHTTPServer:
    page = PAGE_FILE.read_text(encoding="utf-8")
    handler = type("DashboardHandler", (Handler,), {
        "history": history or History(status_file),
        "status_file": Path(status_file),
        "page": page.encode("utf-8"),
        "csp": page_csp(page),
    })
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Spark Energy dashboard (GB10 thermal and power).")
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: loopback)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE,
                        help="energy_control status file (default: /run/spark-energy/status.json)")
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port, args.status_file)
    stop = threading.Event()
    sampler = threading.Thread(target=server.RequestHandlerClass.history.run, args=(stop,),
                               name="status-sampler", daemon=True)
    sampler.start()
    print(f"spark-energy-dashboard on http://{args.host}:{args.port}/ (status file {args.status_file})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
