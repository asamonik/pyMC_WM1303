# WM1303 Manager UI

> Web-based management interface for the WM1303 concentrator

## Overview

The WM1303 Manager is a single-page web application (`wm1303.html`) that provides complete management of the WM1303 concentrator system. It is accessible at:

```
http://<pi-ip>:8000/wm1303.html
```

The UI communicates with the backend via the [WM1303 REST API](./api.md) and receives real-time updates via WebSocket.

## Console access and analytics

The Console Policies view and the Manager's **Access** tab include MeshCore
companion password controls. Changing the administrator password also changes
the Console login password and returns you to sign-in. The guest password can
be changed or explicitly disabled. Both forms require the current administrator
password; saved credentials are never displayed. MeshCore passwords fit at most
15 UTF-8 bytes without NUL; a new administrator password needs at least eight
characters and must differ from the guest password. Successful saves refresh
the running repeater ACL; the response reports if a restart is needed.

Neighbor Links uses advertised node names in the table, selected-link heading,
search, sorting, and chart tooltips. Nodes without an advertised name retain
their peer-key label, and history requests keep using the original peer key.

The Console Policies editor supports live packet policies and named channel-hash
and public-key groups. Enforcement is disabled by default. When enabled, rules
run in order against the original received packet and its RF metadata. The first
matching enabled rule selects `allow`, `drop`, or `log_only`; otherwise the
configured default action applies. A drop prevents application handling and
forwarding through the repeater or bridge. Raw companion "Heard Repeats" remains
an RF monitoring feed before filtering; packet policies do not hide that feed.
The Manager's Bridge tab still selects forwarding destinations.

Validate checks a draft without activating it. Save validates the complete policy
and its group references, writes it atomically, and replaces the running policy
without a restart. The default `policy.yaml` is stored alongside the active
repeater configuration; `policy.policy_file` can select another location. A
malformed existing policy prevents startup and cannot be overwritten through a
save that silently substitutes defaults. Failed saves retain the previous file
and running policy.

LBT Diagnosis uses recorded WM1303 backend TX operations with confirmed CAD/LBT
check results. Its attempt count is `1 + cad_retries + lbt_retries` for the
enabled checks within each operation; scheduler retries are tracked separately. Missing
ACKs, unavailable results, scan failures, or disabled checks do not become
zero-retry samples. Older packet records lack these measurements and are
excluded. Retry metrics display `N/A` when the selected window has no usable
measurements. TX results do not measure reception loss, so packet-loss
correlation remains unavailable. Channel LBT readings are also available in
Manager Spectrum.

The shared install/upgrade overlay helper adapts the shipped Console modules
in both the checkout and installed Python package. It verifies known expressions
before replacing any module and leaves current modules unchanged. It also
migrates the older disabled Policies editor and missing-telemetry explanation.
An unrecognized upstream build stops deployment with an explicit error so it
can be reviewed. Hard-refresh the browser after upgrading.

## Tabs

The UI has **5 tabs**: Status, Channels, Bridge, Spectrum, and Adv. Config.

> **v2.1.0 change:** The **Config** tab has been removed. TX Delay Factor has moved to Adv. Config → TX Queue Management.

### Status Tab

Real-time overview of system health and channel performance:

| Section | Contents |
|---------|----------|
| System Info | Hostname, CPU temp, memory, disk, uptime, kernel version |
| Active Channels | Count of active channels (up to 5 when Channel E is active) |
| Per-channel status | Frequency, BW, SF, RX/TX counts, RSSI, SNR, noise floor, duty cycle |
| Channel E status | Same metrics for the SX1261 channel |
| Signal Quality chart | Real-time per-channel RSSI and SNR |
| Packet Activity chart | Bar chart of per-channel RX/TX counts (Channel E in orange) |

### Channels Tab

Per-channel configuration interface:

#### IF Channels (A–D)
- Frequency (MHz)
- Bandwidth (kHz) — max 125 kHz
- Spreading Factor (SF7–SF12)
- Coding Rate (4/5–4/8)
- Preamble length
- TX Power (dBm)
- LBT enable + threshold (optional, per channel)
- Active toggle
- Channel name (display alias only)

> **v2.1.0 change:** The RF0/RF1 information block has been removed from the Channels tab. CAD is now mandatory and always active — there is no CAD toggle in the UI.

#### SX1261 Channel (E)
Same parameters as above, plus:
- RX Boost toggle
- Bandwidth limited to 62.5 kHz (since v2.0.5)
- Separate configuration section for clarity

All channels use a consistent grid layout (aligned since v2.0.0).

### Bridge Tab

Bridge rules management:

- List of all bridge rules
- Source → target channel mapping
- Packet type filter per rule (all, advert, text, position, path)
- Enable/disable toggle per rule
- Add / edit / delete rules
- Rules read from and written to `wm1303_ui.json` SSOT

### Spectrum Tab

Spectral analysis and channel monitoring charts:

