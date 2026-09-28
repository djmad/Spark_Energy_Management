#!/usr/bin/env bash
# Install the fan-floor kernel module (dgx_ec_fan_control) with DKMS.
#
#   sudo bash scripts/install-driver.sh [thinkstation-pgx-fan-control | dgx-spark-fan-control]
#
# The default is drivers/thinkstation-pgx-fan-control, the documented Lenovo
# ThinkStation PGX adaptation (see drivers/README.md). The script checks the
# platform, the kernel headers and Secure Boot, then registers, builds,
# installs and loads the module, and arms it to load at boot. It refuses when
# another fan driver owns the device. With Secure Boot enforcing, DKMS must
# sign with an enrolled key first; see the driver's docs/installation.md.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VARIANT="${1:-thinkstation-pgx-fan-control}"
case "$VARIANT" in
  thinkstation-pgx-fan-control|dgx-spark-fan-control) ;;
  *) echo "unknown driver folder: $VARIANT" >&2; exit 2 ;;
esac
SRC="$ROOT/drivers/$VARIANT"
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }
[ "$(uname -m)" = aarch64 ] || { echo "this driver builds for aarch64 (GB10) only" >&2; exit 2; }
command -v dkms >/dev/null || { echo "install dkms first: apt-get install dkms build-essential linux-headers-$(uname -r)" >&2; exit 2; }
[ -d "/lib/modules/$(uname -r)/build" ] || { echo "kernel headers for $(uname -r) are missing" >&2; exit 2; }

vendor="$(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || true)"
product="$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
case "$vendor/$product" in
  LENOVO/30KL0005GF|NVIDIA/NVIDIA_DGX_Spark) ;;
  *) echo "platform $vendor/$product is not qualified for this driver; stopping" >&2; exit 2 ;;
esac

NAME="$(sed -n 's/^PACKAGE_NAME="\(.*\)"/\1/p' "$SRC/dkms.conf")"
VERSION="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "$SRC/dkms.conf")"
[ -n "$NAME" ] && [ -n "$VERSION" ] || { echo "cannot read $SRC/dkms.conf" >&2; exit 2; }

if lsmod | grep -q '^dgx_ec_fan_control '; then
  echo "dgx_ec_fan_control is already loaded:"; dkms status | grep -i fan || true
  echo "remove the existing module first (INSTALL.md, section Uninstall) to reinstall"; exit 0
fi
if lsmod | grep -qiE '^(dgx_fan|nvfan|dgx_spark_fan)'; then
  echo "another fan driver is loaded; load only one fan driver" >&2; exit 2
fi

if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -q 'SecureBoot enabled'; then
  if ! grep -qsE '^[[:space:]]*mok_signing_key=' /etc/dkms/framework.conf /etc/dkms/framework.conf.d/*.conf; then
    echo "Secure Boot is enabled but DKMS has no signing key configured." >&2
    echo "Follow drivers/thinkstation-pgx-fan-control/docs/installation.md (steps 2 and 3A) first." >&2
    exit 2
  fi
fi

DEST="/usr/src/$NAME-$VERSION"
if [ -e "$DEST" ]; then
  echo "$DEST already exists; remove it or its DKMS entry first" >&2; exit 2
fi
install -d -m 0755 "$DEST"
cp -r "$SRC/dkms.conf" "$SRC/kernel" "$DEST/"
dkms add -m "$NAME" -v "$VERSION"
dkms build -m "$NAME" -v "$VERSION"
dkms install -m "$NAME" -v "$VERSION"
modprobe dgx_ec_fan_control
install -m 0644 "$ROOT/drivers/thinkstation-pgx-fan-control/systemd/dgx_ec_fan_control.conf" \
  /etc/modules-load.d/dgx_ec_fan_control.conf
for dev in /sys/class/thermal/cooling_device*; do
  if [ "$(cat "$dev/type" 2>/dev/null)" = dgx_ec_fan_floor ]; then
    echo "fan-floor device ready: $dev (state $(cat "$dev/cur_state") of $(cat "$dev/max_state"))"
    exit 0
  fi
done
echo "module loaded but no dgx_ec_fan_floor cooling device appeared; check: journalctl -k | grep -i dgx_ec" >&2
exit 1
