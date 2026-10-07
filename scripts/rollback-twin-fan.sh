#!/usr/bin/env bash
# Undo scripts/deploy-twin-fan.sh: restore the configuration from before the
# twin fan, then the previous code (/opt/spark-energy.prev-*), and restart.
#   sudo bash scripts/rollback-twin-fan.sh           # configuration and code
#   sudo bash scripts/rollback-twin-fan.sh --config  # configuration only: the new code still
#                                                     # contains the "predictive" fan
# The configuration goes first: the previous code does not know fan_policy "twin".
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash $0" >&2; exit 2; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CONF=/etc/spark-energy/config.json
BAK="$(ls -1 "$CONF".bak-*-pre-twin 2>/dev/null | sort | tail -n 1)"
[ -n "$BAK" ] || { echo "no $CONF.bak-*-pre-twin backup" >&2; exit 2; }
cp -a "$CONF" "$CONF.bak-$(date +%Y%m%dT%H%M%S)-twin"
cp -a "$BAK" "$CONF"
echo "restored configuration from $BAK"
if [ "${1:-}" = "--config" ]; then
  systemctl restart energy_control.service spark-energy-api.service
  echo "restarted with the restored configuration (twin code stays installed)"
else
  bash "$SRC/scripts/uninstall-energy-control.sh" --rollback
fi
