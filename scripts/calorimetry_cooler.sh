#!/bin/bash
# Calorimetric re-measurement of the cooler (operator, 1 October 2026: "vermesse
# die box kalorisch neu, der Kühlkörper wurde getauscht und mit neuen
# Thermopads versehen"; "wir können das vllm dafür abdrehen"; "ich vermute die
# Kupfermenge stimmt nicht"; GPU limit 2.5 GHz, burn-in tested there).
#
# The GPU is the reference heater (measured W). Blocks, each followed by a
# cool-down at the same fixed fan floor:
#   idle baseline at fan 12;
#   GPU burn-in alone at 1500, 2000 and 2500 MHz, fan 12 (stores, neck, hotspot);
#   GPU burn-in at 1500 MHz with the fan fixed at 2 and 6 (fan dependence);
#   CPU burn-in on P cores, E cores, all cores, and P cores at 2600 MHz, fan 12
#     (CPU power scale against the GPU-calibrated cooler).
# Fixed fan: live override fan_policy "load" with fan_min_state =
# fan_load_state. GPU: live override gpu_max_mhz = target; the entry ceiling
# stays at min(1700, target), so every block ramps up from the entry ceiling
# at 100 MHz/s (no cold jump to full clock). energy_control's 1 Hz trace
# records everything; analysis/sink_fit.py fits it.
# Every block stops at once when energy_control's readiness vanishes (guard or
# policy abort) or when an LLM server appears on 127.0.0.1:8000; an abort ends
# the calibration. The override is removed at the end.
# Run as root by the root main agent under the hardware claim, vLLM stopped.
set -u
HEAT_S=${HEAT_S:-420}       # GPU heating at fan 12
COOL_S=${COOL_S:-360}       # cool-down after a GPU block at fan 12
LOWFAN_HEAT_S=${LOWFAN_HEAT_S:-540}
LOWFAN_COOL_S=${LOWFAN_COOL_S:-480}
CPU_S=${CPU_S:-300}
CPU_GAP_S=${CPU_GAP_S:-180}
LABEL=${LABEL:-calorimetry-$(date +%Y%m%d)}
TORCH_PY=${TORCH_PY:-python3}
# Optional burn-in options, e.g. "--gb 40": a smaller memory footprint, same
# 16384^2 bf16 matmul. 1 October 2026: the default 100 GB exhausted memory beside
# the desktop and other services, the sampler stalled and the guard aborted.
GPU_ARGS=${GPU_ARGS:-}
# Steps: baseline | fF:MHZ (GPU at a fixed fan floor) | cpu. A partial list resumes a run.
STEPS=${STEPS:-baseline f12:1500 f12:2000 f12:2500 f6:1500 f2:1500 cpu}
# Before each GPU block, wait (idle, fan held) until this much memory is
# available: the burn-in's footprint plus the guard's 12 GiB minimum plus margin.
NEED_GIB=${NEED_GIB:-40}
MEM_WAIT_S=${MEM_WAIT_S:-14400}
GPU_SCRIPT=${GPU_SCRIPT:?set GPU_SCRIPT to the GPU burn-in script}
CPU_SCRIPT=${CPU_SCRIPT:?set CPU_SCRIPT to the CPU burn-in script}
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
READY=/run/spark-energy/entry-ceiling
OVERRIDE=/run/spark-energy/qualification.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
LOG=$DIR/$LABEL-events.jsonl
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$LOG"; sync; }
override() {  # JSON object body without boot_id; empty removes the override
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
fan() { echo "\"fan_policy\": \"load\", \"fan_min_state\": $1, \"fan_load_state\": $1"; }
llm_up() { ss -ltnH 'sport = :8000' 2>/dev/null | grep -q '127.0.0.1:8000'; }
hold() {  # label seconds: wait, stop on abort
    for _ in $(seq 1 "$2"); do
        [ -e "$READY" ] || { event "\"event\": \"abort\", \"label\": \"$1\", \"reason\": \"readiness vanished\""; return 1; }
        sleep 1
    done
}
idle() {  # label seconds fan
    override "$(fan "$3")"
    event "\"event\": \"block\", \"kind\": \"idle\", \"label\": \"$1\", \"fan\": $3, \"phase\": \"start\""
    hold "$1" "$2" || return 1
    event "\"event\": \"block\", \"kind\": \"idle\", \"label\": \"$1\", \"fan\": $3, \"phase\": \"end\""
}
avail_gib() { awk '/MemAvailable/ {print int($2 / 1048576)}' /proc/meminfo; }
wait_memory() {  # label fan: hold idle until NEED_GIB are available
    local waited=0
    [ "$(avail_gib)" -ge "$NEED_GIB" ] && return 0
    event "\"event\": \"memory_wait\", \"label\": \"$1\", \"available_gib\": $(avail_gib), \"need_gib\": $NEED_GIB, \"phase\": \"start\""
    while [ "$(avail_gib)" -lt "$NEED_GIB" ]; do
        [ -e "$READY" ] || { event "\"event\": \"abort\", \"label\": \"$1\", \"reason\": \"readiness vanished\""; return 1; }
        [ $waited -ge "$MEM_WAIT_S" ] && { event "\"event\": \"abort\", \"label\": \"$1\", \"reason\": \"memory wait timed out\""; return 1; }
        sleep 10; waited=$((waited + 10))
    done
    event "\"event\": \"memory_wait\", \"label\": \"$1\", \"available_gib\": $(avail_gib), \"phase\": \"end\""
    idle "settle-gpu-$1" 120 "$2"
}
gpu_block() {  # MHz fan seconds
    local mhz=$1 fanlvl=$2 secs=$3 entry=$(( $1 < 1700 ? $1 : 1700 )) label="gpu-$1-f$2"
    llm_up && { event "\"event\": \"abort\", \"label\": \"$label\", \"reason\": \"LLM server on :8000\""; return 1; }
    override "$(fan "$fanlvl"), \"gpu_max_mhz\": $mhz, \"gpu_entry_mhz\": $entry"
    event "\"event\": \"block\", \"kind\": \"gpu\", \"label\": \"$label\", \"mhz\": $mhz, \"fan\": $fanlvl, \"phase\": \"start\""
    # shellcheck disable=SC2086
    "$TORCH_PY" -u "$GPU_SCRIPT" $GPU_ARGS > "$DIR/$LABEL-$label.log" 2>&1 &
    local pid=$! reason=completed started
    started=$(date +%s)
    while :; do
        [ -e "$READY" ] || { reason="readiness vanished"; break; }
        llm_up && { reason="LLM server on :8000"; break; }
        kill -0 "$pid" 2>/dev/null || { reason="GPU burn-in exited on its own"; break; }
        [ $(( $(date +%s) - started )) -ge "$secs" ] && break
        sleep 1
    done
    kill -INT "$pid" 2>/dev/null; sleep 5; kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    event "\"event\": \"block\", \"kind\": \"gpu\", \"label\": \"$label\", \"mhz\": $mhz, \"fan\": $fanlvl, \"phase\": \"end\", \"reason\": \"$reason\""
    [ "$reason" = completed ]
}
cpu_block() {  # label cpuset (empty = all) [extra override]
    override "$(fan 12)${3:+, $3}"
    event "\"event\": \"block\", \"kind\": \"cpu\", \"label\": \"$1\", \"cpus\": \"$2\", \"fan\": 12, \"phase\": \"start\""
    if [ -n "$2" ]; then
        taskset -c "$2" python3 -u "$CPU_SCRIPT" > "$DIR/$LABEL-$1.log" 2>&1 &
    else
        python3 -u "$CPU_SCRIPT" > "$DIR/$LABEL-$1.log" 2>&1 &
    fi
    local pid=$! reason=completed started
    started=$(date +%s)
    while kill -0 "$pid" 2>/dev/null; do
        [ -e "$READY" ] || { reason="readiness vanished"; break; }
        [ $(( $(date +%s) - started )) -ge "$CPU_S" ] && break
        sleep 1
    done
    kill -TERM "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    event "\"event\": \"block\", \"kind\": \"cpu\", \"label\": \"$1\", \"cpus\": \"$2\", \"fan\": 12, \"phase\": \"end\", \"reason\": \"$reason\""
    [ "$reason" = completed ]
}

[ -e "$READY" ] || { echo "energy_control not running"; exit 2; }
llm_up && { echo "an LLM server serves on 127.0.0.1:8000; stop it first"; exit 2; }
event "\"event\": \"calorimetry_start\", \"boot_id\": \"$BOOT\", \"steps\": \"$STEPS\", \"heat_s\": $HEAT_S, \"cool_s\": $COOL_S, \"lowfan_heat_s\": $LOWFAN_HEAT_S, \"cpu_s\": $CPU_S, \"gpu_args\": \"$GPU_ARGS\", \"gpu_sha256\": \"$(sha256sum "$GPU_SCRIPT" | cut -c1-64)\", \"cpu_sha256\": \"$(sha256sum "$CPU_SCRIPT" | cut -c1-64)\""
ok=1
for step in $STEPS; do
    [ $ok = 1 ] || break
    case "$step" in
        baseline) idle baseline 300 12 || ok=0 ;;
        f*:*)     # fF:MHZ  GPU step at a fixed fan floor, with a settle before it
            f=${step%%:*}; f=${f#f}; mhz=${step#*:}
            heat=$HEAT_S; cool=$COOL_S
            [ "$f" -lt 12 ] && { heat=$LOWFAN_HEAT_S; cool=$LOWFAN_COOL_S; }
            idle "settle-gpu-$mhz-f$f" 120 "$f" || { ok=0; break; }
            wait_memory "gpu-$mhz-f$f" "$f" || { ok=0; break; }
            gpu_block "$mhz" "$f" "$heat" || { ok=0; break; }
            idle "after-gpu-$mhz-f$f" "$cool" "$f" || ok=0 ;;
        cpu)
            idle settle-cpu 120 12 || { ok=0; break; }
            for spec in "p-cores:5-9,15-19" "e-cores:0-4,10-14" "all-cores:"; do
                [ $ok = 1 ] || break
                cpu_block "${spec%%:*}" "${spec#*:}" || { ok=0; break; }
                idle "after-${spec%%:*}" "$CPU_GAP_S" 12 || ok=0
            done
            if [ $ok = 1 ]; then   # clock exponent: P cores with lowered P cluster maxima
                cpu_block "p-cores-2600" "5-9,15-19" '"cpu_p0_max_mhz": 2600, "cpu_p1_max_mhz": 2600' || ok=0
                [ $ok = 1 ] && { idle "after-p-cores-2600" "$CPU_GAP_S" 12 || ok=0; }
            fi ;;
        *) event "\"event\": \"abort\", \"reason\": \"unknown step $step\""; ok=0 ;;
    esac
done
override ""
event "\"event\": \"calorimetry_end\", \"ok\": $ok"
date -Iseconds > "$DIR/$LABEL.done"
