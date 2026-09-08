# TX Queue System

> Per-channel TX queue architecture, scheduling, and hold behavior

## Overview

The WM1303 system uses **per-channel TX queues** managed by a `GlobalTXScheduler`. Each active channel (A–E) has its own queue instance that handles buffering, gating, and fair transmission scheduling.

Since v2.1.0, the TX pipeline includes mandatory hardware CAD (Channel Activity Detection) in the packet forwarder's C layer, with all random TX delays eliminated. Since v2.1.1, the Python pre-TX software check has been removed entirely — the C-level CAD+LBT is now the sole channel assessment mechanism. CAD retry delays have been optimized from worst-case 3100 ms to 1050 ms.

## Architecture

```
Bridge Engine → TX batch window (2s)
    → Per-channel TX Queue instances
        ├── Channel A Queue
        ├── Channel B Queue
        ├── Channel C Queue
        ├── Channel D Queue
        └── Channel E Queue
    → GlobalTXScheduler
        → Fair round-robin scheduling
        → Per-queue gating (TTL, overflow)
        → PULL_RESP (UDP :1730) → Packet Forwarder (C)
            → Mandatory CAD scan (SX1261)
            → Optional LBT check (if enabled per channel)
            → IMMEDIATE TX → Radio
```

## Queue Parameters

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max queue depth | 15 packets | Per channel |
| TTL per packet | 5 seconds | Packet expires if not sent within this time |
| TX batch window | 2 seconds | Group bridge sends for concurrent queuing |
| Queue depth hold | 100 ms | Brief dedup window when 1 packet pending |
| TX delay factor | 0.0 (default) | Random pre-TX jitter; set to 0 since v2.1.0 |

## TX Pipeline (v2.1.1)

The full TX path from enqueue to air:

1. **Bridge Engine** enqueues packet to the appropriate channel queue (origin channel first since v2.1.1)
2. **TX batch window** (2s) groups concurrent bridge sends
3. **GlobalTXScheduler** picks the next packet via round-robin
4. **Python gating checks**: TTL expiry, queue overflow
5. **PULL_RESP** sends packet to the packet forwarder via UDP (:1730)
6. **Packet forwarder (C)**: mandatory CAD scan on SX1261 → optional LBT → IMMEDIATE TX
7. **Radio TX** on SX1302 (Channels A–D) or SX1261 (Channel E)

> **v2.1.1 change:** The Python pre-TX software check (Laag 1) has been removed (`lbt_check=None`). Previously, a Python-level check assessed channel availability using cached spectral/noise data before sending PULL_RESP. With the C-level hardware CAD+LBT fully operational since v2.1.0, this was redundant and added up to 5 seconds of worst-case delay. The TX pipeline is now a single layer (C only).

| Aspect | Before (v2.1.0) | After (v2.1.1) |
|--------|-----------------|----------------|
| Pre-TX checks | 2 layers (Python + C) | 1 layer (C only) |
| Python retry delays | [0, 0.5s, 1.0s, 1.5s] + 2.0s force | None |
| Worst-case total delay | ~8.1s (Python 5.0s + C 3.1s) | **~1.05s** (C only) |

## Origin-Channel-First TX Priority (v2.1.1)

When the Bridge Engine forwards a received packet to multiple target channels via bridge rules, the **originating channel gets TX priority** — it is enqueued and transmitted first.

Example: packet received on Channel E → forwarded to A, B, C, E via bridge rules → Channel E is sent **first**, then A, B, C in their normal order.

This reduces repeat latency for the node that originally sent the packet. The `origin_channel` parameter is tracked through `bridge_engine.py` (`inject_packet()` → `_forward_by_rules()`) and `main.py` (`_bridge_repeater_handler()`). The GlobalTXScheduler's round-robin fairness is unaffected — origin priority only controls the enqueue order within a single bridge forwarding event.

### Python → C Overhead

The handoff from Python to C has been optimized from **~62 ms** to **~8 ms** queue wait time, achieved by:

- JIT thread poll interval reduced from 10 ms to **1 ms**
- Redundant hardware status checks eliminated
- Mutex locks consolidated

## Scheduling

### Fair Round-Robin

The `GlobalTXScheduler` uses a **rotating start index** to ensure fair access across channels:

1. On each scheduling cycle, iterate through all channel queues
2. Start from a different channel each cycle (rotating index)
3. For each queue, check if the head packet can be sent (gating checks)
4. If yes, dequeue and send via PULL_RESP
5. Track TX statistics per channel

This prevents one busy channel from starving others.

### Gating Checks

Gating is split between the Python TX queue and the C packet forwarder:

#### Python-Level (TX Queue)

| # | Check | Action on Fail |
|---|-------|----------------|
| 1 | **TTL** | Packet expired → discard |
| 2 | **Queue overflow** | Queue full → discard oldest |

#### C-Level (Packet Forwarder)

| # | Check | Action on Fail |
|---|-------|----------------|
| 3 | **CAD** (mandatory) | LoRa preamble detected → retry with fixed delays (50→100→200→300→400 ms, up to 5×) → force-send |
| 4 | **LBT** (optional, per channel) | RSSI above threshold → delay TX |

> **v2.1.1 change:** CAD retry delays changed from exponential backoff (100→200→400→800→1600 ms, worst-case 3100 ms) to fixed values (50→100→200→300→400 ms, worst-case 1050 ms) — a 66% reduction.
See [`lbt_cad.md`](./lbt_cad.md) for full details on CAD and LBT behavior.

### RF-Chain Guard

The shared RF-chain guard prevents TX on different channels from overlapping:

