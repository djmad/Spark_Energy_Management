#!/bin/bash
# Temperature-ceiling steps under one continuous worst-case load (operator,
# 27 September 2026: ACPI abort 96 C, CPU target 92 C, GPU target 78 C "klingt
# realistisch", "schauen wir ab wann der Hardware-Regler (vendor) eingreift").
# The shared zone ceiling is min(target 92, 96 - 3 - trend_margin_c); each step
# lowers trend_margin_c live through the boot-bound override (no restart, the
# load keeps running): 4 -> 89 C, 3 -> 90 C, 2 -> 91 C, 1 -> 92 C.
# The operator's unchanged burn-in scripts run for the whole block
# (burnin_block.sh, both); an abort stops them and ends the block. The override
# is removed at the end. Root main agent, hardware claim, vLLM stopped.
set -u
STEP_S=${1:-300}
shift || true
MARGINS=${*:-4 3 2 1}
cd "$(dirname "$0")/.."
DIR=/var/lib/spark-energy
OVERRIDE=/run/spark-energy/qualification.json
BOOT=$(cat /proc/sys/kernel/random/boot_id)
LABEL=temp-raise
event() { echo "{\"utc\": \"$(date -Iseconds)\", $1}" >> "$DIR/$LABEL-steps.jsonl"; sync; }
set_margin() {
    python3 - "$OVERRIDE" "$BOOT" "$1" <<'EOF'
import json, os, sys
path, boot, margin = sys.argv[1], sys.argv[2], float(sys.argv[3])
values = {"boot_id": boot, "gpu_max_mhz": 2200, "gpu_target_c": 78.0, "cpu_target_c": 92.0,
          "tuning": {"trend_margin_c": margin}}
with open(path + ".tmp", "w") as handle:
    json.dump(values, handle)
os.chmod(path + ".tmp", 0o644)
os.replace(path + ".tmp", path)
EOF
    for _ in $(seq 1 20); do
        python3 -c "import json,sys; d=json.load(open('/run/spark-energy/status.json')); \
sys.exit(0 if (d.get('control') or {}).get('tuning',{}).get('trend_margin_c')==float($1) else 1)" \
            2>/dev/null && return 0
        sleep 0.5
    done
    return 1
}
count=$(echo $MARGINS | wc -w)
first=$(echo $MARGINS | cut -d' ' -f1)
set_margin "$first" || { event "\"event\": \"override_not_applied\""; exit 1; }
event "\"event\": \"block_start\", \"margins\": \"$MARGINS\", \"step_s\": $STEP_S"
bash scripts/burnin_block.sh $((STEP_S * count)) "$LABEL" both &
BLOCK=$!
for margin in $MARGINS; do
    [ "$margin" = "$first" ] || set_margin "$margin" || { event "\"event\": \"override_not_applied\", \"margin\": $margin"; break; }
    event "\"event\": \"step\", \"trend_margin_c\": $margin, \"ceiling_c\": $(python3 -c "print(min(92, 96 - 3 - $margin))")"
    for _ in $(seq 1 "$STEP_S"); do
        kill -0 "$BLOCK" 2>/dev/null || break 2
        sleep 1
    done
done
wait "$BLOCK"
reason=$(tail -1 "$DIR/$LABEL-events.jsonl" 2>/dev/null | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("reason",""))
except Exception: print("no events")')
rm -f "$OVERRIDE"
event "\"event\": \"block_end\", \"reason\": \"$reason\", \"override\": \"removed\""
date -Iseconds > "$DIR/$LABEL.done"
