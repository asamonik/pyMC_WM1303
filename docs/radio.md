# Radio Architecture

> Detailed description of the WM1303 radio topology, channel model, and RF behavior

## Radio Topology

The WM1303 Pi HAT contains the following radio components:

| Chip | Role | SPI Device | Capabilities |
|------|------|------------|---------------|
| **SX1302** | Baseband processor | `/dev/spidev0.0` | 8 IF demodulators, AGC, timestamp engine |
| **SX1250_0** (RF0) | Front-end radio 0 | via SX1302 | TX + RX, center freq configurable |
| **SX1250_1** (RF1) | Front-end radio 1 | via SX1302 | RX only, center freq configurable |
| **SX1261** | Companion radio | `/dev/spidev0.1` | CAD, spectral scan, LBT, Channel E reception |

## Six-Channel Model

The system supports four concentrator channels plus the E and F receivers:

| Channel | Radio Chain | IF Chain | Backend | Supported BW |
|---------|------------|----------|---------|--------|
| **Channel A** | SX1250 RF0 | chan_multiSF_0 | VirtualLoRaRadio | 125 kHz |
| **Channel B** | SX1250 RF0 | chan_multiSF_1 | VirtualLoRaRadio | 125 kHz |
| **Channel C** | SX1250 RF0 | chan_multiSF_2 | VirtualLoRaRadio | 125 kHz |
| **Channel D** | SX1250 RF0 | chan_multiSF_3 | VirtualLoRaRadio | 125 kHz |
| **Channel E** | SX1261 | Dedicated RX | Channel E Bridge | 62.5 / 125 / 250 / 500 kHz |
| **Channel F** | SX1250 RF0 + SX1302 | chan_Lora_std | Channel F Bridge | 125 / 250 / 500 kHz |

> **Design guideline:** Maximum 4 channels recommended. Fewer active channels = more stable operation.

### Channel A–D: Concentrator-Backed

Channels A–D use the SX1302 concentrator's multi-channel demodulator system:

- Each channel maps to one or more IF chain demodulator slots
- All channels share the SX1250 RF front-ends (RF0 for TX+RX, RF1 for RX)
- IF chain offsets are calculated from the RF chain center frequency
- Maximum bandwidth: 125 kHz (SX1302 IF chain limitation)
- Each channel has its own frequency and spreading factor, with fixed 125 kHz bandwidth

### Channel E: SX1261-Backed

Channel E uses the SX1261 companion chip directly:

- Independent receive path; transmission still uses the concentrator's RF0 TX path
- This HAL supports SF7–SF12 for Channel E reception
- Supports **sub-125 kHz bandwidths** (e.g., 62.5 kHz) — unique capability
- Also performs **mandatory CAD scans** before every TX (all channels), spectral scanning, and optional LBT measurements
- CAD timing is longer on Channel E (~47–56 ms vs ~37–43 ms for Channels A–D) due to narrower bandwidth requiring more symbols for preamble detection
- See [`channel_e_sx1261.md`](./channel_e_sx1261.md) for full details

## RF Chain Configuration

The SX1302 has two RF chains (radio front-ends):

| RF Chain | Radio | Capabilities | Typical Use |
|----------|-------|-------------|-------------|
| RF0 | SX1250_0 | TX + RX | Primary TX chain + RX |
| RF1 | SX1250_1 | RX only | Additional RX coverage |

Each RF chain has a center frequency. The IF demodulators use offsets from
that center. The generator fits active A-D and F within a shared 1.6 MHz
receive window, allowing half the channel bandwidth and a 7.5 kHz margin at
each edge. E's independent receiver does not constrain that center.

The current HAL's LBT implementation supports only 62.5, 125 and 250 kHz;
an enabled 500 kHz TX channel cannot be combined with HAL LBT on any channel.
LBT channel lists are limited to 16 entries. The parser and HAL reject larger
lists, and the HAL validates all channel bandwidth/scan settings before
publishing a new SX1261 configuration.

TRACE forwarding uses packet-local bridge origin metadata with one permanent
sender. Concurrent radio receptions cannot replace the shared helper's sender
or leave a stale channel selected for later packets. Unmarked router packets
retain their normal routing path.

### Bridge Configuration Generation

The `_generate_bridge_conf()` function reads channel definitions from `wm1303_ui.json` and calculates:

1. RF chain center frequency (auto-calculated from active channels or manually set)
2. IF chain offsets for each channel relative to the RF center
3. IF chain enable/disable state — **unused slots are disabled** (critical fix in v2.0.5)

