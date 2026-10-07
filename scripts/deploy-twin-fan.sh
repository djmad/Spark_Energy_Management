#!/usr/bin/env bash
# Install the twin fan (doc/59) and switch the committed configuration to it.
#   sudo bash scripts/deploy-twin-fan.sh
# Steps: refuse when an agent holds the hardware claim; back up
# /etc/spark-energy/config.json; install the code with the standard installer
# (runs the test suite, keeps /opt/spark-energy.prev-*); set fan_policy "twin"
# and fan_min_state 2; restart energy_control and spark-energy-api (vLLM keeps
# running: the GPU cap starts at the entry ceiling and ramps back within
# ~30 s); wait for a healthy status. If the service does not come up, it rolls
# back on its own (scripts/rollback-twin-fan.sh).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0" >&2; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/spark-energy/config.json
STATUS=/run/spark-energy/status.json
STAMP="$(date +%Y%m%dT%H%M%S)"
CLAIM=/run/spark-energy/agent-command
if [ -e "$CLAIM" ]; then
  echo "hardware claim held ($CLAIM):" >&2; cat "$CLAIM" >&2; exit 3
fi
cp -a "$CONF" "$CONF.bak-$STAMP-pre-twin"
echo "config backup: $CONF.bak-$STAMP-pre-twin"
bash "$SRC/scripts/install-energy-control.sh"
TMP="$(mktemp /etc/spark-energy/.config.XXXXXX)"
python3 - "$CONF" "$TMP" <<'PY'
import json, sys
src, dst = sys.argv[1], sys.argv[2]
values = json.load(open(src))
values["fan_policy"] = "twin"
values["fan_min_state"] = 2
open(dst, "w").write(json.dumps(values, indent=2, sort_keys=True) + "\n")
PY
chown root:root "$TMP"; chmod 0644 "$TMP"
( cd /opt/spark-energy && python3 -c "import sys; from energy_control.service import load_config; c = load_config(sys.argv[1]); print('config ok:', c.fan_policy, c.fan_min_state)" "$TMP" )
mv "$TMP" "$CONF"
systemctl restart energy_control.service spark-energy-api.service
echo "restarted; waiting for energy_control (up to 120 s)"
for _ in $(seq 1 60); do
  sleep 2
  if python3 - "$STATUS" <<'PY' 2>/dev/null
import json, sys, time
d = json.load(open(sys.argv[1]))
fresh = time.time() - d["utc_ns"] / 1e9 < 5
ok = fresh and d.get("limits", {}).get("fan_policy") == "twin" and d.get("mode") not in ("FAULT", "ABORT", "SAFE_STATE", None)
fan = d.get("control", {}).get("fan", {})
print(f"mode {d.get('mode')}, GPU cap {d.get('gpu', {}).get('cap_mhz')} MHz, TGPU {d.get('zones_c', {}).get('TGPU')} C, "
      f"fan floor {d.get('fan', {}).get('floor')} (target {fan.get('target')}, steady TGPU {fan.get('steady_tgpu_c')} C)")
sys.exit(0 if ok else 1)
PY
  then
    echo "twin fan active. The fan steps down one level per minute from 12."
    exit 0
  fi
done
echo "energy_control did not report the twin fan within 120 s; rolling back" >&2
bash "$SRC/scripts/rollback-twin-fan.sh"
exit 1
