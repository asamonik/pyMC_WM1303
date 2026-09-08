# Installation Guide

> Complete installation instructions for the WM1303 Bridge/Repeater system

## Prerequisites

### Hardware

- **SenseCAP M1** (Raspberry Pi 4 + WM1303 LoRa HAT) — or compatible Pi 4 with WM1303 HAT
- MicroSD card (16 GB+ recommended)
- Ethernet or Wi-Fi connection
- Antenna connected to the WM1303 HAT

### Software

- **Raspberry Pi OS Lite** (Bookworm or newer) — freshly flashed
- SSH access enabled
- Internet connectivity (for package installation and git clone)

## Fresh Installation

### Quick Start (Recommended)

Use the bootstrap one-liner — it automatically detects fresh install vs upgrade:

```bash
curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

For unattended installation, select a region with an environment variable:

```bash
curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo env WM1303_REGION=AU915 bash
```

The wizard selects the region and device-wide sync word. All channels initially
remain disabled; configure the channels for your local mesh after signing in.
On upgrades the wizard is skipped and existing settings are preserved.

### Manual Installation

```bash
git clone https://github.com/HansvanMeer/pyMC_WM1303.git
cd pyMC_WM1303
sudo bash install.sh
```

The installation script handles everything automatically. It will take 15–30 minutes depending on your internet speed.

### What the Install Script Does

The installation is divided into clearly labeled steps. All actions produce visible output during execution.

#### Step 1: System Update

- `apt update && apt upgrade`
- Ensures the system is up to date

#### Step 2: Install Build Dependencies

- Installs packages required for compiling C code and building Python packages:
  - `build-essential`, `gcc`, `make`
  - `git`, `rsync`, `jq`, `python3-pip`, `python3-venv`, `python3-dev`
  - `libffi-dev`, `libssl-dev`, `librrd-dev`
  - And other required development packages

#### Step 3: Enable SPI Interface

- Enables SPI via `raspi-config` or `/boot/firmware/config.txt`
- Required for communication with SX1302 and SX1261

#### Step 4: Configure SPI Buffer Size

- Sets `spidev bufsiz=32768` (up from default 4096)
- Configured via **two methods** for maximum compatibility:
  1. `/etc/modprobe.d/spidev.conf` (older kernels)
  2. `/boot/firmware/cmdline.txt` (Debian Trixie and newer)
- Required for the optimized 16 KB SPI burst transfers
- **Reboot required** after installation for this to take effect

#### Step 5: Configure User Permissions

- Detects the non-root service user, or accepts `--user=<name>`
- Validates and installs `/etc/sudoers.d/090_wm1303-<uid>` for that user
- Adds the selected user to hardware groups: `spi`, `i2c`, `gpio`, `dialout`
- Required for GPIO reset, pkt_fwd management, and hardware access

The current service account receives unrestricted passwordless sudo. Treat the
account and authenticated management UI as privileged; do not expose them to
untrusted networks.

#### Step 6: Clone Repositories

Clones the required upstream forks:

```
/opt/pymc_repeater/repos/pyMC_core/       ← dev branch
/opt/pymc_repeater/repos/pyMC_Repeater/   ← dev branch
~/sx1302_hal/                          ← master branch, HAL v2.10
```

#### Step 7: Apply Overlay Files

Copies overlay files from this repository into the cloned repos:

- HAL source files → `sx1302_hal/libloragw/` (including `sx1261_spi.c`)
- HAL packet forwarder files → `sx1302_hal/packet_forwarder/`
- openhop_core files → `pyMC_core/src/openhop_core/`
- pymc_repeater files → `pyMC_Repeater/repeater/`

Complete overlay trees are copied, including new modules and UI assets, while
unrelated upstream files are retained. Published fork repositories are unchanged;
the installed checkouts contain the overlaid file replacements.

#### Step 8: Build HAL and Packet Forwarder

- Compiles `libtools` prerequisites
- Compiles `libloragw.a` (SX1302 HAL library)
- Compiles `lora_pkt_fwd` (packet forwarder binary)
- Compiles and installs `spectral_scan`
- Copies binary to `~/wm1303_pf/`
- Copies configuration files

#### Step 9: Install pyMC Core and Repeater

- Creates Python virtual environment
- Installs pyMC_core (dev) in development mode
- Installs pyMC_Repeater (dev) in development mode
- Installs Python dependencies
- Restores the local editable WM1303 core after the repeater's dependency installation
- Verifies both installed distribution records and imports independently of the working directory or `PYTHONPATH`; upgrades repair missing repeater installations even if source revisions have not changed
- Symlinks the matching system `rrdtool` and `systemd` modules into the venv and checks imports

#### Step 10: Copy Configuration Files

- `config.yaml.template` → `/etc/openhop_repeater/config.yaml`
- `wm1303_ui.json` → `/etc/openhop_repeater/wm1303_ui.json`
- `reset_lgw.sh` and `power_cycle_lgw.sh` → `~/wm1303_pf/`
- GPIO reset and power cycle scripts
- Normalizes legacy field names in `wm1303_ui.json` (`sf`→`spreading_factor`, `bw`→`bandwidth`, `cr`→`coding_rate`) — since v2.1.1

#### Step 11: Install and Enable systemd Service

- Copies `openhop-repeater.service` to `/etc/systemd/system/`
- Installs the root-owned WM1303 update launcher and trusted bootstrap/config copies
- Enables auto-start on boot
- Starts and checks the service, unless hardware changes require a reboot first

#### Step 12: Authentication Setup

- The repeater creates and persists a JWT signing secret when needed
- Complete setup and sign in through the main Console at `/`
- Successful login issues the browser's JWT for authenticated API requests

#### Step 13: Version Tracking

- Writes this integration's `VERSION` to `/etc/openhop_repeater/version`
- This WM1303 version is separate from the underlying OpenHop package version

#### Step 14: NTP Verification

- Checks that the NTP client is properly configured and syncing
- Accurate time is important for packet timestamps and log correlation

## Post-Installation

### Verify Installation

```bash
# Check service status
sudo systemctl status openhop-repeater