> **Important (v2.0.5):** Unused IF demodulator slots (`chan_multiSF_1` through `_7`) must have `enable: false`. A bug in earlier versions set these to `enable: true`, causing phantom packets that flooded the bridge engine.

## VirtualLoRaRadio

`VirtualLoRaRadio` is the per-channel radio abstraction used by the bridge/repeater stack:

- Implements the standard `LoRaRadio` interface (send, receive, sleep, rssi, snr)
- Each instance represents one logical channel
- Contains an async RX queue with thread-safe enqueue
- Provides per-channel noise floor estimation from RSSI history
- TX operations delegate to `WM1303Backend.send()` which handles the UDP PULL_RESP path
- One VirtualLoRaRadio instance per active channel (A–D)

## MeshCore Sync Word

MeshCore uses a specific LoRa sync word that differs from LoRaWAN. This is configured in the HAL/forwarder layer and ensures:

- MeshCore packets are received correctly
- LoRaWAN traffic is filtered out at the radio level
- No cross-protocol interference

## LoRaWAN Compatibility

The WM1303 system is **not compatible with LoRaWAN** in its current configuration:

- Different sync word
- Different packet format
- No LoRaWAN MAC layer
- The concentrator hardware is LoRaWAN-capable but repurposed for MeshCore

## SPI Bus Architecture

The radio components use separate SPI buses:

| Bus | Device | Clock Speed | Used For |
|-----|--------|-------------|----------|
| `/dev/spidev0.0` | SX1302 + SX1250s | Standard | RX data, TX commands, concentrator control |
| `/dev/spidev0.1` | SX1261 | ~2 MHz | CAD scans, spectral scan, LBT, Channel E RX, PRAM upload |

The ~2 MHz SPI clock for the SX1261 provides sufficient bandwidth for the current implementation:
- Bulk PRAM write (1546 bytes) completes in ~42 ms
- CAD setup and result reads are single-register operations
- The separate bus means SX1261 operations (CAD, spectral scan) do not block SX1302 RX data transfer

## Noise Floor Monitoring

Noise floor values are maintained **per channel** using **direct channel IDs** (not frequencies). This is critical when multiple channels share the same frequency but use different spreading factors.

### Two-Level Timing

| Layer | Interval | What |
|-------|----------|------|
| HAL spectral scan thread | ~1 second (`pace_s=1`) | SX1261 scans during TX-free windows |
| Python NoiseFloorMonitor | 30 seconds | Harvests results, calculates per-channel noise floor |

### Behavior Rules

- Noise floor monitoring **does NOT pause TX queues** (old hold behavior removed)
- Monitoring waits for TX-free windows with retry logic
- Results are persisted to database for API/UI use
- Per-channel values feed into LBT threshold decisions

### Fallback Chain

When spectral scan data is unavailable, the system falls back through:

1. **Spectral scan data** (primary — from SX1261)
2. **SX1261 RSSI point measurement** (secondary)
3. **RX packet-based estimation** (last resort)

> **Troubleshooting:** If all channels show `-120.0 dBm`, spectral scan data is not being collected. Check SX1261 availability, scan output files, and runtime permissions.

## CRC Errors

RX CRC errors indicate corrupted packets. Common causes:

- Interference from other transmitters
- Multi-path reflections
- Near-field overload (transmitter too close)
- AGC issues (see FEM/LNA/AGC section in hardware docs)

CRC errors are counted per channel and visible in the Status tab.

## TX/RX Echo

Echo detection prevents the system from processing its own transmitted packets:

### Self-Echo (Backend Level)
When a packet is transmitted, its hash is stored for 30 seconds. If the same hash appears on RX (own TX bounced back via antenna), it is discarded.

### Multi-Demod Dedup (Backend Level)
The SX1302 can demodulate the same packet via multiple IF chains simultaneously. A 2-second cache prevents duplicate dispatch.

### Cross-Channel Dedup (Bridge Level)
The bridge engine maintains a 15-second hash cache to catch the same packet received on different channels.

## Related Documents

- [`architecture.md`](./architecture.md) — System architecture
- [`channel_e_sx1261.md`](./channel_e_sx1261.md) — Channel E details
- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`tx_queue.md`](./tx_queue.md) — TX queue system
- [`hardware.md`](./hardware.md) — Hardware details
- [`configuration.md`](./configuration.md) — Configuration reference
