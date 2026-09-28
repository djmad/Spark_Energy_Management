#!/usr/bin/env bash
# Install the reviewed energy_control code (service, simulation, dashboard, docs)
# under /opt/spark-energy and stage the systemd units. It does NOT enable or
# start any service and does not touch other fan/CPU controllers: switching
# over is a separate, supervised step (INSTALL.md, doc/41-handoff-runbook.md).
# On a clean machine it seeds /etc/spark-energy/config.json from
# deploy/config.example.json; an existing configuration is never overwritten.
set -euo pipefail
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DST=/opt/spark-energy
CONF_DIR=/etc/spark-energy
STAMP="$(date +%Y%m%dT%H%M%S)"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
  || { echo "Python 3.12 or newer is required" >&2; exit 2; }
(cd "$SRC" && python3 -m unittest discover -s tests >/dev/null 2>&1) \
  || { echo "test suite failed; not installing" >&2; exit 1; }
STAGE="$DST.new"
rm -rf "$STAGE"
install -d -o root -g root -m 0755 "$STAGE"
for part in energy_control simulation dashboard doc; do
  cp -r "$SRC/$part" "$STAGE/$part"
done
cp "$SRC/goal.md" "$SRC/AGENTS.md" "$SRC/README.md" "$SRC/INSTALL.md" "$SRC/LICENSE" "$STAGE/"
find "$STAGE" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$STAGE" -name '*.jsonl' -path '*/doc/*' -delete
chown -R root:root "$STAGE"
find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
(cd "$STAGE" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z \
   | xargs -0 sha256sum > MANIFEST.sha256)
echo "source: $SRC  installed: $STAMP" > "$STAGE/INSTALLED_FROM"
if [ -d "$DST" ]; then mv "$DST" "$DST.prev-$STAMP"; fi
mv "$STAGE" "$DST"
# Rollback copies: keep the newest KEEP_PREVIOUS (default 3), remove older ones.
KEEP_PREVIOUS="${KEEP_PREVIOUS:-3}"
ls -1d "$DST".prev-* 2>/dev/null | sort | head -n "-$KEEP_PREVIOUS" | while read -r old; do
  case "$old" in "$DST".prev-*) rm -rf -- "$old" ;; esac
done
for unit in energy_control.service spark-energy-api.service spark-energy-dashboard.service; do
  install -o root -g root -m 0644 "$SRC/deploy/$unit" "/etc/systemd/system/$unit"
done
install -d -o root -g root -m 0755 "$CONF_DIR"
if [ ! -e "$CONF_DIR/config.json" ]; then
  install -o root -g root -m 0644 "$SRC/deploy/config.example.json" "$CONF_DIR/config.json"
  echo "seeded $CONF_DIR/config.json from deploy/config.example.json"
fi
systemctl daemon-reload
echo "installed $DST ($(wc -l < "$DST/MANIFEST.sha256") files); units staged, not enabled or started"
