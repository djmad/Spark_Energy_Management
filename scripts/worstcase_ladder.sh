#!/bin/bash
# Worst-case ladder (operator, 27 September 2026: "lass uns mit dem Worst Case
# + CPU beginnen, 1700 als Start aufwärts, jeweils mit dem servicegeregelten
# Ramp-up"). Per step the GPU maximum is set through the boot-bound live
# override (no service restart); the entry ceiling stays 1700 MHz, so each
# step starts from idle at the entry ceiling and ramps under energy_control.
# The operator's unchanged burn-in scripts start at the same moment
# (burnin_block.sh, both). The ladder stops at the first step that does not
# complete; the override is removed at the end (production settings again).
# Run as root by the root main agent under the hardware claim, vLLM stopped.
set -u
STEP_S=${1:-300}
shift || true
STEPS=${*:-1700 1800 1900 2000 2100 2200}
MODE=${MODE:-both}   # both (GPU + CPU burn-in) | gpu (GPU burn-in alone)
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
OVERRIDE=/run/spark-energy/qualification.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$DIR/worstcase-ladder-events.jsonl"; sync; }
set_max() {
    python3 - "$OVERRIDE" "$BOOT" "$1" <<'EOF'
import json, os, sys
path, boot, mhz = sys.argv[1], sys.argv[2], int(sys.argv[3])
with open(path + ".tmp", "w") as handle:
    json.dump({"boot_id": boot, "gpu_max_mhz": mhz}, handle)
os.chmod(path + ".tmp", 0o644)
os.replace(path + ".tmp", path)
EOF
    for _ in $(seq 1 20); do   # applied live within ~1 s
        python3 -c "import json,sys; d=json.load(open('/run/spark-energy/status.json')); \
sys.exit(0 if d.get('limits',{}).get('gpu_max_mhz')==$1 else 1)" 2>/dev/null && return 0
        sleep 0.5
    done
    return 1
}
ready() {  # controlling (readiness file, not SAFE_STATE/STARTING), up to 180 s
    for _ in $(seq 1 360); do
        if [ -e /run/spark-energy/entry-ceiling ] && python3 -c "import json,sys; \
d=json.load(open('/run/spark-energy/status.json')); \
sys.exit(0 if d.get('mode') not in ('SAFE_STATE','STARTING') else 1)" 2>/dev/null; then
            return 0
        fi
        sleep 0.5
    done
    return 1
}
event "\"event\": \"ladder_start\", \"steps\": \"$STEPS\", \"step_s\": $STEP_S"
for mhz in $STEPS; do
    ready || { event "\"event\": \"controller_not_ready\", \"mhz\": $mhz"; break; }
    set_max "$mhz" || { event "\"event\": \"override_not_applied\", \"mhz\": $mhz"; break; }
    event "\"event\": \"step_start\", \"mhz\": $mhz"
    bash scripts/burnin_block.sh "$STEP_S" "${LABEL:-worst}-$mhz" "$MODE"
    reason=$(tail -1 "$DIR/${LABEL:-worst}-$mhz-events.jsonl" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("reason",""))
except Exception: print("no events (block refused)")')
    event "\"event\": \"step_end\", \"mhz\": $mhz, \"reason\": \"$reason\""
    [ "$reason" = completed ] || break
    sleep 45   # back to idle at the entry ceiling, fan 12
done
rm -f "$OVERRIDE"
event "\"event\": \"ladder_end\", \"override\": \"removed\""
date -Iseconds > "$DIR/worstcase-ladder.done"
