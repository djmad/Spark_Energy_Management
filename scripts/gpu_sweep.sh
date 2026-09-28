#!/bin/bash
# GPU matrix-multiplication frequency sweep (operator, 27 September 2026:
# "vermesse die Matrixmultiplikation mit verschiedenen Frequenzen, startend
# bei 1500 steigend"). Identification data for the twin up to ~70 W.
#
# For each step the GPU maximum (and, below 1700 MHz, the entry ceiling) is
# written to /etc/spark-energy/config.json and energy_control is restarted;
# then the operator's unchanged gpu_burnin.py runs alone (CPU idle) for STEP_S
# through burnin_block.sh. The sweep stops at the first step that does not
# complete (guard/policy abort). The original configuration is restored and
# the service restarted at the end, also after an abort.
# Run as root by the root main agent under the hardware claim, vLLM stopped.
set -u
STEP_S=${1:-120}
shift || true
STEPS=${*:-1500 1600 1700 1800 1900 2000 2100 2200}
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
CONFIG=/etc/spark-energy/config.json
BACKUP=$CONFIG.bak-sweep-$(date +%Y%m%dT%H%M%S)
READY=/run/spark-energy/entry-ceiling
cp -p "$CONFIG" "$BACKUP"
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$DIR/gpu-sweep-events.jsonl"; sync; }
set_limits() {  # max entry
    python3 - "$CONFIG" "$1" "$2" <<'EOF'
import json, os, sys
path, maximum, entry = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
config = json.load(open(path))
config["gpu_max_mhz"], config["gpu_entry_mhz"] = maximum, entry
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(config, handle, indent=2, sort_keys=True); handle.write("\n")
    handle.flush(); os.fsync(handle.fileno())
os.chmod(tmp, 0o644); os.replace(tmp, path)
sys.path.insert(0, "/opt/spark-energy")
from energy_control.service import load_config
loaded = load_config()
assert (loaded.gpu_max_mhz, loaded.gpu_entry_mhz) == (maximum, entry)
EOF
}
restart_ready() {
    systemctl restart energy_control
    for _ in $(seq 1 120); do [ -e "$READY" ] && return 0; sleep 1; done
    return 1
}
event "\"event\": \"sweep_start\", \"steps\": \"$STEPS\", \"step_s\": $STEP_S, \"backup\": \"$BACKUP\""
for mhz in $STEPS; do
    entry=$(( mhz < 1700 ? mhz : 1700 ))
    set_limits "$mhz" "$entry" || { event "\"event\": \"config_failed\", \"mhz\": $mhz"; break; }
    restart_ready || { event "\"event\": \"not_ready\", \"mhz\": $mhz"; break; }
    sleep 10   # idle baseline in the trace before the load
    event "\"event\": \"step_start\", \"mhz\": $mhz, \"entry\": $entry"
    bash scripts/burnin_block.sh "$STEP_S" "sweep-$mhz" gpu
    reason=$(tail -1 "$DIR/sweep-$mhz-events.jsonl" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("reason",""))')
    event "\"event\": \"step_end\", \"mhz\": $mhz, \"reason\": \"$reason\""
    [ "$reason" = completed ] || break
    sleep 20   # cool-down between steps (fan 12)
done
cp -p "$BACKUP" "$CONFIG"
restart_ready
event "\"event\": \"sweep_end\", \"restored\": \"$BACKUP\""
date -Iseconds > "$DIR/gpu-sweep.done"
