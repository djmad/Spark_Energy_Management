# Credits and provenance

This project combines two independently published bodies of work and adds a
hardware-validated Lenovo ThinkStation PGX adaptation.

## Z841973620 / 841973620

Full credit goes to **Z841973620** for the foundational embedded-controller
reverse engineering published in
[`Z841973620/dgx-spark-fan-override`](https://github.com/Z841973620/dgx-spark-fan-override).

That work established and documented:

- EC thermal mailbox command 5 and its `07 05 00 xx xx` request frame;
- automatic value `0xffff` and maximum value `13500` / `0x34bc`;
- override slots `0x119190` and `0x119192`;
- fan RPM limits, PWM conversion, thermal profiles, and telemetry command;
- OEM eSPI command 17, the raw FF-A service UUID, and shared-page layout;
- the meaning of secure-service statuses `0`, `5`, and `10`.

Upstream commit reviewed for this adaptation:

```text
7ffcb3e28327d3e0f4d62210845d3357f0fbe256
Author identity: 841973620 <841973620@qq.com>
Date: 2026-08-23
```

The complete English technical translation is preserved in
[`docs/Z841973620_EC_RESEARCH_EN.md`](docs/Z841973620_EC_RESEARCH_EN.md).
It covers every section of the upstream Chinese README. The upstream kernel,
packaging, and command sources were already written in English.

## Christopher Owen

The guarded packet-service kernel driver, Linux thermal/hwmon integration,
userspace controller, DKMS configuration, automated tests, bounded recovery,
incident logging, and most operational documentation originate from
[`christopherowen/dgx-spark-fan-control`](https://github.com/christopherowen/dgx-spark-fan-control).

Upstream commit used as the implementation base:

```text
deb2ea155f6698b769ff4977dea2119e3b32f460
Christopher Owen <3221756+christopherowen@users.noreply.github.com>
Date: 2026-09-08
```

The upstream source identifies Christopher Owen as the kernel module author.
That author metadata remains intact.

## Lenovo ThinkStation PGX adaptation

The adaptation adds:

- a strict DMI allowance for `LENOVO` product `30KL0005GF`;
- validation on NVIDIA Kernel `7.0.0-1019-nvidia`;
- Lenovo EC 3.5.8 firmware verification and disassembly confirmation;
- cold-power recovery evidence for secure-service status `5`;
- a fixed maximum-at-boot service with automatic rollback on stop;
- Lenovo-specific installation and firmware documentation.

The adaptation does not claim authorship of the upstream discoveries or driver
design. Please retain this file and the GPL-2.0-only license in forks and binary
distributions.
