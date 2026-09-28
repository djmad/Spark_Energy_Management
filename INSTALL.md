# Installation

This takes a Lenovo ThinkStation PGX (NVIDIA GB10) with nothing installed to
energy_control running. Run every command on the machine itself, from the root of this
repository. Read the safety limits in the [README](README.md) first.

## 1. Prerequisites

```sh
sudo apt-get update
sudo apt-get install build-essential dkms linux-headers-"$(uname -r)" python3 mokutil
python3 --version            # 3.12 or newer
nvidia-smi                   # the NVIDIA driver is installed and sees the GB10 GPU
python3 -m unittest discover -s tests    # everything passes without hardware
```

- PyTorch with CUDA is only needed for the GPU burn-in in `tools/burnin/`.
- vLLM is optional: when it serves on 127.0.0.1:8000, a new prompt re-arms the GPU entry
  ceiling before its prefill.

## 2. Fan driver

energy_control sets an *additive* fan floor through the `dgx_ec_fan_control` kernel
module; the firmware can always add more cooling. See [drivers/README.md](drivers/README.md).

```sh
mokutil --sb-state
sudo bash scripts/install-driver.sh      # default: drivers/thinkstation-pgx-fan-control
```

- **Secure Boot enabled:** first enroll a signing key and set DKMS's signing identity, as in
  steps 2 and 3A of
  [the driver's installation guide](drivers/thinkstation-pgx-fan-control/docs/installation.md).
  The script refuses to build until DKMS has a signing key.
- **What the script does:**
  - checks the platform (`LENOVO` `30KL0005GF`, or NVIDIA DGX Spark `P4242`);
  - builds and installs the module with DKMS, loads it, and arms it to load at boot;
  - prints the `dgx_ec_fan_floor` cooling device.
- **Load only one fan driver.** Do not enable the fan units shipped in `drivers/`
  (`dgx-fan-max`, `dgx-fan-control`): energy_control is the only fan-floor writer.

## 3. Service

```sh
sudo bash scripts/install-energy-control.sh
```

- **What the installer does:**
  - runs the test suite;
  - installs the service, simulation, dashboard and docs to `/opt/spark-energy`;
  - stages the systemd units `energy_control`, `spark-energy-api` and
    `spark-energy-dashboard`;
  - on a clean machine, seeds `/etc/spark-energy/config.json` from
    [`deploy/config.example.json`](deploy/config.example.json).
- **What it does not do:** enable or start anything.
- **Rollback copies:** it keeps the three previous versions as `/opt/spark-energy.prev-*`.

## 4. Configuration

`/etc/spark-energy/config.json` must be root-owned and not writable by group or others.
The example holds the qualified settings of the reference machine:

| Setting | Value |
| --- | --- |
| GPU entry ceiling / maximum | 1700 / 2200 MHz (hard limit 2500 MHz, above 2200 at your own risk) |
| CPU target / GPU target | 92 °C / 78 °C |
| CPU/GPU priority | 1 : 1 |
| Fan | `predictive`, floor 6, 12 under load |

- Change values before the first start if your machine differs.
- After that, change them live (step 5): the service rewrites the file itself on every
  committed change.
- Recommended after the first start: `--tune trend_margin_c=3`, which gives a control
  ceiling of 90 °C.

## 5. Operator password (optional, for live changes)

This lets one Unix user change settings with a password; without it the operator socket
stays off. `1000` is that user's UID (`id -u`).

```sh
cd /opt/spark-energy && sudo python3 -c "import getpass; from energy_control.broker import PasswordVerifier; \
from energy_control.operator_broker import write_password_file; \
write_password_file(PasswordVerifier.provision(getpass.getpass('operator password: ')), 1000)"
```

Then, as that user:

```sh
cd /opt/spark-energy && python3 -m energy_control.cli --gpu-target-c 78
```

## 6. Start and check

Stop any other program that writes GPU clock locks, CPU `scaling_max_freq` or the fan floor
first; the unit conflicts with the known ones.

```sh
sudo systemctl enable --now energy_control spark-energy-api
systemctl status energy_control
journalctl -u energy_control -n 30          # "readiness", entry ceiling, policy lines
cat /run/spark-energy/entry-ceiling         # present = armed
python3 -m json.tool /run/spark-energy/status.json | head -40
```

At idle you should see:
- mode `COOLDOWN` or `HOLD`;
- GPU cap 1700 MHz;
- the fan stepping down to its floor;
- no `abort` in the journal.

A first load (an LLM prompt or `tools/burnin/`) ramps from 1700 MHz once the GPU is busy.

## 7. Dashboard (optional)

```sh
sudo systemctl enable --now spark-energy-dashboard
# open http://127.0.0.1:8790/
```

- It is read-only, binds to loopback, and reads only `/run/spark-energy/status.json`.
- To share it, put it behind an authenticated reverse proxy, never a public bind. See
  [dashboard/README.md](dashboard/README.md).

## Update

```sh
git pull
sudo bash scripts/install-energy-control.sh
sudo systemctl restart energy_control spark-energy-api spark-energy-dashboard
```

The configuration in `/etc/spark-energy` is kept.

## Rollback and uninstall

```sh
sudo bash scripts/uninstall-energy-control.sh --rollback          # newest /opt/spark-energy.prev-*, restarts running units
sudo bash scripts/uninstall-energy-control.sh --remove            # stop, disable, remove code and units
sudo bash scripts/uninstall-energy-control.sh --remove --driver   # ... and the DKMS fan driver
sudo bash scripts/uninstall-energy-control.sh --remove --purge    # ... and /etc/spark-energy + /var/lib/spark-energy
```

`--remove` hands the hardware back:
- fan floor 0, the firmware's own fan policy;
- CPU maxima at the hardware limits;
- GPU clock lock reset (`nvidia-smi -rgc`).

Unless you pass `--purge`, the configuration, password, audit and run logs are kept.
