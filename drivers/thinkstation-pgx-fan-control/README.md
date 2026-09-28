# ThinkStation PGX Fan Control

Linux fan control for the Lenovo ThinkStation PGX / DGX Spark platform.

This project exposes both embedded-controller fan RPM readings and a safe,
additive minimum fan-speed control. Lenovo's normal thermal policy remains
active and may always request more cooling. State `0` removes the added floor;
state `12` requests the maximum shared floor and produces approximately
**9000 RPM** on fan 0 and **13,500 RPM** on fan 1.

## Verified Lenovo configuration

Hardware validation was completed on 25 September 2026:

| Component | Verified value |
| --- | --- |
| System vendor | `LENOVO` |
| Product | ThinkStation PGX `30KL0005GF` |
| Architecture | `aarch64` / NVIDIA GB10 |
| Kernel | `7.0.0-1019-nvidia` |
| Embedded controller | Lenovo EC `3.5.8` (`0x03000508`) |
| UEFI firmware | `2.0.14` |
| USB PD firmware | `0.5.22` |
| ARM FF-A interface | `1.2` |
| Packet service | UUID `78b04d80-d21d-4986-8acb-467b60247ac5`, partition `0x8003` |
| OEM eSPI service | UUID `884a63a0-3285-4120-83aa-eec008a0a546`, partition `0x11` |

The driver fails closed if the DMI identity, FF-A version, partition details,
capability identifier, operating mode, fan ranges, or initial override state do
not match the verified contract. Other Lenovo model numbers and future firmware
versions require separate validation.

## Features

- Exact Lenovo PGX platform guard for product `30KL0005GF`.
- Secure FF-A packet interface; no raw controller MMIO or arbitrary EC access.
- Live fan RPM readings through Linux `hwmon`.
- Thirteen cooling states: automatic plus twelve additive RPM floors.
- Readback after writes and ownership checks before changing an existing floor.
- Automatic restoration during orderly service stop, suspend, reboot, and
  module removal.
- DKMS support and Secure Boot module signing.
- Fixed maximum-at-boot service and an optional temperature-based performance
  curve.
- Bounded diagnostics for the observed secure-firmware pending-state failure.

## Install on NVIDIA Kernel 7.x

Follow [Kernel 7.x installation](docs/INSTALL_KERNEL_7.md). The short version is:

```sh
sudo apt-get install build-essential dkms linux-headers-"$(uname -r)" python3 mokutil
./scripts/check
```

The complete guide covers platform and firmware checks, Secure Boot key
enrollment, DKMS installation, service selection, RPM verification, rollback,
updates, and removal.

## Usage

### Maximum cooling now and after every boot

```sh
sudo systemctl enable --now dgx-fan-max.service
dgx-fan-control status
```

### Return to Lenovo/NVIDIA automatic thermal control

```sh
sudo systemctl disable --now dgx-fan-max.service
sudo dgx-fan-control automatic
dgx-fan-control status
```

Expect `state=0/12` after restoration. Firmware hysteresis can keep the fans
fast briefly while they ramp down.

### Select a temporary floor

```sh
sudo systemctl stop dgx-fan-max.service dgx-fan-control.service
sudo dgx-fan-control set-state 8
dgx-fan-control status
```

State 8 requests a 9000-RPM common floor. Fan 0 saturates at 9000 RPM while
fan 1 can continue to 13,500 RPM. See [usage](docs/usage.md) for every state,
RPM monitoring, job wrappers, and the optional temperature-based daemon.

## How it works

```text
dgx-fan-control
  -> Linux thermal cooling device and hwmon
  -> dgx_ec_fan_control kernel module
  -> ARM FF-A Direct Request 2
  -> EC packet service at arm-ffa-18
  -> embedded-controller thermal mailbox
  -> additive lower RPM floor at EC SRAM 0x119192
  -> firmware RPM-to-PWM policy
  -> PWM0/TACH0 and PWM1/TACH1
```

The original low-level research showed that EC mailbox command 5 writes the
high override slot at `0x119192`. Despite its firmware name, this slot raises
the effective minimum requested speed. This implementation uses the safer
packet service, reads the current value before and after changes, and never
writes the upper/capping slot at `0x119190`.

## Documentation

- [Kernel 7.x installation](docs/INSTALL_KERNEL_7.md)
- [Supported hardware and firmware](docs/SUPPORTED_FIRMWARE.md)
- [Usage and fan states](docs/usage.md)
- [Protocol and safety model](docs/protocol.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Validation record](docs/validation.md)
- [Lenovo hardware validation, 25 September 2026](docs/LENOVO_VALIDATION_20260925.md)
- [English translation of Z841973620's EC research](docs/Z841973620_EC_RESEARCH_EN.md)
- [Credits and provenance](CREDITS.md)

## Credits

Full credit for discovering and documenting the EC command, override slots,
fan limits, thermal policy, and raw FF-A/eSPI path goes to
**[Z841973620](https://github.com/Z841973620)** and the original
[`dgx-spark-fan-override`](https://github.com/Z841973620/dgx-spark-fan-override)
project.

The guarded packet-service driver, readback/ownership model, userspace control,
tests, and recovery research originate from
**[Christopher Owen](https://github.com/christopherowen)** and
[`dgx-spark-fan-control`](https://github.com/christopherowen/dgx-spark-fan-control).

See [CREDITS.md](CREDITS.md) for detailed attribution and upstream commits.

## License

This repository is distributed under [GPL-2.0-only](LICENSE), matching both
upstream kernel projects and their SPDX identifiers. Preserve the copyright,
license, and attribution notices when redistributing modified versions.