# Check version
cat /etc/openhop_repeater/version

# Check pkt_fwd is running
ps aux | grep lora_pkt_fwd

# Check logs
journalctl -u openhop-repeater -f
```

If installation reports that a reboot is required, run `sudo reboot` first.
Noninteractive installs leave rebooting to the operator.

### Access the Web Interface

First open `http://<pi-ip>:8000/` to complete setup and sign in. Then open the
Manager on the same hostname and port:

```
http://<pi-ip>:8000/wm1303.html
```

The WM1303 Manager should show the Status tab with channel information. Configure
and enable the channels you need; the region preset alone does not enable RX/TX.
If your session expires, sign in again through the Console. Port 8000 is the
default; use `web.port` from your configuration if changed.

### Verify Channel E

In the Status tab, verify that Channel E appears with its own RSSI/SNR/noise floor values. In the Channels tab, verify that Channel E (SX1261) has a separate configuration section.

### Verify Bridge Configuration

Check that `bridge_conf.json` was generated correctly:

```bash
# Verify unused IF channels are disabled
sudo cat ~/wm1303_pf/bridge_conf.json | python3 -c "
import sys, json
c = json.load(sys.stdin)
for i in range(8):
    k = f'chan_multiSF_{i}'
    if k in c.get('SX130x_conf', {}):
        ch = c['SX130x_conf'][k]
        print(f'{k}: enable={ch.get(\"enable\")}, if={ch.get(\"if\", 0)}')
"
```

Expected: only actively configured channels should show `enable: true`.

## Upgrade

