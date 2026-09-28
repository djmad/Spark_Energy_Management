#!/bin/bash
# Worst-case burn-in block (operator, 27 September 2026: "gpuburnin + cpu,
# beide starten gleichzeitig ohne versetzen"; "verwende ausschließlich den
# burnin wie er ist, keine Änderung der Parameter"; a crash is accepted).
#
# Starts the operator's unchanged scripts at the same moment, with their
# defaults (no arguments):
#   $GPU_SCRIPT   GPU: ~100 GB bf16 16384^2 matmul, endless (operator's gpu_burnin.py)
#   $CPU_SCRIPT   CPU: 20 integer workers, endless (operator's cpu_burnin.py)
# Only start and stop are controlled from outside: SIGINT (the script's
# Ctrl+C) and SIGTERM after DURATION_S, or at once when energy_control's
# readiness file vanishes (guard or policy abort). Nothing restarts.
# A black box appends energy_control's status once per second with fsync,
# so the last state before a possible power loss survives.
# Run as root by the root main agent under the hardware claim, vLLM stopped.
set -u
DURATION_S=${1:?duration in seconds}
LABEL=${2:?label}
MODE=${3:-both}   # both | gpu  (gpu: the GPU script alone, for identification sweeps)
DIR=/var/lib/spark-energy
READY=/run/spark-energy/entry-ceiling
TORCH_PY=${TORCH_PY:-python3}          # a Python with PyTorch/CUDA for the GPU script
GPU_SCRIPT=${GPU_SCRIPT:?set GPU_SCRIPT to the GPU burn-in script}
CPU_SCRIPT=${CPU_SCRIPT:?set CPU_SCRIPT to the CPU burn-in script}
BOX=$DIR/$LABEL-blackbox.jsonl

[ -e "$READY" ] || { echo "energy_control not running"; exit 2; }
if (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; then
    echo "vLLM serves on :8000; the burn-in never runs alongside the LLM"; exit 2
fi
event() {
    python3 - "$DIR/$LABEL-events.jsonl" "$@" <<'EOF'
import json, os, sys, time
path, event, *rest = sys.argv[1:]
record = {"utc": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mono_s": round(time.monotonic(), 3),
          "event": event, **dict(item.split("=", 1) for item in rest)}
with open(path, "a") as handle:
    handle.write(json.dumps(record) + "\n"); handle.flush(); os.fsync(handle.fileno())
EOF
}
event start duration_s="$DURATION_S" \
    gpu_sha256="$(sha256sum "$GPU_SCRIPT" | cut -c1-64)" \
    cpu_sha256="$(sha256sum "$CPU_SCRIPT" | cut -c1-64)" \
    mem_available_kib="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"

# Both at the same moment, unchanged, default parameters.
"$TORCH_PY" -u "$GPU_SCRIPT" > "$DIR/$LABEL-gpu.log" 2>&1 &
GPU=$!
CPU=
if [ "$MODE" = both ]; then
    python3 -u "$CPU_SCRIPT" > "$DIR/$LABEL-cpu.log" 2>&1 &
    CPU=$!
fi
event launched mode="$MODE" gpu_pid="$GPU" cpu_pid="${CPU:-none}"

stop_all() {
    kill -INT "$GPU" 2>/dev/null
    [ -n "$CPU" ] && kill -TERM "$CPU" 2>/dev/null
}
reason=completed
started=$(date +%s)
while :; do
    # Black box: one status line per second, fsynced.
    python3 - "$BOX" <<'EOF'
import json, os, sys, time
try:
    status = json.load(open("/run/spark-energy/status.json"))
    status = status.get("status", status)
except Exception as exc:
    status = {"error": type(exc).__name__}
row = {"utc": time.strftime("%H:%M:%S"), "mono_s": round(time.monotonic(), 2),
       "gpu": status.get("gpu"), "cpu_caps": (status.get("cpu") or {}).get("cluster_caps_mhz"),
       "cpu_p_mhz": (status.get("cpu") or {}).get("p_mhz"), "fan": status.get("fan"),
       "zones": status.get("zones_c"), "mode": status.get("mode")}
with open(sys.argv[1], "a") as handle:
    handle.write(json.dumps(row) + "\n"); handle.flush(); os.fsync(handle.fileno())
EOF
    if [ ! -e "$READY" ]; then reason="energy_control readiness vanished (abort or stop)"; break; fi
    if ! kill -0 "$GPU" 2>/dev/null; then reason="GPU burn-in exited on its own"; break; fi
    if [ -n "$CPU" ] && ! kill -0 "$CPU" 2>/dev/null; then reason="CPU burn exited on its own"; break; fi
    [ $(( $(date +%s) - started )) -ge "$DURATION_S" ] && break
    sleep 1
done
stop_all
wait "$GPU"; gpu_rc=$?
cpu_rc=none
[ -n "$CPU" ] && { wait "$CPU"; cpu_rc=$?; }
event stop reason="$reason" elapsed_s="$(( $(date +%s) - started ))" gpu_rc="$gpu_rc" cpu_rc="$cpu_rc"
date -Iseconds > "$DIR/$LABEL.done"
