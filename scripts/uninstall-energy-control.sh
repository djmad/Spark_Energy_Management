#!/usr/bin/env bash
# Roll back or remove energy_control.
#
#   sudo bash scripts/uninstall-energy-control.sh --rollback   # restore the newest /opt/spark-energy.prev-*
#   sudo bash scripts/uninstall-energy-control.sh --remove     # stop, disable and remove the service
#   sudo bash scripts/uninstall-energy-control.sh --remove --driver   # ... and the DKMS fan driver
#
# --rollback swaps the installed code for the newest rollback copy and restarts
# the running units; the configuration in /etc/spark-energy stays.
# --remove stops and disables the units (energy_control's own stop path
# leaves minimum caps and fan 12), removes the code, units and runtime files,
# then returns the fan floor to 0 (the firmware's own policy) and the CPU and
# GPU clocks to their hardware limits. /etc/spark-energy and
# /var/lib/spark-energy (configuration, audit, run logs) are kept unless
# --purge is given.
set -euo pipefail
DST=/opt/spark-energy
UNITS="spark-energy-dashboard.service spark-energy-api.service energy_control.service"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
mode=""; driver=0; purge=0
for arg in "$@"; do
  case "$arg" in
    --rollback) mode=rollback ;;
    --remove) mode=remove ;;
    --driver) driver=1 ;;
    --purge) purge=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
[ -n "$mode" ] || { sed -n '2,6p' "$0"; exit 2; }

running=""
for unit in $UNITS; do
  systemctl is-active --quiet "$unit" && running="$running $unit"
done

if [ "$mode" = rollback ]; then
  prev="$(ls -1d "$DST".prev-* 2>/dev/null | sort | tail -n 1)"
  [ -n "$prev" ] || { echo "no rollback copy in $DST.prev-*" >&2; exit 2; }
  [ -n "$running" ] && systemctl stop $running
  mv "$DST" "$DST.rolled-back-$(date +%Y%m%dT%H%M%S)"
  mv "$prev" "$DST"
  [ -n "$running" ] && systemctl start $running
  echo "restored $(cat "$DST/INSTALLED_FROM" 2>/dev/null || echo "$prev"); restarted:${running:- none}"
  exit 0
fi

# --remove
[ -n "$running" ] && systemctl stop $running
for unit in $UNITS; do
  systemctl disable --quiet "$unit" 2>/dev/null || true
  rm -f "/etc/systemd/system/$unit"
done
systemctl daemon-reload
rm -rf -- "$DST" "$DST.new"
ls -1d "$DST".prev-* "$DST".rolled-back-* 2>/dev/null | while read -r old; do
  case "$old" in "$DST".*) rm -rf -- "$old" ;; esac
done
rm -rf -- /run/spark-energy /run/energy-control

# Hand the hardware back: firmware fan policy, full CPU range, no GPU lock.
for dev in /sys/class/thermal/cooling_device*; do
  [ "$(cat "$dev/type" 2>/dev/null)" = dgx_ec_fan_floor ] && echo 0 > "$dev/cur_state"
done
for policy in /sys/devices/system/cpu/cpufreq/policy*; do
  cat "$policy/cpuinfo_max_freq" > "$policy/scaling_max_freq"
done
command -v nvidia-smi >/dev/null && nvidia-smi -rgc >/dev/null || true

if [ "$driver" = 1 ]; then
  modprobe -r dgx_ec_fan_control || true
  rm -f /etc/modules-load.d/dgx_ec_fan_control.conf
  dkms status | sed -n 's/^\(dgx-spark-fan-control\)[/,] *\([^,:]*\).*/\1 \2/p' | sort -u | while read -r name version; do
    dkms remove -m "$name" -v "$version" --all || true
    rm -rf -- "/usr/src/$name-$version"
  done
fi
if [ "$purge" = 1 ]; then
  rm -rf -- /etc/spark-energy /var/lib/spark-energy
fi
echo "energy_control removed; fan floor 0 (firmware), CPU maxima and GPU clocks at hardware limits"
[ "$driver" = 1 ] && echo "fan driver removed (DKMS and module autoload)"
[ "$purge" = 1 ] || echo "kept: /etc/spark-energy (configuration, password) and /var/lib/spark-energy (audit, run logs)"
