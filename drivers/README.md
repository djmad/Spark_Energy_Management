# Fan drivers

energy_control sets the additive fan floor through the `dgx_ec_fan_control` kernel
module (Linux cooling device `dgx_ec_fan`). It never writes the embedded controller
directly. Both folders are GPL-2.0-only, and each keeps its own `LICENSE`. The
repository's CC BY-NC 4.0 licence does not apply to them.

| Folder | What it is | Use it for |
| --- | --- | --- |
| [`thinkstation-pgx-fan-control/`](thinkstation-pgx-fan-control/) | The documented Lenovo ThinkStation PGX adaptation (`0.1.3-lenovo.1`). It carries the full credits for the Z841973620 EC research and Christopher Owen's driver, hardware validation, Secure Boot signing scripts, a userspace controller and systemd units. | **New installs.** Start with its README. |
| [`dgx-spark-fan-control/`](dgx-spark-fan-control/) | Byte-for-byte the source of the DKMS module installed on the reference machine: upstream `deb2ea1` plus the Lenovo PGX platform match (`PROVENANCE.md`, `local-changes-vs-upstream-deb2ea1.patch`). | Reproducing the exact reference build. |

The two kernel sources are functionally identical. They differ only in a header
comment and the `MODULE_DESCRIPTION` string.

Load only **one** fan driver. With energy_control installed, do not run the fan
units shipped in these folders (`dgx-fan-max.service`, `dgx-fan-control.service`):
energy_control is the only fan-floor writer and conflicts with `dgx-fan-max`.
