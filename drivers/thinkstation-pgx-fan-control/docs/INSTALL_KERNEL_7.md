# Install on Lenovo ThinkStation PGX with NVIDIA Kernel 7.x

[← README](../README.md) · [Supported firmware](SUPPORTED_FIRMWARE.md) · [Usage](usage.md)

These instructions target the Lenovo ThinkStation PGX `30KL0005GF` running an
NVIDIA-flavoured 7.x kernel. They were executed successfully with
`7.0.0-1019-nvidia`, Secure Boot enabled, and Lenovo EC firmware 3.5.8.

## 1. Confirm the platform and firmware

```sh
uname -m
uname -r
cat /sys/class/dmi/id/sys_vendor
cat /sys/class/dmi/id/product_name
fwupdmgr get-devices
```

The validated values are:

```text
aarch64
7.0.0-1019-nvidia
LENOVO
30KL0005GF
Embedded Controller: 3.5.8
UEFI firmware: 2.0.14
USB PD firmware: 0.5.22
```

The kernel patch level may be newer than the validated build, but matching
headers are mandatory. Stop if the vendor or product differs. See
[SUPPORTED_FIRMWARE.md](SUPPORTED_FIRMWARE.md) before testing another EC version.

## 2. Install build requirements

```sh
sudo apt-get update
sudo apt-get install build-essential dkms python3 mokutil \
  linux-headers-"$(uname -r)"
```

Confirm that the active kernel has a build tree:

```sh
test -r "/lib/modules/$(uname -r)/build/Makefile"
```

Run the hardware-free validation suite from the repository root:

```sh
./scripts/check
```

## 3. Prepare Secure Boot signing

Check whether Secure Boot is active:

```sh
mokutil --sb-state
```

Ubuntu/DGX OS normally keeps the DKMS signing pair here:

```text
/var/lib/shim-signed/mok/MOK.priv
/var/lib/shim-signed/mok/MOK.der
```

Check whether its public certificate is enrolled:

```sh
sudo mokutil --test-key /var/lib/shim-signed/mok/MOK.der
```

If it is not enrolled, request enrollment:

```sh
sudo mokutil --import /var/lib/shim-signed/mok/MOK.der
sudo reboot
```

Create a temporary password when prompted. During the next boot select
**Enroll MOK**, confirm the enrollment, and enter that password. After Linux
starts, repeat `mokutil --test-key`. Do not continue until it reports that the
certificate is enrolled.

If those files do not exist, use [`scripts/generate-signing-key`](../scripts/generate-signing-key)
and the detailed signing instructions in [installation.md](installation.md).

## 4. Remove competing fan-driver ownership

Only one driver may own each FF-A fan service. Check first:

```sh
lsmod | grep -E 'nvfancontrol|dgx_ec_fan_control'
systemctl is-enabled nvfancontrol.service dgx-fan-control.service \
  dgx-fan-max.service 2>/dev/null || true
```

If `nvfancontrol` previously set a fan override and its transport still works,
restore automatic mode before unloading it:

```sh
sudo nvfancontrol auto
sudo systemctl disable --now nvfancontrol.service
sudo modprobe -r nvfancontrol
```

Unloading `nvfancontrol` by itself does not clear its EC override. If its
automatic command fails with service status `5`, collect the logs and perform
an orderly shutdown followed by at least 60 seconds with power disconnected.
The cold start recovered the eSPI transport on the validated Lenovo system.

To prevent the older write-only module from binding later:

```sh
printf '%s\n' 'blacklist nvfancontrol' | \
  sudo tee /etc/modprobe.d/nvfancontrol-blacklist.conf >/dev/null
```

## 5. Install with DKMS

From this repository's root:

```sh
version=0.1.3
source_dir="/usr/src/dgx-spark-fan-control-$version"

sudo install -d -m 0755 "$source_dir/kernel"
sudo install -m 0644 dkms.conf "$source_dir/dkms.conf"
sudo install -m 0644 kernel/Makefile kernel/dgx_ec_fan_control.c \
  "$source_dir/kernel/"

sudo dkms add -m dgx-spark-fan-control -v "$version"
sudo dkms build -m dgx-spark-fan-control -v "$version" -k "$(uname -r)"
sudo dkms install -m dgx-spark-fan-control -v "$version" -k "$(uname -r)"
```

Verify the DKMS state and signature:

```sh
dkms status -m dgx-spark-fan-control -v 0.1.3
modinfo -F signer dgx_ec_fan_control
```

The signer must match the enrolled key. A `Key was rejected by service` error
means the key used by DKMS is not enrolled.

## 6. Install the command and systemd units

```sh
sudo install -D -m 0755 userspace/dgx_fan_control.py \
  /usr/local/sbin/dgx-fan-control
sudo install -D -m 0644 systemd/dgx_ec_fan_control.conf \
  /etc/modules-load.d/dgx_ec_fan_control.conf
sudo install -D -m 0644 systemd/dgx-fan-control.service \
  /etc/systemd/system/dgx-fan-control.service
sudo install -D -m 0644 systemd/dgx-fan-max.service \
  /etc/systemd/system/dgx-fan-max.service
sudo systemctl daemon-reload
```

Load the module and verify its read-only startup contract:

```sh
sudo modprobe dgx_ec_fan_control
dgx-fan-control status
sudo journalctl -k -b -g 'dgx-ec-fan-control'
```

A healthy initial result contains:

```text
pinned RPM capabilities fan0=1260..9000 fan1=1890..13500
additive fan-floor cooling device registered in automatic state
```

`dgx-fan-control status` must report `state=0/12`. The driver refuses to load
if it finds an unknown pre-existing floor.

## 7. Choose one startup policy

### Fixed maximum cooling

```sh
sudo systemctl disable --now dgx-fan-control.service
sudo systemctl enable --now dgx-fan-max.service
```

This sets state 12 at boot. Stopping the unit requests and verifies automatic
control:

```sh
sudo systemctl disable --now dgx-fan-max.service
```

### Temperature-based performance curve

```sh
sudo systemctl disable --now dgx-fan-max.service
sudo systemctl enable --now dgx-fan-control.service
```

Do not enable both services. The temperature daemon selects an additive floor
from current temperature; its curve is documented in [usage.md](usage.md).

### Firmware automatic mode

```sh
sudo systemctl disable --now dgx-fan-max.service dgx-fan-control.service
sudo dgx-fan-control automatic
```

The module can remain loaded for telemetry while the floor is unset.

## 8. Verify maximum RPM

After selecting maximum mode:

```sh
dgx-fan-control status
for device in /sys/class/hwmon/hwmon*; do
  if [ "$(cat "$device/name" 2>/dev/null)" = dgx_ec_fan ]; then
    printf 'Fan 0: %s RPM\n' "$(cat "$device/fan1_input")"
    printf 'Fan 1: %s RPM\n' "$(cat "$device/fan2_input")"
  fi
done
```

The validated maximum readings are 9000 and 13,500 RPM. Firmware ramps speed,
so allow several seconds after selecting state 12.

## Kernel 7.x updates

DKMS should rebuild the module automatically when a new matching NVIDIA kernel
and headers are installed. Before rebooting into a new kernel, check:

```sh
next_kernel='7.x.y-nvidia'  # replace with the installed release
dkms status -m dgx-spark-fan-control -v 0.1.3 -k "$next_kernel"
```

After booting the new kernel, verify the module signer, state, RPM telemetry,
and automatic rollback before relying on unattended maximum mode. Kernel 7.0
is hardware validated; later 7.x releases are build-compatible candidates,
not automatically hardware-qualified versions.

## Remove the project

First stop both optional policy services and require verified automatic state:

```sh
sudo systemctl disable --now dgx-fan-max.service dgx-fan-control.service
sudo dgx-fan-control automatic
dgx-fan-control status                    # require state=0/12
sudo modprobe -r dgx_ec_fan_control
```

Do not continue if automatic restoration, readback, or module removal fails.
Keep the controller and logs available and follow the
[troubleshooting guide](troubleshooting.md).

After successful restoration, remove the DKMS package and installed files:

```sh
sudo dkms remove -m dgx-spark-fan-control -v 0.1.3 --all
sudo rm -f /etc/modules-load.d/dgx_ec_fan_control.conf
sudo rm -f /etc/systemd/system/dgx-fan-control.service
sudo rm -f /etc/systemd/system/dgx-fan-max.service
sudo rm -f /usr/local/sbin/dgx-fan-control
sudo systemctl daemon-reload
```

The enrolled signing certificate may be shared with other DKMS modules; keep
it unless you have separately verified that nothing else uses it.