| Version | Guard | Timing |
|---------|-------|--------|
| Before v2.1.0 | Static | 50 ms |
| v2.1.0+ | Dynamic | airtime + 250 ms |

The dynamic guard calculates the actual packet airtime and adds a 250 ms margin, preventing both premature and unnecessarily long guard periods.

## TX Delay Elimination

Since v2.1.0, mandatory CAD handles collision avoidance, replacing all random TX delays:

| Parameter | Before | After (v2.1.0) |
|-----------|--------|----------------|
| `tx_delay_factor` | 1.0 | **0.0** |
| `direct_tx_delay_factor` | 0.5 | **0.0** |
| Per-rule `tx_delay_ms` | variable | **0 ms** |
| Python airtime guard | duplicated | **removed** |

Users can still increase `tx_delay_factor` in **Adv. Config → TX Queue Management** for specific use cases.

## TX Batch Window

When the Bridge Engine forwards a packet to multiple target channels, it uses a **2-second batch window**:

1. First target channel enqueue triggers the batch timer
2. Additional target channels are enqueued within the window
3. After 2 seconds, all queued packets are eligible for scheduling
4. This groups related forwards for efficient multi-channel TX

## TX Hold Behavior

| Hold Type | Status | Duration | Purpose |
|-----------|--------|----------|---------|
| **CAD scan** | ✅ Mandatory | 37–56 ms | Hardware preamble detection (C layer) |
| **CAD retry backoff** | ✅ Active (on detection) | 50–400 ms (fixed) | Wait for channel to clear (worst-case 1050 ms total) |
| TX batch window | ✅ Active | 2 seconds | Group concurrent bridge sends |
| Queue depth hold | ✅ Active | 100 ms (1 pkt) to 2 s (batch) | Brief dedup window |
| Noise floor hold | ❌ Removed | — | Was: pause TX for noise measurement |

The noise floor hold was removed because it violated the "TX ASAP" design principle — the SX1261 spectral scan runs on a separate SPI bus and the NoiseFloorMonitor now waits for TX-free windows with retry logic.

## Noise Floor Integration

Each TX queue receives per-channel noise floor values from the NoiseFloorMonitor:

- Values stored in a rolling buffer (20 samples)
- Used for LBT threshold comparison (when LBT is enabled)
- Updated every 30 seconds (monitor interval)
- **Does NOT pause the queue** — values are fed asynchronously

## TX Statistics

Per-channel TX statistics tracked:

| Metric | Description |
|--------|-------------|
| `tx_sent` | Packets successfully sent |
| `tx_dropped_ttl` | Packets expired before send |
| `tx_dropped_overflow` | Packets dropped due to full queue |
| `tx_lbt_blocked` | TX attempts blocked by LBT |
| `tx_cad_detected` | CAD detections (retried, then force-sent) |
| `tx_duty_cycle` | Cumulative TX duty cycle (sum across channels sharing an RF chain) |

### Duty Cycle Calculation

Since Channels A–D share the SX1250 RF chain, the TX duty cycle is the **sum** of all individual channel duty cycles.

### Duty-cycle enforcement

The repeater binds its live airtime manager to the global TX scheduler before
starting forwarding. All enabled channels, including E/F and room-server
transmissions, share the configured rolling 60-second budget. For example,
10% permits 6,000 ms of airtime in that window. Airtime is calculated from each
destination channel's SF, bandwidth, coding rate and preamble.

When the budget is exhausted, the scheduler waits within the packet's original
TTL. Expired or cancelled requests never reach the radio. Live changes to the
limit or enforcement switch apply without clearing transmission history.
Successful transmissions update the same counter displayed by the Console;
dispatcher traffic is not counted twice. Missing acknowledgements are charged
conservatively, while confirmed LBT/JIT refusals consume no airtime. The budget
is held in memory and resets with the daemon. It is shared conservatively even
when enabled channels use different sub-bands.

This duty budget is separate from the HAL's CAD/LBT checks and RF busy guard.
Scheduler statistics expose `duty_cycle_waits` for packets delayed by the budget.

## Packet Activity Recording

TX events (including CAD results, LBT checks, and packet delivery) are recorded by the `_packet_activity_recorder` in `repeater.db`:

| Table | Data |
|-------|------|
| `packet_activity` | Per-packet TX events with timing and channel info |
| `cad_events` | Per-channel CAD clear/detected counts |
| `dedup_events` | Deduplication statistics |

All data is automatically cleaned up after **8 days** (retention policy).

## Socket Recovery

The TX path includes automatic socket recovery:

- If UDP send fails, the socket is recreated
- Retry logic ensures packets are not lost on transient failures
- Socket errors are logged for diagnostics

## Channel E TX

Channel E TX follows the same queue model but uses the SX1261 radio path:

- Supports sub-125 kHz bandwidths (62.5 kHz)
- TX power configurable per channel
- CAD scan timing is longer (~47–56 ms vs ~37–43 ms) due to narrower bandwidth
- Same mandatory CAD + optional LBT flow as Channels A–D

## Design Principles

1. **TX ASAP** — Zero random delays; CAD overhead is deterministic and minimal (37–56 ms)
2. **RX priority** — CAD + TX duration minimized to restore RX as quickly as possible
3. **Fairness** — Round-robin scheduling prevents channel starvation
4. **Safety** — TTL and overflow prevent unbounded queue growth
5. **Deterministic collision avoidance** — Hardware CAD replaces random delays

## Related Documents

- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`radio.md`](./radio.md) — Radio architecture
- [`architecture.md`](./architecture.md) — System architecture
- [`software.md`](./software.md) — Software components
- [`configuration.md`](./configuration.md) — Configuration reference
