# Changelog

## 0.1.3-lenovo.1 — 2026-09-25

- Added strict support for Lenovo ThinkStation PGX product `30KL0005GF`.
- Validated Linux kernel `7.0.0-1019-nvidia` with Secure Boot enabled.
- Validated Lenovo EC 3.5.8 (`0x03000508`), UEFI 2.0.14, and USB PD 0.5.22.
- Confirmed 9000/13,500 RPM at cooling state 12 and verified state-0 rollback.
- Added a fixed maximum-at-boot systemd service with automatic stop rollback.
- Added Kernel 7.x installation and firmware-compatibility documentation.
- Added an English preservation of Z841973620's original EC research.
- Added detailed upstream authorship and provenance credits.

## Upstream base

The implementation begins from `dgx-spark-fan-control` 0.1.3 at commit
`deb2ea155f6698b769ff4977dea2119e3b32f460`. See [CREDITS.md](CREDITS.md).
