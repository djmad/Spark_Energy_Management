#!/bin/bash
# Calorimetric CPU power calibration (operator, 27 September 2026: "wir müssen
# die Leistung auf Grund des digitalen Zwillings errechnen … alternativ auf
# der echten Maschine auf Grund der erkannten Wärmeentwicklung, wir haben ja
# die Grafikkarte zum Feststellen von Referenz-Temperaturerzeugung"; "schnelle
# und langsame Cores gesondert vermessen").
# Blocks (fan 12, vLLM stopped), each followed by an idle gap:
#   idle baseline;
#   GPU reference heater: the operator's unchanged gpu_burnin.py alone at fixed
#     clocks (live override: entry = maximum = f), CPU idle;
#   CPU: the operator's unchanged cpu_burnin.py, pinned from outside with
#     taskset: P cores only, E cores only, all 20; then P cores only with the
#     P cluster maxima lowered (clock exponent). GPU idle.
# energy_control's 1 Hz trace records temperatures, GPU power, clocks and
# utilisation; analysis/calibrate_power.py fits the copper and the CPU model.
# Every block stops at once when energy_control's readiness vanishes; an
# abort ends the calibration. The override is removed at the end.
set -u
STEP_S=${STEP_S:-300}
GAP_S=${GAP_S:-60}
# GPU reference levels (MHz). The fin block behind the plate's narrow neck
# settles with tau ~130-240 s (thermal-step-1500, doc/55), so long steps
# and gaps matter more than many levels.
GPU_LEVELS=${GPU_LEVELS:-1500 1700 1900}
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
READY=/run/spark-energy/entry-ceiling
OVERRIDE=/run/spark-energy/qualification.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
CPU_SCRIPT=${CPU_SCRIPT:?set CPU_SCRIPT to the CPU burn-in script}
LOG=$DIR/calibration-events.jsonl
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$LOG"; sync; }
override() {  # JSON object body without boot_id, e.g. '"gpu_max_mhz": 1500'
    python3 - "$OVERRIDE" "$BOOT" "$1" <<'EOF'
import json, os, sys
path, boot, body = sys.argv[1], sys.argv[2], sys.argv[3]
values = {"boot_id": boot, **(json.loads("{" + body + "}") if body else {})}
if len(values) == 1:
    if os.path.exists(path):
        os.remove(path)
    sys.exit(0)
with open(path + ".tmp", "w") as handle:
    json.dump(values, handle)
os.chmod(path + ".tmp", 0o644)
os.replace(path + ".tmp", path)
EOF
    sleep 3   # applied live within ~1 s
}
idle() {  # label seconds
    event "\"event\": \"block\", \"kind\": \"idle\", \"label\": \"$1\", \"phase\": \"start\""
    for _ in $(seq 1 "$2"); do [ -e "$READY" ] || return 1; sleep 1; done
    event "\"event\": \"block\", \"kind\": \"idle\", \"label\": \"$1\", \"phase\": \"end\""
}
gpu_block() {  # MHz
    override "\"gpu_max_mhz\": $1, \"gpu_entry_mhz\": $1"
    event "\"event\": \"block\", \"kind\": \"gpu\", \"label\": \"gpu-$1\", \"phase\": \"start\""
    bash scripts/burnin_block.sh "$STEP_S" "calib-gpu-$1" gpu
    reason=$(tail -1 "$DIR/calib-gpu-$1-events.jsonl" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("reason",""))
except Exception: print("no events")')
    event "\"event\": \"block\", \"kind\": \"gpu\", \"label\": \"gpu-$1\", \"phase\": \"end\", \"reason\": \"$reason\""
    [ "$reason" = completed ]
}
cpu_block() {  # label cpuset (empty = all)
    event "\"event\": \"block\", \"kind\": \"cpu\", \"label\": \"$1\", \"cpus\": \"$2\", \"phase\": \"start\""
    if [ -n "$2" ]; then
        taskset -c "$2" python3 -u "$CPU_SCRIPT" > "$DIR/calib-$1.log" 2>&1 &
    else
        python3 -u "$CPU_SCRIPT" > "$DIR/calib-$1.log" 2>&1 &
    fi
    local pid=$! reason=completed started
    started=$(date +%s)
    while kill -0 "$pid" 2>/dev/null; do
        [ -e "$READY" ] || { reason="energy_control readiness vanished"; break; }
        [ $(( $(date +%s) - started )) -ge "$STEP_S" ] && break
        sleep 1
    done
    kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    event "\"event\": \"block\", \"kind\": \"cpu\", \"label\": \"$1\", \"phase\": \"end\", \"reason\": \"$reason\""
    [ "$reason" = completed ]
}
event "\"event\": \"calibration_start\", \"step_s\": $STEP_S, \"gap_s\": $GAP_S"
ok=1
idle baseline 180 || ok=0
for mhz in $GPU_LEVELS; do
    [ $ok = 1 ] || break
    gpu_block "$mhz" || { ok=0; break; }
    override ""
    idle "after-gpu-$mhz" "$GAP_S" || ok=0
done
if [ $ok = 1 ]; then
    for spec in "p-cores:5-9,15-19" "e-cores:0-4,10-14" "all-cores:"; do
        cpu_block "${spec%%:*}" "${spec#*:}" || { ok=0; break; }
        idle "after-${spec%%:*}" "$GAP_S" || { ok=0; break; }
    done
fi
if [ $ok = 1 ]; then   # clock exponent: P cores with lowered P cluster maxima
    override '"cpu_p0_max_mhz": 2600, "cpu_p1_max_mhz": 2600'
    cpu_block "p-cores-2600" "5-9,15-19" || ok=0
    override ""
    [ $ok = 1 ] && idle "after-p-2600" "$GAP_S"
fi
override ""
event "\"event\": \"calibration_end\", \"ok\": $ok"
date -Iseconds > "$DIR/calibration.done"