Companion databases from older versions need an explicit ownership confirmation
before their legacy state can be migrated. Back up the database and review
[companion storage ownership](configuration.md#companion-storage-ownership)
before restarting an existing installation. Unclaimed legacy state is preserved;
the affected companion remains inactive instead of starting with another
identity's data or an apparently empty contact list.

### One-Liner Bootstrap (Recommended)

The bootstrap script handles both fresh install and upgrade automatically:

```bash
curl -fsSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
```

`bootstrap.sh` preserves local integration-checkout changes in a Git stash before
resetting it to `origin/main`. Existing checkout origins are checked against the
documented HansvanMeer repositories; equivalent HTTPS and SSH URLs are accepted.
An unexpected origin aborts before that checkout is changed. The scripts do not
rewrite remotes; inspect the repository and choose the intended source before retrying.

Use the WM1303-aware Console updater, this bootstrap, or `upgrade.sh`, not a
generic OpenHop pip updater.
The WM1303 update must also reapply overlays, restore the local core package,
rebuild changed HAL code, and update the service/configuration. A generic package
update does not perform those steps. Console package-channel choices do not change
the script's pinned HAL `master` and core/repeater `dev` branches.

### Console Update

The WM1303-aware update action launches `/usr/local/sbin/wm1303-upgrade start`
through passwordless sudo. The launcher starts a separate `wm1303-update.service`
job, so updating can stop and restart the repeater without killing its updater.
The Console connection will temporarily drop and retry during the build. Reopen it after the restart if needed to
read the persisted job status and log.

The root-owned configuration in `/usr/local/lib/pymc-wm1303/updater.conf` captures
the installed service user, integration checkout, and its GitHub repository.
Updates follow that repository's `main` branch, including an explicitly installed
fork named `pyMC_WM1303`; they do not silently switch forks or rewrite remotes.
Detached updates fetch that fork over HTTPS, so an SSH checkout does not require
an interactive SSH agent. Public forks work without credentials. Private forks
require unattended HTTPS credentials configured for the installed service user;
the detached job cannot prompt for a password.
The HAL/core/repeater dependency forks remain those listed in
[Repositories](repositories.md). No channel, URL, path, or shell command is
accepted from the GUI.

To inspect an update over SSH without starting another one:

```bash
sudo /usr/local/sbin/wm1303-upgrade status
sudo /usr/local/sbin/wm1303-upgrade log
```

`status` reports the systemd job state and installed repository; `log` returns the
last 500 lines of `/var/log/wm1303-update.log`. The log is retained until the next
update. A second start is refused while a job is running. Older installations
without this launcher must first use the bootstrap/manual upgrade once.

The standalone bootstrap still defaults to `HansvanMeer/pyMC_WM1303`. For an
intentional fork, set `WM1303_REPO_URL` to its GitHub HTTPS or SSH URL; its basename
must be `pyMC_WM1303` and the existing checkout origin must match. The GUI launcher
supplies the saved fork automatically.

> **Note:** `upgrade_bootstrap.sh` has been removed and superseded by `bootstrap.sh`.

### Manual Upgrade (From Existing Clone)

```bash
cd ~/pyMC_WM1303
git pull origin main
sudo bash upgrade.sh
```

### Force HAL Rebuild

If HAL overlay files changed (C code modifications):

```bash
sudo bash upgrade.sh --force-rebuild
```

> **Important:** After every upgrade, perform a **hard browser refresh** (Ctrl+Shift+R or Ctrl+F5) to load updated UI assets.

A reboot is recommended after upgrades that change SPI configuration.

### What the Upgrade Script Does

1. Checks dependency origins before stopping the service, then backs up configuration and SQLite data
2. Updates the three dependency repositories; refresh this integration checkout first, as above
3. Detects HAL overlay checksum changes **before** copying overlays
4. Applies complete overlay trees and rebuilds changed HAL code (or when forced)
5. Updates Python dependencies and restores the local WM1303 core
6. Adds missing configuration keys, normalizes legacy channel names, and preserves user values
7. Updates `/etc/openhop_repeater/version`, restarts the service, and checks startup

`--skip-pull` deliberately uses the existing local dependency checkouts without
fetching or checking their origins. `--force-config` overwrites configuration with
templates; do not use it when you intend to preserve settings. Backups are placed
in a unique directory under the selected user's `~/backups/`.

### Configuration Preservation

During upgrade:

- **User settings are preserved** — the merge adds new keys without overwriting existing values
- **Cache limits and TX delays are preserved**, including nonzero custom values
- **Deep merge** handles nested dictionaries (e.g., new Channel E sub-keys)
- **bridge_conf.json is regenerated** on service restart from the merged SSOT

## Troubleshooting

### Service fails to start

```bash
journalctl -u openhop-repeater -n 50
```
Common causes:

- Missing sudo permissions → check `/etc/sudoers.d/090_wm1303-<service-user-uid>`
- SPI not enabled → check `/boot/firmware/config.txt` for `dtparam=spi=on`
- Missing Python dependencies → reinstall in venv

### pkt_fwd crashes on startup

- Check SPI device availability: `ls -la /dev/spidev0.*`
- Check GPIO permissions
- Verify HAL compilation was successful
- Check `bridge_conf.json` for valid configuration

### No RX packets

- Verify antenna is connected
- Check channel frequency matches your MeshCore network
- Check pkt_fwd stdout for RX activity
- Verify IF chain configuration in `bridge_conf.json`

### spidev bufsiz not applied
After installation, verify:

```bash
cat /sys/module/spidev/parameters/bufsiz
```
Should show `32768`. If not, reboot the Pi.

## Directory Structure (After Installation)

```
/opt/pymc_repeater/                    # Main installation
├── repos/
│   ├── pyMC_core/                     # Core library (dev)
│   └── pyMC_Repeater/                 # Repeater app (dev)
│
/etc/openhop_repeater/                 # Configuration (legacy files are migrated)
├── config.yaml
├── wm1303_ui.json
└── version
│
~/
├── sx1302_hal/                        # HAL source (compiled)
├── wm1303_pf/                         # Packet forwarder runtime
│   ├── lora_pkt_fwd
│   ├── bridge_conf.json
│   ├── global_conf.json
│   ├── reset_lgw.sh
│   └── power_cycle_lgw.sh
└── pyMC_WM1303/                       # This repository (if cloned here)
│
/etc/systemd/system/
└── openhop-repeater.service
│
/etc/sudoers.d/
└── 090_wm1303-<uid>                   # Selected service user's sudo rule
│
/usr/local/sbin/wm1303-upgrade         # Root-owned fixed start/status/log launcher
/usr/local/lib/pymc-wm1303/            # Root-owned bootstrap.sh and updater.conf
/var/log/wm1303-update.log             # Persistent latest-update log (root only)
│
/etc/modprobe.d/
└── spidev.conf                        # spidev bufsiz=32768
```

## Related Documents

- [`hardware.md`](./hardware.md) — Hardware requirements
- [`configuration.md`](./configuration.md) — Configuration files
- [`repositories.md`](./repositories.md) — Repository structure
- [`architecture.md`](./architecture.md) — System architecture
