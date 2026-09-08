# Configuration

> Configuration files, SSOT model, and runtime configuration management

## Overview

The WM1303 system uses multiple configuration files with a **Single Source of Truth (SSOT)** model centered on `wm1303_ui.json`.

## Configuration Files

| File | Location (installed) | Role | Editable By |
|------|---------------------|------|-------------|
| `wm1303_ui.json` | `/etc/openhop_repeater/wm1303_ui.json` | **SSOT** — desired WM1303 settings | WM1303 Manager UI, authenticated API, manual |
| `config.yaml` | `/etc/openhop_repeater/config.yaml` | Repeater-level config (radio type, identity) | Repeater UI, manual |
| `bridge_conf.json` | `~/wm1303_pf/bridge_conf.json` | **Generated** — HAL/forwarder config | Auto-generated on service start |
| `global_conf.json` | `~/wm1303_pf/global_conf.json` | **Copy** of bridge_conf.json | Auto-generated |
| `pymc_wm1303_bridge_conf.json` | `/tmp/pymc_wm1303_bridge_conf.json` | Active forwarder configuration snapshot | Backend at startup |
| `version` | `/etc/openhop_repeater/version` | Deployed WM1303 version number | Install/upgrade scripts |

`~` means the installed service user's home. Legacy `/etc/pymc_repeater` data is
migrated on install/upgrade; use the canonical `openhop_repeater` paths for new settings.

## SSOT Model — `wm1303_ui.json`

`wm1303_ui.json` is the **authoritative source** for all WM1303-specific configuration:

### What it contains

