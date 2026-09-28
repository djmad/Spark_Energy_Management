# Provenance of this driver copy

This folder is the exact source of the `dgx_ec_fan_control` kernel module (DKMS
package `dgx-spark-fan-control` 0.1.3) that energy_control's fan owner uses on the
reference Lenovo ThinkStation PGX. energy_control sets the additive fan floor
through this driver's cooling device; it never writes the EC directly.

- **Upstream:** https://github.com/christopherowen/dgx-spark-fan-control
- **Base:** upstream commit `deb2ea1` (8 September 2026).
- **Local changes:** in `local-changes-vs-upstream-deb2ea1.patch`. The platform match
  also accepts the qualified Lenovo ThinkStation PGX (`LENOVO` / `30KL0005GF`, whose
  DMI board name reads `INVALID`), and the protocol and troubleshooting docs say so.
  `kernel/dgx_ec_fan_control.c` here is identical to the installed DKMS source.
- **Licence:** GPL-2.0-only (see `LICENSE`). This licence covers this folder only.

## Build and install (DKMS, aarch64)

```sh
sudo cp -r drivers/dgx-spark-fan-control /usr/src/dgx-spark-fan-control-0.1.3
sudo dkms add -m dgx-spark-fan-control -v 0.1.3
sudo dkms install -m dgx-spark-fan-control -v 0.1.3
sudo modprobe dgx_ec_fan_control
```

Read the upstream README and `docs/troubleshooting.md` first. Load only one fan
driver, and keep firmware thermal protection intact.