| Chart | Description |
|-------|-------------|
| **RSSI History** | Per-channel RSSI values over time |
| **SNR History** | Per-channel SNR values over time |
| **Noise Floor** | Per-channel noise floor from spectral scan |
| **Channel Activity** | Combined TX counts and RX Origin counts per channel (merged chart since v2.1.1) |
| **CAD Activity** | Per-channel CAD results: **Clear** (green) and **Detected** (red) |
| **LBT History** | Per-channel LBT events with frequency and RSSI values |
| **Dedup Chart** | Deduplication event visualization |

Channel E is displayed in **orange** across all charts (since v2.0.1).

Extended color palette ensures all 5 channels are visually distinct.

#### v2.1.0 Spectrum Tab Changes

- **Removed 6 stat cards**: SPI Path, SX1261 Role, SX1261 Status, Active Channels, Spectral Scan Status, Noise Floor
- **Removed header/subtitle**: "📊 Channel E Spectrum & Signal Quality" header removed
- **CAD Activity chart simplified**: shows only Clear and Detected (removed SW/HW/Skipped distinction)
- **Info text added**: explanatory note below CAD chart explains that "Detected" means TX was force-sent after all CAD retries
- **LBT History chart fixed**: frequency and RSSI data now correctly captured (was previously stored as freq=0, rssi=None)

#### v2.1.1 Spectrum Tab Changes

- **Channel Activity chart merged**: TX Activity per Channel renamed to **Channel Activity**; now shows both TX counts (light colors, solid border) and RX Origin counts (dark semi-transparent, dashed border) on the same timeline
- **Signal History default changed**: Time range selector defaults to **1h** instead of 24h for a more responsive initial view

### Adv. Config Tab

Advanced system configuration:

| Section | Contents |
|---------|----------|
| GPIO Pins | Reset, power, SX1261 NSS, AD5338R reset pin assignments |
| RF Chains | RF0/RF1 center frequencies |
| IF Chains | Per-IF-chain enable, frequency offset, radio assignment |
| TX Queue Management | TX delay factor, direct TX delay factor (defaults: 0.0 since v2.1.0) |
| SPI Settings | Speed, burst size (informational) |
| System Actions | Restart service, restart pkt_fwd, hardware reset |

## UI Behavior Notes

### v2.1.1 UI Bug Fixes

- **SF? display fix**: Added `normCh()` JavaScript function that normalizes legacy field names (`sf`→`spreading_factor`, `bw`→`bandwidth`, `cr`→`coding_rate`) on every data load. Prevents "SF?" display in channel configuration cards when the underlying JSON uses legacy field names.

### Channel Names Are Aliases
Channel names shown in the UI are **display aliases only**. Internal logic and API responses use stable channel identifiers (`channel_a` through `channel_e`). Users can rename channels freely without affecting system operation.

### CAD Is Mandatory (v2.1.0)
CAD is always active on every TX — there is no user toggle for CAD. The CAD Activity chart in the Spectrum tab shows the results of every mandatory CAD scan.

### LBT Controls
LBT can be independently enabled/disabled per channel in the Channels tab. When enabled, an RSSI threshold slider appears. LBT is optional and currently recommended to keep disabled (work-in-progress).

### Configuration Persistence
- UI changes are saved to `wm1303_ui.json` (SSOT) via the API
- Changes take effect within **5 seconds** (cache TTL auto-reload)
- No service restart required for most settings
- Bridge rule changes take effect immediately

### Frequency Display
All frequencies are displayed in **MHz** throughout the UI. Internal storage uses Hz.

### Bandwidth Display
All bandwidths are displayed in **kHz** throughout the UI. Internal storage uses Hz.

### Channel E Bandwidth
Since v2.0.5, the Channel E bandwidth dropdown is limited to **62.5 kHz** only. The 125/250/500 kHz options were removed because the SX1261 RX path through the concentrator HAL only supports sub-125 kHz bandwidths.

### RX Boost
The RX Boost toggle (Channel E) enables enhanced RX sensitivity on the SX1261. It is positioned before the Active toggle for better workflow (since v2.0.0).

### Hard Refresh After Upgrade
After every upgrade, perform a **hard browser refresh** (Ctrl+Shift+R or Ctrl+F5) to ensure the updated UI assets are loaded.

## Technology Stack

| Component | Technology |
|-----------|------------|
| UI framework | Vanilla HTML/JavaScript/CSS |
| Charts | Chart.js |
| Data transport | REST API + WebSocket |
| Auto-refresh | WebSocket-driven (no polling) |

## Screenshots

Screenshots of the interface are available in the `/screenshots/` directory:

| File | Shows |
|------|---------|
| `status.jpg` | Status tab with channel overview |
| `channels-1.jpg` | Channel configuration (IF channels) |
| `channels-2.jpg` | Channel configuration (SX1261 channel) |
| `bridge-rules.jpg` | Bridge rules management |
| `spectrum-1.jpg` | Spectrum tab with charts |
| `dedup.jpg` | Deduplication event chart |

## Related Documents

- [`api.md`](./api.md) — REST API reference
- [`configuration.md`](./configuration.md) — Configuration files
- [`lbt_cad.md`](./lbt_cad.md) — LBT/CAD behavior and UI interaction
- [`tx_queue.md`](./tx_queue.md) — TX queue system