| Section | Contents |
|---------|----------|
| `channels` | Per-channel settings: frequency, bandwidth, SF, CR, preamble, TX power, LBT, CAD, active state, name |
| `channel_e` | Channel E (SX1261) specific settings: frequency, bandwidth, SF, CR, preamble, TX power, LBT, CAD, RX boost, name |
| `channel_f` | Channel F (concentrator's single-SF modem) settings |
| `bridge.rules` | Source → target mapping, packet type filters, per-rule delays |
| `region`, `rf_center_freq_mhz` | Region and RF center selection |
| `gpio_pins`, `spi_devices` | Board pin and SPI device settings |
| `sync_word`, `adv_config` | Device-wide LoRa sync word and advanced parameters |

### How it flows

```
User changes setting in WM1303 Manager UI
    → API writes to wm1303_ui.json (SSOT)
    → _sync_config_yaml_channels() syncs active channels into config.yaml
    → Desired HAL settings are validated and saved
    → Save & Restart applies radio changes to hardware

Service start / restart
    → _generate_bridge_conf() reads wm1303_ui.json
    → Generates bridge_conf.json (HAL/forwarder config)
    → Copies bridge_conf.json → global_conf.json
    → Backend publishes /tmp/pymc_wm1303_bridge_conf.json
    → lora_pkt_fwd loads that active snapshot
```

### Important rules

1. **Never edit `bridge_conf.json` or `global_conf.json` directly** — they are regenerated on every service start
2. **`wm1303_ui.json` is the source of truth** for desired settings. Live channel metrics describe the running snapshot until restart.
3. **`config.yaml` channel data is synchronized** from `wm1303_ui.json` on save (not the other way around)
4. Saving radio settings alone does not reconfigure running hardware. Use **Save & Restart** to apply them. Bridge rules can hot-reload separately.

## config.yaml

The standard pyMC Repeater configuration file. WM1303-specific additions:

```yaml
radio_type: wm1303
radio:
  frequency: 0             # Unconfigured until channels are set in the Manager
  spreading_factor: 8
  bandwidth: 125000
  sync_word: 5156           # MeshCore private sync word, 0x1424
storage:
  storage_dir: /var/lib/openhop_repeater
```

When `radio_type: wm1303` is set, the factory creates a `WM1303Backend` instance.
Fresh installations have no active channels: the web UI runs, but the radio
forwarder remains idle until channels are configured and the service restarts.

An omitted or null `repeater.identity_key` loads or creates a persisted key.
An optional relative `repeater.identity_file` is resolved beside `config.yaml`,
not against the service's working directory. An unreadable or invalid key file
stops startup without replacing it; failure to save a new key also stops startup
instead of using a temporary identity. Restore the existing key or correct its
permissions before retrying.

Console settings are saved before live application. A failed save leaves the
running settings unchanged; a successful save may still report
`restart_required` if live application was unavailable. Identity edits/deletions
and imported identity keys require restart; existing listeners keep running
until then. Generic radio/CAD endpoints do not configure WM1303 channels.

Console identity creation/updates, configuration imports and applied repeater
keys validate the final saved identity set before writing. Room servers and
companions need distinct nonempty names and distinct one-byte routing hashes,
including the reserved repeater identity. Conflicting keys are rejected, not
regenerated. Pending replacements/deletions are evaluated from saved state,
not the identities still running until restart; deletion remains available to
repair an invalid saved set. Redacted identity imports preserve the latest
saved keys. Room identities accept both 32-byte seeds and 64-byte firmware keys
during live creation and restart.

Room startup now requires matching login/text/protocol handlers and a running
sync worker before the daemon reports ready. New rooms can also activate live
after saving: provisional handlers stay hidden until startup succeeds and the
saved room settings are rechecked. Failure removes only that attempt's runtime
objects and leaves the saved configuration intact. A 15-second HTTP wait timeout
returns `activation_pending: true`, without cancelling startup or claiming that
restart is required; refresh identity status to see whether activation finishes.
Startup and hot-created room workers share health monitoring, and shutdown
drains admitted room starts before releasing their dependencies.

Rooms register even without passwords. `settings.allow_read_only: true` (the
room default) allows new blank-password logins as read-only guests;
`guest_password` grants write access and `admin_password` grants administrator
access. Disabling read-only admission does not remove existing writer/admin
ACL access. Blank-password reconnects cannot create or promote those roles.
The room's post-storage boundary checks the author's full public key and
writer/admin role; read-only clients remain eligible to receive and synchronize
posts. The explicit Console server/system-author path requires the room's own
public key. Failed post insertion does not consume posting quota, and failure
of later activity bookkeeping does not turn an already-stored post into a
failed admission result.
New RF posts are acknowledged only after successful storage; matching accepted
retries do not insert a second post. Rejected posts can retry until a newer
accepted request advances the session timestamp. Retry receipts are RAM-only,
not an exactly-once guarantee across restarts. Room CLI commands require the
CLI wire type and admin access, rather than being inferred from ordinary chat.
Room-admin CLI access is scoped to that room, not the shared repeater host.
Editable values are labelled as saved/desired; CLI changes to room passwords,
name or location require restart, just like Console edits. Removed or replaced
rooms cannot use their old live identity to rewrite the saved replacement.
Login is rejected if its room sync-state reset cannot be saved; the prior
session remains unchanged on that failure.

Set the post limit under each room's `settings.max_posts`, not beside `settings`.
It must be a positive integer and defaults to 32; larger values are capped at
32. The room helper now reads this nested setting during startup and hot creation.
Room settings are validated before saving and again on load/registration:
`allow_read_only` must be a boolean, and `max_clients` must be an integer from
0 to 50 (zero disables new-client admission). Passwords must be strings of at
most 15 UTF-8 bytes without NUL; empty strings or null disable that password
role, and nonempty admin/guest passwords must differ. Numbers and booleans are
not accepted as passwords or coerced from strings. Unknown settings and omitted
defaults are preserved. Correct older out-of-range settings before restarting;
invalid settings are rejected, not silently repaired or overwritten.

Default configuration exports now redact room passwords as well as identity
keys. Import restores redacted room passwords only from an unambiguous matching
saved room and existing field, under the configuration lock; explicit empty/null
passwords remain explicit changes. Full backups requested with
`include_secrets=true` still contain credentials and must be kept private.
Login diagnostics record credential presence/length rather than passwords or
decrypted login payloads. These changes affect new exports and log entries only:
existing exports, logs and diagnostic archives are not scrubbed.
See [validation limits and room reply behavior](testing.md#validation-limits).

Targeted Console, radio CLI/GPS and Manager saves share an in-process transaction
lock. They merge requested changes into the latest saved configuration so a
pending radio change is not lost to an unrelated save. Only explicitly updated
runtime sections are applied; reading a saved snapshot does not retune hardware.

Adaptive advert thresholds use activity per minute, not percentages. The
Console's `quiet_max`, `normal_max` and `busy_max` correspond to the runtime's
`normal`, `busy` and `congested` lower bounds. Both naming schemes are supported;
canonical keys take precedence in YAML, and Console saves synchronize each
changed pair. The activity metric is smoothed adverts/minute plus 0.1 times
smoothed packets/minute. Adaptive limiting remains disabled by default.

### Companion storage ownership

Companion preferences, contacts, channels and queued messages are stored under
the full public key (`pubkey:<64 hex characters>`). Mesh routing, TCP client
status and Console selectors still use their existing one-byte hashes. Reusing
a routing hash does not reuse another identity's state.

Older databases stored state only under `0xHH`, without the owner's public key.
If such an unclaimed bucket exists, that companion does not activate until its
historical owner is explicitly confirmed. The daemon does not guess from the
current name, routing hash, contacts or message senders. Existing rows remain
untouched.

Before confirming ownership, stop the service and make a private SQLite backup
of the database in your configured `storage.storage_dir`. Use SQLite's backup
API, or copy the database only after a clean shutdown; do not copy a running
WAL database's main file alone. Verify which full public key owned the complete
legacy bucket. If keys were replaced or the bucket may contain mixed state,
do not guess or use a private key/seed as confirmation.

Add this field to the matching companion's existing settings:

```yaml
settings:
  # Keep its other existing settings.
  legacy_storage_owner: "<verified historical owner's 64-character public-key hex>"
```

The confirmed owner must share the bucket's first byte, but may be a retired
identity rather than the currently configured one. On activation, one database
transaction copies all four tables into that owner's empty full-key scope,
preserves message order and original rows, and records the claim. Existing
destination data or a conflicting claim aborts without overwriting anything.
Recorded claims prevent repeat copying, including after queue consumption or
data purging. A replacement identity uses its own separate scope.

Remove the one-time setting after a successful migration. The create/update
identity APIs also accept it; send `settings.legacy_storage_owner: null` in an
update to remove it. Changing an identity key clears a previous confirmation
unless the request explicitly provides one again. Ordinary settings edits do
not clear it. Config exports/imports preserve this field, so remove it or verify
it against the destination database before importing a configuration elsewhere.
This migration has been statically reviewed only; it has not been executed by
the latest validation pass.

### Companion capacity and stored data

Companion settings now honor `max_contacts` (default 1000, minimum 1) and
`offline_queue_size` (default 512, minimum 0). The create/update identity APIs
also preserve these fields; updates take effect after restart. Use integers,
not fractional values or booleans. `max_contacts` cannot exceed the protocol's
32-bit contact-count limit. MeshCore's compact device-info capacity field still
saturates at 510; this does not reduce the actual configured store capacity.

Startup does not trim stored contacts to fit a smaller capacity. An oversized
contact list, duplicate public keys, invalid contact fields, duplicate channel
slots or malformed channel secrets leave that companion inactive and preserve
its stored rows. Increase `max_contacts` if the list is valid but too large;
back up and inspect malformed records before correcting them. Channels retain
valid 16-byte secrets by zero-extending them to 32 bytes; other secret lengths
are rejected rather than padded or truncated.

`offline_queue_size: 0` disables admission of new queued messages, including
messages a connected client would otherwise fetch through queue sync. It does
not purge older persisted messages. Large contact-list responses wait for room
in the bounded TCP output queue rather than dropping contacts when it fills.

Ordinary command replies also wait for bounded output admission. Each command
keeps a bounded set of response frames owned by its originating connection;
delayed login, status and telemetry replies cannot reach a replacement client
or overtake their initiating `SENT` response. Disconnect discards that client's
output but does not cancel an already-admitted radio request. Unsolicited radio
pushes still use the existing non-blocking queue policy.
The configured client idle timeout covers each complete inbound frame,
including its length and body, but never cancels an admitted command. Disabling
the timeout also disables this incomplete-frame deadline.

Binary/anonymous and PATH request replies register their originating connection
before transmission. An early reply waits until `SENT` enters that connection's
output queue; failed sends or disconnects discard only the associated client
reply, without cancelling radio work. Up to 128 TCP request replies may be
pending, including received replies waiting for output. Unowned web-region
responses keep their existing broadcast behavior; a stale TCP-owned region
response cannot become such a broadcast after reconnect.
Binary/anonymous parsing metadata is independently capped at 128 requests per
bridge and expires monotonically after successful transmission, retaining the
existing requested/adaptive lifetime and ANON subtype. It does not expire while
TX is still queued. Expiry is pruned on request/response activity; shutdown
clears remaining metadata after draining owned work.

Message reception and queue sync share a per-companion lock, preventing an
in-flight database insert from leaving the same message independently readable
in RAM. Confirmed SQLite duplicates count as already retained. Failed saves
keep their exact RAM entries for FIFO retries; later arrivals cannot bypass
those pending entries during persistence. Shutdown retries remaining fallbacks
and reports incomplete persistence if any still cannot be saved.

Queue sync reads and encodes its next message before removing it, then waits
for bounded output admission. Read/encoding failures report an error instead
of an empty queue. A disconnect before admission leaves the message queued.
Output admission is not a client acknowledgement: a connection failure after
admission can still lose an in-flight frame, and a database deletion failure
after admission can cause a later duplicate.

For accepted private plain/signed messages, the sender's local modification
time is updated together with message retention. Signed room posts also advance
the room contact's `sync_since` without moving it backwards. With SQLite, the
message and complete real-contact snapshot commit in one transaction; failed
admission leaves the cursor unchanged, and a retry merges the latest contact
state. A removed sender is not recreated. Anonymous contact metadata remains
RAM-only, and memory-only mode publishes metadata after RAM queue admission.

Delayed text ACK work is tracked before its task starts and drained by the
bridge. Shutdown rejects late transmission through the existing injector gate;
rejected sends are not logged as successful. Firmware ACK timing and admission
policy are unchanged: an RF ACK is not a database-commit acknowledgement.

### Importing repeater contacts

The companion contact-import API seeds unknown peers from stored repeater
adverts. It recognizes the stored `Chat Node`, `Repeater`, `Room Server` and
`Sensor` types; API filters remain `companion`, `repeater`, `room_server` and
`sensor`. Unknown advert types are not imported as anonymous contacts. The
requested candidate limit is capped at `max_contacts`, which is also the
default when no limit is supplied.

Existing contacts keep their names, favourites, routes, sync state and advert
data. If candidates exceed capacity, favourites remain protected and freshness
determines which non-favourites survive; existing peers win equal-freshness
ties. Repeating an unchanged import does not rotate older candidates into the
table. New contacts have unknown routes and remote advert timestamps: the
repeater's local reception time is not the sender's signed ADVERT timestamp.
Their local modification timestamps advance past the existing sync watermark.

The API reports actual retained additions (`imported`), existing contacts
evicted (`removed`) and candidate rows not retained as additions (`skipped`).
Invalid candidates or a failed save leave the live contacts and stored snapshot
unchanged. Empty/no-op imports do not rewrite the contact table. A successful
response follows one snapshot save and publication on the daemon loop. HTTP
timeouts explicitly report that the owned operation may still complete; check
the contact list before retrying. Web path resets use the same serialized,
save-first workflow.

### Companion TCP mutations

With SQLite enabled, contact add/update/remove/path-reset and channel edits save
their candidate state before publication and acknowledgement. Failed saves
return the protocol's file-I/O error, leaving active state unchanged. Transient
type-0 contacts use the separate eight-entry in-memory pool and are never
persisted. Real-contact capacity and favourite-aware overwrite policy do not
consume those reserved transient slots.

Contact updates require the complete mandatory frame fields. They preserve
retained sync/advert metadata, omitted GPS fields, all 64 path-buffer bytes and
an explicitly supplied modification timestamp of zero. TCP path reset follows
firmware semantics: only the path-length marker becomes unknown; the buffer and
modification timestamp remain unchanged. The web reset also clears the buffer
and advances the sync watermark so connected clients can learn that web change.

Channel names retain whitespace. The TCP parser accepts the existing exact
16-byte-secret, 32-byte-secret and hexadecimal-secret payload formats; the latter
two remain Python extensions, not MeshCore firmware support. Channel reads still
expose the first 16 secret bytes, as before. Malformed or incomplete mutations
return an argument error. Responses wait for bounded queue space after the
transaction lock is released.

`IMPORT_CONTACT` is different: its acknowledgement means the raw ADVERT was
parsed and queued for normal verification and policy handling. It does not
promise that the contact was accepted or stored, matching MeshCore firmware.

Received adverts are now saved even when no companion client has connected.
Ordinary contact changes use one upsert; eviction or demotion uses one atomic
snapshot so the old stored peer cannot reappear after restart. Publication and
deletion notifications follow the save. Valid reception paths may still update
the informational path cache if contact persistence fails.

New adverts for temporary anonymous recipients undergo normal type, hop and
capacity admission. They do not inherit the temporary route or bypass filters.
The reserved anonymous pool no longer reduces real-contact capacity. Original
wire advert timestamps drive replay checks, including clocks ahead of this
host; an omitted GPS location does not erase a known contact's coordinates.
Nonzero wire contact types remain real contacts even when their type names are
not recognized by the Python event producer.
Queued event dispatch is owned by the bridge and drained during shutdown.

Learned `PATH` routes also save before becoming active, even before a client has
connected. Updates preserve unused path-buffer bytes and contact metadata while
refreshing the local modification timestamp. Anonymous routes remain RAM-only.
A failed route save leaves the previous route active without suppressing the
authenticated packet's embedded response or acknowledgement. Matching pending
path-discovery responses report their paths without replacing the saved route
or sending a reciprocal path.
PATH discovery registers its tag before awaiting transmission, so an early
response is recognized. At most 64 discoveries may be pending; failed sends
release their reservation, and successful sends start the advertised response
window only after transmission completes. Expired requests are pruned on later
request/response activity, with remaining state cleared during shutdown.
Zero-hop CONTROL discovery uses the existing response broadcasts and does not
register disposable callbacks in the web discovery handler's shared tag map.
Only direct, zero-hop, high-bit CONTROL packets reach discovery consumers;
flood packets cannot bypass the parser's rejection through the TCP push path.

Web contact imports now notify connected clients of additions and deletions;
web path resets send path-update hints. Contact notifications wait until an
in-progress contact dump finishes, so its older rows cannot undo a deletion.
They recheck current state and the client connection after waiting. This output
ordering does not hold the persistence lock. Learned-path notifications use
bridge-owned background callbacks so slow clients do not delay radio response
handling; shutdown drains those callbacks.

TCP reconnects retain unrelated web event subscriptions and add frame handlers
only when absent. Concurrent first web-event connections share one registration
lock, preventing duplicate subscriptions. A web stream opened before any
companion exists retries registration on its event/keepalive cycle after the
first companion activates. Client queues are registered only when streaming
starts, so a response closed before its first iteration leaves no orphan queue.
If a slow client overflows its bounded SSE queue, the stream emits
`resync_required` with reason `slow_consumer` and closes instead of sending
keepalives forever without new events. EventSource clients can reconnect, but
must reload durable state through REST after that gap. SSE has no replay log;
transient acknowledgement/login notifications may not be recoverable.

## bridge_conf.json — Generated HAL Config

Generated by `_generate_bridge_conf()` from `wm1303_ui.json`. Contains:

| Section | Contents |
|---------|----------|
| `SX130x_conf` | SX1302 concentrator config: radio chains, IF channels, gain tables |
| `SX1261_conf` | SX1261 companion radio config: spectral scan, Channel E parameters |
| `gateway_conf` | Server info, keepalive settings |
| `debug_conf` | Debug and diagnostic settings |

### Critical: Unused IF Channel Slots

The `_generate_bridge_conf()` function must set **unused IF demodulator slots to `enable: false`**. A bug in versions prior to v2.0.5 set unused slots to `enable: true`, causing concentrator packet flooding.

```json
"chan_multiSF_0": { "enable": true, "radio": 0, "if": -200000 },
"chan_multiSF_1": { "enable": false },
"chan_multiSF_2": { "enable": false },
...
"chan_multiSF_7": { "enable": false }
```

## Channel Configuration

Live signal cards show unavailable data explicitly. Noise values expose their
source and observation timestamp where available; rolling LBT estimates have
sample counts but no timestamp. Zero-sample spectral entries are not measurements
and are not added to new history. Older stored placeholders cannot be reliably
distinguished from real readings and are not rewritten.

### Channels A–D (Concentrator)

Each channel is configured with:

| Parameter | Description | Example |
|-----------|------------|--------|
| `frequency` | Channel frequency in Hz | `869525000` |
| `bandwidth` | Bandwidth in Hz (A–D require 125000) | `125000` |
| `spreading_factor` | Spreading factor (5–12) | `12` |
| `coding_rate` | Coding rate (4/5–4/8) | `"4/8"` |
| `preamble_length` | Preamble length | `17` |
| `tx_power` | TX power in dBm | `20` |
| `lbt_enabled` | LBT enable/disable (optional, per channel) | `false` |
| `lbt_threshold` | LBT RSSI threshold in dBm | `-75` |
| `cad_enabled` | Enable channel activity detection before TX | `false` |
| `active` | Channel active/inactive | `true` |
| `name` | Display alias (not used as ID) | `"SF12-868"` |

> **v2.1.1 note:** Legacy field names (`sf`, `bw`, `cr`) are automatically normalized to standard names (`spreading_factor`, `bandwidth`, `coding_rate`) during installation and upgrade. The UI also normalizes these on every data load via the `normCh()` function. Always use the full field names in new configurations.

### Channel E (SX1261)

Channel E has additional parameters:

| Parameter | Description | Example |
|-----------|------------|--------|
| `enabled` | Enable Channel E (instead of A–D's `active`) | `false` |
| `boosted_rx` | RX boost mode enable | `true` |
| `bandwidth` | 62500, 125000, 250000 or 500000 Hz | `125000` |
| `spreading_factor` | SX1261 path supports 7–12 | `8` |

### Channel F (Concentrator Single-SF Modem)

Channel F uses `enabled`, with bandwidth 125000, 250000 or 500000 Hz and SF 5–12.
The template leaves it disabled, with BW 250000 and SF 9.

HAL LBT cannot be combined with an enabled 500 kHz TX channel, even if LBT is
selected only on another channel. Invalid combinations are rejected before saving.

### Channel Names Are Aliases

Channel names (e.g., "SF12-868", "EU-Narrow") are **display aliases only**. Use stable channel identifiers (`channel_a` through `channel_f`) in rules and integrations.

## Bridge Rules (SSOT)

Bridge rules define how packets are routed between channels:

```json
{
  "bridge": {"rules": [
    {
      "id": "rule_1",
      "source": "channel_a",
      "target": "channel_b",
      "packet_types": [],
      "enabled": true
    },
    {
      "id": "rule_2",
      "source": "channel_b",
      "target": "repeater",
      "packet_types": [],
      "enabled": true
    }
  ]}
}
```

### Available targets

- `channel_a` through `channel_f` — radio channels
- `repeater` — the pyMC repeater handler (hop +1, path update)

An empty `packet_types` list allows all packet types. Use the Manager's packet
type selector for a restricted rule; do not use the old `types` key.

## GPIO Pin Configuration

GPIO pins are configurable via the Advanced Config tab:

| Pin | Default BCM | Function |
|-----|------------|----------|
| Reset pin | 17 | SX1302 hardware reset |
| Power pin | 18 | SX1302 power enable |
| SX1261 reset | 5 | Active-low SX1261 reset |
| AD5338R reset | 13 | DAC reset (unused) |

Changing GPIO pins regenerates the reset/power cycle scripts automatically.

## Configuration on Fresh Install

During installation:

1. Template `config.yaml.template` is copied to `/etc/openhop_repeater/config.yaml`
2. Default `wm1303_ui.json` is copied to `/etc/openhop_repeater/wm1303_ui.json`; the bootstrap wizard selects region and sync-word defaults, not active radio channels
3. `bridge_conf.json` and `global_conf.json` are generated on first service start
4. Service generates the JWT token for pyMC Repeater authentication

## Configuration During Upgrade

The upgrade script (triggered via `bootstrap.sh` or `upgrade.sh`):

1. Preserves existing `wm1303_ui.json` settings
2. **Deep-merges** new keys into existing config (e.g., new Channel E sub-keys)
3. Does not overwrite user settings
4. Regenerates `bridge_conf.json` on service restart
5. Syncs `config.yaml` channel data if needed

Existing TX delays and cache settings are preserved. Legacy `sf`/`bw`/`cr` keys
are normalized without replacing the saved values with template defaults.
`--force-config` is the explicit exception: it replaces configuration templates.
If a migration copy fails, the installer stops; inspect its log and both data
directories before retrying rather than starting against a partial migration.

## TX Queue Management (Advanced Config)

Since v2.1.0, TX delay settings are managed in the **Adv. Config → TX Queue Management** section:

| Parameter | Default (v2.1.0) | Description |
|-----------|-------------------|-------------|
| `tx_delay_factor` | `0.0` | Random pre-TX jitter multiplier (0 = no delay) |
| `direct_tx_delay_factor` | `0.0` | Direct TX delay multiplier |

> **Note:** The Config tab has been removed in v2.1.0. TX Delay Factor was the only setting it contained and has moved to Adv. Config.

## Glass Configuration Updates

Glass `patch` recursively merges YAML settings; `replace` replaces only the
supplied top-level sections. Both save before applying runtime changes and keep
unrelated pending settings. Prefer patch for small updates: repeater replacement
must explicitly retain the node name, identity fields and existing login secrets.
Identity changes belong in the Console; WM1303 radio and bridge rules belong in
the Manager. Settings without live reload support are saved with a restart notice.

`glass_managed` is a separate private JSON file. Its patch is shallow; its replace
replaces the whole managed document. A mixed JSON/YAML update can partially save
and reports that failure. Change `glass.cert_store_dir` separately from managed
settings. A successful save does not confirm a connection to the Glass/MQTT server.

Certificate renewal writes a new private `renewal-*` directory under the saved
`glass.cert_store_dir`, validates the certificate/key material, then saves all
three paths together. Encrypted private keys are unsupported. Previous sets
are retained for rollback and existing readers; do not delete files referenced
by active configuration or backups. Custom certificate directories need their
own service-user permissions and backups.

## Related Documents

- [`architecture.md`](./architecture.md) — SSOT model overview
- [`ui.md`](./ui.md) — WM1303 Manager UI
- [`api.md`](./api.md) — REST API (reads/writes config)
- [`installation.md`](./installation.md) — Installation process
- [`lbt_cad.md`](./lbt_cad.md) — LBT and CAD behavior
- [`tx_queue.md`](./tx_queue.md) — TX queue system
