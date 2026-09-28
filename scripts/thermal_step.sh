#!/bin/bash
# Thermal step identification (operator, 27 September 2026: "check if the
# weight seems correct based on our measurements"; "du kannst auch Messungen
# mit der Hardware durchführen, wenn das hilft").
# From idle equilibrium: the operator's unchanged gpu_burnin.py alone at a fixed
# GPU clock (live override: entry = maximum = MHZ) for HEAT_S, then idle for
# COOL_S. Fan and CPU stay as energy_control holds them (fan 12, CPU idle).
# The 1 Hz trace records the step response; analysis fits the heat stores.
# The block stops at once when energy_control's readiness vanishes. The
# override is removed at the end.
set -u
MHZ=${MHZ:-1500}
HEAT_S=${HEAT_S:-900}
COOL_S=${COOL_S:-900}
LABEL=${LABEL:-thermal-step-$MHZ}
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
READY=/run/spark-energy/entry-ceiling
OVERRIDE=/run/spark-energy/qualification.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
LOG=$DIR/$LABEL-steps.jsonl
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$LOG"; sync; }
set_override() {  # JSON body without boot_id; empty removes the override
    python3 - "$OVERRIDE" "$BOOT" "$1" <<'EOF'
import json, os, sys
path, boot, body = sys.argv[1], sys.argv[2], sys.argv[3]
if not body:
    if os.path.exists(path):
        os.remove(path)
    sys.exit(0)
values = {"boot_id": boot, **json.loads("{" + body + "}")}
with open(path + ".tmp", "w") as handle:
    json.dump(values, handle)
os.chmod(path + ".tmp", 0o644)
os.replace(path + ".tmp", path)
EOF
    sleep 3   # applied live within ~1 s
}
event "\"event\": \"start\", \"mhz\": $MHZ, \"heat_s\": $HEAT_S, \"cool_s\": $COOL_S"
set_override "\"gpu_max_mhz\": $MHZ, \"gpu_entry_mhz\": $MHZ"
event "\"event\": \"heat_start\""
bash scripts/burnin_block.sh "$HEAT_S" "$LABEL" gpu
reason=$(tail -1 "$DIR/$LABEL-events.jsonl" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("reason",""))
except Exception: print("no events")')
set_override ""
event "\"event\": \"heat_end\", \"reason\": \"$reason\""
if [ "$reason" = completed ]; then
    event "\"event\": \"cool_start\""
    for _ in $(seq 1 "$COOL_S"); do
        [ -e "$READY" ] || { reason="readiness vanished during cool-down"; break; }
        sleep 1
    done
    event "\"event\": \"cool_end\", \"reason\": \"$reason\""
fi
event "\"event\": \"end\", \"reason\": \"$reason\""
date -Iseconds > "$DIR/$LABEL.done"
