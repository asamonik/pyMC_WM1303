# Software checks and MeshCore compatibility

Run the local regression suite without a Raspberry Pi, radio, or upstream
checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
```

Deployment tests also use Bash and rsync. Tests requiring rsync are skipped if
it is unavailable. When Node.js is available, the update-stream scenario also
checks browser reconnect behavior with fake streams and timers. All test files
and databases are temporary. GPIO commands, radio operations, and service
operations are mocked. The software-checks GitHub
Actions workflow runs the suite on Python 3.11 and 3.12, checks shell syntax, and
checks Python correctness, including undefined names, duplicate keys/methods,
and invalid control flow. Unused imports/locals and redundant f-strings are not
part of this correctness gate.

For source-only validation (without running the application or tests):

```bash
ruff check --select E9,F,PLE,RUF006 --ignore F401,F541,F841 overlay config tests
python3 -m compileall -q overlay config tests
for script in bootstrap.sh install.sh upgrade.sh config/*.sh config/wm1303-upgrade; do
    bash -n "$script" || exit 1
done
git diff --check
```

The static review also used ShellCheck, Cppcheck and GCC's analyzer. Fixes include
checked shell failure paths, SPI/file/SQLite resource cleanup, diagnostic bundle
ownership, and retained async callback/shutdown tasks. Generic CAD calibration
now rejects failed readings and invalid sample counts, cancels abandoned work,
and drains radio restoration before daemon teardown or another run. WM1303 uses
the Manager's CAD configuration, not this generic calibration endpoint.
These source-level checks do not establish runtime or RF behavior; unrelated
style warnings are not treated as correctness failures.
Cppcheck still reports `lbt_tx_allowed` as potentially uninitialized in
`loragw_hal.c`: its path assumes LBT changes from disabled to enabled inside
`lgw_send`. The forwarder serializes HAL access and keeps that startup
configuration fixed; this reviewed warning remains visible, not suppressed.

The tests cover MeshCore path encodings and packet identities, routed packets,
multipart/routed acknowledgements, companion delivery, six-channel transmission queues, cancellation and shutdown,
radio settings and received signal metadata, configuration persistence,
diagnostic collection, complete overlay deployment, and SQLite backups.
Focused retention scenarios check sample conservation, failed-rollup rollback,
nullable averages, cumulative-counter resets and historical chart visibility.

Lifecycle scenarios cover failed HTTP construction and mocked listener startup,
cleanup of owned checkpoint workers and routes, retry with a new server instance,
and idempotent shutdown without stopping another listener. Required HTTP, bridge,
TX scheduler, or RX task startup failure prevents the daemon's `READY=1`
notification; a required worker stopping later also ends the daemon so systemd
can restart it. Idle and receive-only configurations remain supported. A
real-dependency F-only receive check confirmed callback registration before
readiness and owned radio shutdown, using a mocked backend and HTTP listener.
The storage scenario uses a temporary real SQLite database to check
nonblocking, bounded, ordered writes,
rejection when the queue is full, and draining accepted writes before publisher
shutdown. SQLite connections are closed on their owning threads.
Live daemon statistics expose the storage writer's pending, rejected and failed
write counts. A full backlog rejects new records with an error log instead of
blocking the receive loop or growing without a bound.
Shutdown stops radio-owned producers before draining bridge/storage writes.
A gated dedup batch checks that draining leaves the event loop available and
retains worker ownership through cancellation. Hardware process termination
still has bounded signal/reap timeouts; an in-progress hardware reset can delay
shutdown until its existing timeout.

HAL LBT telemetry matches the HAL's frequency and bandwidth selection. Only a
confirmed LBT refusal is counted as a busy-channel block; ordinary hardware
errors and unmeasured outcomes remain unknown, not passes or blocks. Native
lookup/ACK checks and the radio/bridge scenario cover nearby channels, bandwidth
matching, null outcomes, and preserved failure reporting. Global TX counters
include E/F direct queue submissions exactly once, and count radio attempts
rather than packets rejected before the scheduler attempts a send.

## Reference versions

Protocol changes were compared with the following source snapshots:

| Component | Commit |
| --- | --- |
| [MeshCore firmware](https://github.com/meshcore-dev/MeshCore/tree/0679dbeffc504d562d2f09eb072fdc223f8ffc2a) | `0679dbeffc504d562d2f09eb072fdc223f8ffc2a` |
| [Python core fork](https://github.com/HansvanMeer/pyMC_core/tree/18848a4f5a09164a2f3536ef348243ae7d701e78) | `18848a4f5a09164a2f3536ef348243ae7d701e78` |
| [Python repeater fork](https://github.com/HansvanMeer/pyMC_Repeater/tree/c2d63968bb79af37a8407f0632194ac361d2a92d) | `c2d63968bb79af37a8407f0632194ac361d2a92d` |
| HAL fork used by the installer | `4b42025d1751e04632c0b04160e0d29dbbb222a5` |

The wire checks cover all four route types, one-, two-, and three-byte path
hashes, transport-code prefixes, 64-byte path and 184-byte payload limits, and
TRACE packet identities that include the encoded path length. The parser and
serializer in the Python core were additionally checked with 952 structural
frames; these are software checks rather than over-the-air measurements.

MeshCore interoperability requires matching frequency, bandwidth, spreading
factor, coding rate, and the device-wide sync word. A-D use the concentrator's
125 kHz channels; use E for 62.5 kHz MeshCore presets. E and F also support wider
bandwidths as described in [the radio documentation](radio.md).

Use the private sync word (0x1424) for standard MeshCore nodes. Public mode
changes the physical sync word on every receive path and requires matching
peers; it is not a MeshCore application-level community identifier.

Fresh configurations have no enabled channels or bridge rules. Enable the
desired channels and add explicit bridge rules for RF forwarding. Empty rules
allow local reception only. Disabling A does not rename B, C or D. A-D and F
must fit the concentrator's shared receive bandwidth; E receives independently.
The current HAL cannot combine any enabled LBT with a 500 kHz TX channel.

The overlaid HAL libraries, packet forwarder and spectral utility were built
on the development host. Startup, local packet reception, readiness notification
and shutdown were also checked against the real Python dependencies using
simulated radio and HTTP endpoints. Neither check executes the radio hardware.

A separate offline packaging check installed the actual editable core and
repeater distributions from copied reference checkouts into a temporary venv.
Distribution metadata and `repeater.config`, `openhop_core.hardware`, and
`WM1303Backend` imports passed with isolated Python (`-I`), without relying on the
working directory or `PYTHONPATH`. This checks package registration and imports,
not fresh network dependency resolution or Raspberry Pi native packages.

The WM1303 Manager uses the Console's JWT/API-token authentication. Open the
Console to sign in or complete initial setup; the Manager displays a link when
the session is missing or expired. Metrics workers start and stop with the HTTP
server and use the configured storage directory. Spectrum displays contain
measured data only, with observation timestamps; absent data is not simulated.
Password changes persist before updating live Console and MeshCore login state.
Setup updates shared state immediately, preserves WM1303 channel settings, and
exposes a failed restart through setup status. These flows were checked with
temporary configuration files and actual login helpers; service restarts were
mocked.

Mode, duty-cycle, advert limiter, flood-policy and generic radio/CAD changes
also save before changing live state. Invalid requests and failed saves leave
the active settings unchanged. The airtime limiter refreshes its cached limit
without losing transmission history. Configuration import preserves redacted
credentials and reports staged settings that require restart; WM1303 radio
imports cannot override the Manager. Vanity identity replacement is saved for
restart rather than presented as an immediate identity switch.
Identity creation, updates and deletion use detached, serialized configuration
transactions. Updates/deletions require restart and leave the active identity's
handlers and listeners intact until then; new identities still attempt live
activation after a successful save. GET requests do not add runtime metadata
to the persisted configuration.
An additional check imported the full API, repeater engine, airtime manager
and advert helper from the reference dependencies. It verified live limit and
threshold changes, preserved airtime history, and unchanged runtime/disk state
after real atomic-write failures, with temporary storage and fake hardware.

Saving radio settings updates the desired configuration. Apply them with a
service restart. Automatic watchdog recovery retains the previous active radio
configuration, so a Save cannot partially retune the running system.
An intentionally empty Manager bridge-rule list remains empty after restart;
legacy YAML rules are used only when the Manager file is absent. Malformed
Manager rules fail startup and leave working rules unchanged on hot reload.

Companion checks against the referenced Python dependencies also exercised
encrypted login/status, bidirectional text, co-hosted room delivery, signed
room posts, routed ACK confirmation and once-only persisted message retrieval.
These used temporary identities/storage and local packet injection, not RF or
a mobile application's complete connection handshake.
Manual/selective contact preferences also survive companion reconstruction;
new companions retain the normal automatic-discovery default.

Historical metrics retain one-minute summaries until day three, then roll
directly into fifteen-minute summaries. Existing ten-minute summaries remain
readable until expiry. Nullable averages now retain their own sample counts;
cumulative channel counters stay raw until expiry, with one older baseline
per retained channel. Queries report actual bucket widths and include whole
summary buckets overlapping the requested window. Boundary summaries and old
averages/distinct counts are approximate; previously discarded data cannot be
reconstructed.
Console charts preserve SQLite bucket totals, native RRDtool rates and missing
readings, and report their data source and counter units.

RRDtool packet-type totals integrate observed per-second rates over the requested
window, following the [RRDtool counter semantics](https://oss.oetiker.ch/rrdtool/doc/rrdcreate.en.html).
They are marked approximate; missing/reset intervals are not measurements.
AVERAGE/MIN/MAX reads have separate caches. Existing RRD files and archive
schemas are preserved; a decreasing counter creates an unknown transition
instead of a rollover spike.

SQLite maintains at most 17 lifetime packet-type counter rows in the same
transaction as each packet insert. They survive raw packet pruning/purging and
are seeded once from retained pre-upgrade history; older deleted packets cannot
be recovered. This also removes repeated full-log scans from RRD updates.
A native RRDtool 1.9.0 check verified constant traffic rates through SQLite
pruning and handler restart, plus boundary integration and counter-reset
handling, using only temporary databases.

The WM updater uses the installed fork's VERSION and its existing bootstrap
scripts, launched in a separate systemd unit. The Console's update stream can
reconnect throughout a long build. Launcher commands, failure/completion state,
fork preservation and browser retries were checked with mocks. A real local
HTTP check verified authentication, the Console adapter and shutdown; no
installer, updater, privileged service command or radio operation was executed.

## Validation limits

The repeater stage now enforces MeshCore DIRECT next-hop routing, scoped-flood
validation, supported flood payload types, and airtime-based delay factors.
Explicit radio-to-radio bridge rules still support transparent cross-channel
copying; those are distinct from passing packets through the repeater stage.
The full upstream OpenHop suite does not pass unchanged: some expectations
conflict with the referenced firmware, and newer companion policy hooks,
neighbor-link observations and raw local-transmission echo APIs are not all
implemented in this overlay.
The local regression suite is not a claim of complete OpenHop feature parity
or of the absence of all bugs.

The daemon now uses a small Glass adapter over the referenced upstream handler.
Its patch and replace paths save before changing runtime configuration;
replacement affects only supplied top-level sections and preserves unrelated
pending settings. Managed JSON writes are atomic and private (`0600`). Known
Glass scalar types are checked before saving, and the inform-interval command
no longer changes memory before a successful save. WM1303 settings stay in the
Manager, and identity changes use the Console's lifecycle. Replacement cannot
silently omit existing node identity fields or login secrets.
These adapter changes and the latest shutdown/installer/reload/preference fixes below were
statically reviewed and syntax/lint checked only; the earlier runtime checks
above do not validate these latest batches.
Managed JSON and YAML are separate commits: a mixed update reports partial
persistence if the second save fails. Change the managed-settings directory in
a separate update. Saved/reload status does not confirm MQTT connectivity.
Certificate renewal stages a private generation, checks PEM loading and the
certificate/key pair using [Python's SSL API](https://docs.python.org/3/library/ssl.html#ssl.SSLContext.load_cert_chain),
then saves all three absolute paths in one YAML commit. New paths also trigger
MQTT's existing TLS-context reload. Old generations remain available to readers
and configuration backups; committed or uncertain-save files are never removed
on activation failure. These checks do not establish validity dates, remote
trust-chain acceptance, or successful authentication.
Glass control work drains before radio/storage teardown; its MQTT publisher
stays available until storage drains. Cancellation of the drain caller retains
the unfinished inform task for a later awaited stop. Storage draining confirms
publisher calls completed, not broker delivery: MQTT remains QoS 0, and pending
Glass command results are memory-only.

HTTP shutdown now rejects new requests and closes response iterators at their
next yield, including upstream SSE streams. Main awaits the HTTP worker drain
before radio/storage teardown rather than abandoning its stop thread after
three seconds. A quiet companion SSE stream can wait for its configured
heartbeat (15 seconds by default); custom settings can extend that wait.
Installer/upgrade scripts no longer generate inline identities: first-start
creation and preservation of file-backed identities belong to the runtime loader.
Live reload hooks now report failures; discovery-handler changes require restart,
and generic radio power changes check the hardware setter. Reloads are sequential,
not rollback transactions: a later failure can leave earlier steps applied.

The five installer/upgrade JSON/YAML normalization, migration and merge paths
now serialize before atomic replacement, preserve existing permissions and
ownership, and stop on errors. Malformed existing documents are not replaced
with defaults. Initial template copies and explicit `--force-config` replacement
are separate paths, not covered by this atomic-migration change.

Companion shutdown closes TCP admission and drains admitted commands before
radio/storage teardown. Router delivery and bridge-owned receive/background
work drain before the final contact/channel snapshots; both saves are attempted
and database rejection is reported. Cancelled drain callers retain the owned
work instead of abandoning SQLite threads. Live activation and its rollback
are serialized, and shutdown waits for them before releasing dependencies.
Reconnects close superseded readers and finish their admitted command before
replacing shared session queues, preventing its reply from reaching the new
client. Undelivered TCP replies are dropped on disconnect; asynchronous PUSH
notifications remain companion-wide rather than session-specific.

Companion preference updates now serialize candidate construction, persistence
and publication across HTTP and TCP callers. A failed SQLite save raises an
error and leaves active preferences unchanged; successful saves publish a new
preferences object and update the multi-ACK handler. Source inspection accounts
for all eight supported preference setters; the two shared-radio setters remain
rejected by the core bridge. These paths remain synchronous, so database latency
can delay command handling.

A missing preference row retains defaults. Read errors, malformed JSON and
invalid known fields instead prevent that companion's activation without
rewriting its stored state. Binary scope keys are decoded strictly; numeric
values must be finite and satisfy the checked protocol bounds. Missing fields
retain defaults and unknown future fields are ignored.
Protocol names, including empty names and surrounding whitespace, remain
unchanged in SQLite. Names incompatible with Console naming policy skip the
optional YAML copy. If SQLite commits but an attempted YAML copy fails, the
operation reports partial persistence without undoing the saved preferences;
retrying the rename retries that copy. Without SQLite, a configured name-save
callback must succeed before publishing the rename. YAML copies also check the
active public key, so a still-running companion cannot overwrite a staged
replacement or recreate a deleted identity. Unrelated preference changes do not
rewrite pending YAML name settings.

Companion storage now uses full-public-key owner scopes for preferences,
contacts, channels, messages and the inherited web contact-import path. Routing
hashes and active-client association remain unchanged. In-place private-key
replacement is rejected; use the Console's staged identity replacement.
Legacy hash-only records are never loaded automatically. A configured,
explicitly confirmed historical owner permits one atomic copy into an empty
destination, with original rows and message order retained. A durable claim
registry prevents reassignment and repeat copying, and is excluded from data
purging. Unconfirmed buckets, destination conflicts and migration failures leave
the affected companion inactive. See the required
[migration procedure](configuration.md#companion-storage-ownership).
The latest static pass reviewed owner-key call paths and migration transaction
boundaries; it did not run a migration or validate a real legacy database.

The latest companion loading pass stages and validates complete contact lists
before replacing the live store. Duplicate keys, capacity overflow, malformed
binary fields and values outside the encoded wire fields now fail without
publishing a partial list. Valid packed paths, zero route bytes, unused path
tails and future timestamps are preserved. Startup rejects persisted anonymous
placeholders, which final snapshots would otherwise omit. Channel loading and
SQLite channel snapshots likewise reject duplicate slots and malformed secrets;
validation runs before a snapshot deletes any old rows.

Configured contact and offline-message limits now reach bridge construction and
are preserved by identity create/update endpoints. Contact dumps use copied
contact values and bounded, disconnect-aware queue admission, retaining the
protocol's total count and final modification watermark. The wire command uses
the firmware's strict `lastmod > since` boundary, without changing generic
store iteration. A full queue waits no longer than the configured client idle
timeout (unless explicitly disabled), then closes the stalled connection. These
changes received source review and static checks only; large-list transfer,
slow-client disconnects and persistence failure paths have not been executed.

Contact import now reads bounded, normalized advert candidates without writing
them directly into companion tables. It builds a validated detached selection,
preserves existing contact metadata and favourite protection, and saves one
snapshot before publishing into the same ContactStore retained by protocol
handlers. Source freshness determines selection independently of the new sync
timestamp, including retry ties and clocks moving backwards. Source reception
time is never presented as a remote ADVERT timestamp. Empty/no-op imports skip
the snapshot write; malformed rows and failed reads/saves cannot masquerade as
successful imports. Import results distinguish additions, removals and skips.

Contact saves, delayed upserts, import and web path reset share a persistence
lock. Snapshots and callback targets are resolved after acquiring it; stale
callbacks cannot resurrect an already removed contact. Web mutations execute on
the daemon loop, and their final synchronous save/publication has no intervening
await. This can briefly delay other loop work during SQLite writes. Transient
anonymous recipients remain in RAM but outside persisted snapshots. HTTP timeout
does not cancel admitted work, which frame shutdown drains before final saves.
These transaction, retention and lifecycle changes received static source,
syntax and lint review only; they have not been exercised against a database or
live companion client in this pass.

TCP contact mutations now use the same prepared-state/save/publication ordering,
including explicit bounded transient-pool changes and real/transient promotion
or demotion. Wire parsing retains complete path buffers and optional-field
semantics, and does not replace explicit timestamp zero. TCP reset preserves the
firmware's existing buffer and timestamp; deferred ADVERT import keeps its
distinct queued-processing acknowledgement. Channel mutations serialize against
channel snapshots, publish into the retained store only after saving, and notify
subscribers afterwards. Storage failures return file-I/O errors rather than OK;
response backpressure never holds a persistence lock. These changes received
static source, syntax and lint checks only, with no command/client or database
execution in this pass.
Successful automatic TCP-contact eviction also queues the firmware deletion
push before its OK response, so an incremental-sync client can remove the old
peer. Failed saves do not emit that deletion notification.

The regular ContactStore CRUD paths now enforce separate real/anonymous bounds,
including promotion, demotion, refresh and valid `uint32max` eviction timestamps.
The companion's received-advert path rechecks replay state after taking the
persistence lock, applies new-contact filters to prior anonymous recipients and
saves before publication or deletion callbacks. Ordinary accepted contacts use
an upsert; real eviction/demotion removes and replaces records in one snapshot.
This persistence no longer depends on client-installed advert callbacks.

The event decoder recovers the original remote timestamp, contact-type nibble
and GPS-presence bit from retained, already-verified packet bytes; it does not re-run signature
verification or clamp future remote clocks. Missing/unknown location presence
preserves existing real-contact GPS. Unknown nonzero wire types remain real
contacts, not anonymous recipients. A full table reports the discovered contact
before its contacts-full notification, matching firmware ordering.
Paths use their packed lengths, not unused
buffer tails. Event dispatch is scheduled into the bridge's owned task set
before subscriber execution, closing a shutdown race with queued events.
The contact-based TRACE convenience method also respects the packed route and
rejects unknown or incompatible-width paths; TCP's explicit-route TRACE helper
is unchanged. These changes received static review and syntax/lint checks only;
no database, event-loop, client or RF execution was performed for this pass.

The route-update pass intercepts the protocol handler's early in-memory write,
then validates and commits a detached contact under the shared persistence
lock. Ordinary PATH updates retain unused route-buffer tails, refresh local
`lastmod`, preserve other metadata and leave anonymous recipients RAM-only.
Save failures leave the old route active and do not escape through the
authenticated response parser. Matching pending path-discovery replies skip
both route mutation and reciprocal PATH transmission, following the existing
pending-tag correlation contract.

Web import/reset notifications and received contact hints now share an output
lock with complete contact dumps, separate from the persistence lock. After
waiting, they recheck the current key and connection; a reintroduced real
contact receives a refresh instead of a stale deletion. Existing anonymous
contacts retain their protocol advert/path hints. Route notifications are
scheduled as owned bridge callbacks after commit, keeping client backpressure
outside radio response/ACK processing. Static review checked these ordering and
shutdown paths; no database, concurrent-client or packet execution was used to
verify them.

The message-queue pass serializes all three core queue producers (direct text,
channel text and channel data) with persistence and sync consumption. RAM
fallback metadata follows exact entry identity and live queue order; channel
entries evicted by the queue policy are not resurrected by retry. Confirmed
SQLite duplicates remove their RAM copy, while failed admissions keep it.
Final shutdown retries fallbacks and reports any still unsaved.

Message sync now peeks and encodes before bounded output admission, consuming
the selected SQL row or RAM entry only afterwards. SQL peek distinguishes a
successful empty read from a storage/decoding error; owner-scoped deletion and
legacy pop use explicit transactions. This does not provide exactly-once
delivery across transport/database failure. This pass used static source, lint
and syntax review only; no queue, database, client or radio execution verified
these changes.

The room-cursor pass gives the retained text decoder detached Contact/Proxy
snapshots, preventing its early `sync_since` write from reaching the live
contact book. A permanent message owner runs before client notification
callbacks, including before any TCP connection. Accepted private plain/signed
messages merge current contact metadata under the contact lock; signed posts
advance `sync_since` monotonically and both types refresh local `lastmod`.
SQLite message admission and the real-contact snapshot share one transaction,
including verified duplicates. Failed admission or contact upsert rolls back
both; publication follows commit without an intervening await. Removed peers
are not recreated, anonymous metadata remains RAM-only, and memory-only mode
publishes only after RAM admission. FIFO retries retain message data, not a
stale contact snapshot.

Actual delayed text ACK work is registered in the bridge's owned task set
synchronously, before the core's compatibility waiter starts. The waiter is
shielded; shutdown drains the actual work, which checks bridge stop state and
uses the existing daemon injector shutdown gate. Rejected transmission is no
longer logged as a successful send. ACK delays and ACK-before-admission policy
remain unchanged. Static review checked rollback, ownership and callback/lock
ordering only; it did not execute SQLite transactions, event-loop concurrency,
client delivery or radio transmission.

The command-output pass captures ordinary replies by exact command task and
originating writer, with at most 256 frames per capture. Flushing waits for
bounded output admission and rechecks the connection. Delayed login/status/
telemetry completions have their own bridge-owned capture and wait until the
initiating command has flushed its `SENT` frame. Their compatibility waiters
are shielded; cancellation before startup closes the unawaited coroutine.
The full-frame read timeout now covers prefix, length and body, excluding
command execution so it cannot orphan an admitted persistence operation.

PATH discovery reserves its tag before asynchronous transmission, bounds
pending requests and cleans up failed/cancelled sends. A successful send starts
the advertised response window after TX; an early consumed response is not
registered again. A verified PATH is classified consistently through route,
reciprocal and response handling, even at the expiry boundary. Static review
also checked command frame types, partial-output behavior and shutdown
ownership. No event-loop, client, database or radio execution verified this
pass. CONTROL commands no longer register unused no-op callbacks in the shared
discovery handler, avoiding leaked entries and collisions with active web
discovery callbacks. Discovery responses still broadcast independently to
companion clients.

Binary/anonymous and PATH replies now register an opaque TCP owner before TX,
using the initiating command task rather than a global active-client field.
The retained binary builders and parser keep ANON subtype metadata. Bounded
bridge records use post-TX monotonic lifetimes, exact-entry cleanup on failure
or cancellation, and do not revive early-consumed responses. Nested task-local
response origins distinguish stale TCP-owned region replies from intentionally
unowned web broadcasts, without changing public callback arguments.

The frame server retains at most one response per request and 128 live reply
owners, including delivery work waiting for output. RX schedules owned delivery
without waiting on the initiating command, avoiding an inline TX/RX cycle.
Delivery requires successful `SENT` admission and the original writer; failure
or disconnect invalidates only that owner's reply and never cancels RF work.
Static review checked bounds, callbacks, task-map cleanup, synchronous legacy
registration, cancellation before a delivery task starts, and shutdown order.
Lint, compilation, shell syntax and whitespace checks passed; no application,
database, client or radio execution verified this request-ownership pass.

The routing/subscription pass removes per-packet mutation of the shared TRACE
injector, preserving each packet's bridge origin across concurrent radio tasks.
CONTROL delivery now enforces the direct zero-hop high-bit gate before parsing
or companion broadcasts, including valid encoded zero-hop lengths 0/64/128.
TCP callback setup is idempotent and preserves web subscriptions; first SSE
registration is serialized across HTTP workers. Streams opened before initial
companion activation retry registration during their event/keepalive cycle.
SSE queues are registered inside the generator's cleanup scope, preventing a
response closed before its first iteration from leaking an orphan client queue.

LBT configuration now rejects more than 16 channels before narrowing or array
access and validates the complete candidate before replacing HAL context.
Debug reference-payload configuration has equivalent 16-entry parser/HAL
bounds. Packet timestamp proximity now uses wrap-aware unsigned differences;
impossible fixed-array/null and byte-length checks were removed while retaining
filename termination and zero-length RX rejection.
Python lint/compilation and shell/whitespace checks passed. GCC syntax checking
was warning-free for the four reviewed C sources. Cppcheck retains 23 existing
format-signedness warnings in that pass; the subsequent diagnostic-only changes
corrected all 23, including negative keepalive values and unsigned bandwidth.
Cppcheck now reports only the reviewed LBT flag warning for those four sources,
and GCC syntax checks remain warning-free. These are static checks, not a build or RF test. No application,
database, installer, concurrent-client or hardware execution verified this pass.

Slow SSE consumers now receive one `resync_required` marker and stream closure
after queue eviction, rather than an apparently live stream that never receives
new events. Queue limits and authentication are unchanged. There is no event
replay: clients must reload durable state after reconnecting, and transient
notifications may be lost.

Daemon shutdown now has one shielded owner shared by all callers. The `run()`
finalizer continues joining it through caller cancellation, then propagates
cancellation only after cleanup settles. Existing startup/worker failures and
cleanup errors remain failures; cancellation of the actual cleanup worker is
reported as incomplete shutdown, not retried indefinitely. The stop-event
waiter is drained with owned service tasks, without an intervening await that
could mask the original failure. Dependency order is unchanged. These guarantees
do not override forced process termination or service-manager deadlines.
The network-address probe also closes its socket on failed route lookup.
Independent static review, Python lint/compilation, shell syntax and whitespace
checks passed. One existing constructor-bypassing fixture was updated for the
new shutdown field; no new tests or runtime checks were executed in this pass.

The worker-drain pass removes timed joins that could report shutdown while
SQLite maintenance, statistics or spectrum writes were still running. Metrics
retention now joins its worker before releasing connections; main also awaits
that join without an outer timeout. Stop requests skip later checkpoint/VACUUM
phases. Checkpoint, storage-statistics, WM recorder and spectrum workers are
joined before their owners finish closing; concurrent storage close callers
serialize through completion without holding the lock needed by writer callbacks.
GPS shutdown uses the upstream full-join option off-loop. Dispatcher shutdown
signals and joins its actual maintenance task, including any threaded health
check, without relying on an event that startup failure can leave unset.

Spectrum scans now use one transaction for all valid channel readings. The
in-memory observation cursor advances only after successful commit, allowing a
failed scan to retry while it remains in the source JSON file, without repeating
partially committed channels. This does not add a durable scan replay log or
guarantee recovery after the source file is replaced. Initialization failures
close their SQLite connection, and a failed worker start leaves no false running
state.

Companion PATH deduplication now retains authenticated local ownership per
recipient, so a duplicate does not become newly forwardable. Unclaimed or failed
recipients remain retryable, including when another recipient succeeded. A
recipient's concurrent copies wait for its actual delivery result; caller
cancellation does not release that reservation while bridge-owned RX still
runs. Completed ownership entries are capped at 1,024 and retain the existing
60-second TTL, measured from successful completion.
The repeater's local PATH helper now tries all ACL clients sharing the source
routing byte until one MAC verifies, rather than stopping at the first colliding
client. The overlay preserves the reference helper's packed-path validation,
authenticated route updates and bundled-ACK callback.

WM1303 shutdown now joins TX scheduler cancellation and queue completion on the
scheduler's event loop before closing radio resources. Its synchronous stop
must run off that loop while the loop remains available, as the daemon already
does. Ownership is retained after failure and premature backend restart is
rejected. Channel E closes its receive-only UDP socket before cancellable
callback draining, so repeated cancellation cannot skip the close.

These changes received independent source review, Python correctness lint and
compilation, shell syntax and whitespace checks only. One existing mocked
backend lifecycle fixture now uses the daemon's off-loop stop contract; no new
test cases were added and no tests, application, database, services or radio
operations were executed for this pass. Full joins remain subject to forced
process termination and service-manager deadlines.

The HTTP lifecycle now serializes complete start/stop calls. Concurrent callers
wait for cleanup; the stopped flag and global engine ownership are released
only after successful cleanup. Constructor/startup errors remain primary when
rollback also fails. SQLite connection ownership is cleared only after a
successful close. Checkpoint/statistics/writer cleanup failures remain visible
after worker exit rather than allowing a later close to claim success. These
failures are terminal when the owning thread has exited, not automatically
retried on a different SQLite thread. Independent cleanup still runs. Main
attempts its loop-thread SQLite close even after worker cleanup fails and
reports HTTP/storage cleanup failure without masking an earlier startup error.

WebSocket packet heartbeats now have per-generation stop events and are joined
before replacement. Plugin cleanup is idempotent and handles a manager whose
thread never started; unsubscribing alone no longer abandons its resources.
The additional companion-proxy/socket cleanup is described below.
CherryPy's own Bus.exit can terminate the process immediately
on listener failure, bypassing Python exception handling and further cleanup;
these source-level improvements do not override that dependency behavior.

Identity API mutations now validate unique names and routing bytes against the
final saved repeater/room/companion set. A colliding room can no longer be saved
and then displace an existing companion through startup order. Redacted imports
resolve against the latest locked saved snapshot. The room startup loader now
accepts the same 32-/64-byte keys as live creation. Passwordless read-only rooms
use the separate ACL/login changes described below; saved-identity validation
does not itself establish room activation.

This pass used independent source review, correctness lint, compilation, shell
syntax and whitespace checks only. No test cases were added and no application,
database, service, network-client or RF behavior was executed or verified.

The companion WebSocket proxy now resolves the active identity's exact bridge
and serving TCP socket rather than desired configuration. Hot-created companions,
staged configuration changes, OS-assigned ports and IPv6 listeners therefore use
the actual endpoint; wildcard addresses map to local loopback and IPv6 scope IDs
are retained. Closing listeners are not used as a fallback. JWT authentication
and raw byte forwarding remain unchanged.
The proxy owns its TCP socket through connect and reader-start failure, unblocks
both directions with socket shutdown and joins its reader before ws4py clears
the stream. WebSocket data and control-frame writes share a write lock; socket
shutdown does not take that lock, so it can unblock a stalled sender.
The WebSocket plugin serializes admission with cleanup, waits for the manager's
actual startup and retains active/in-progress retirement sockets before
stopping it. Sockets unblock before worker joins; the poller and stream are
released only after their users drain. Its small manager loop retains ws4py's
frame parser but also finalizes close replies whose two termination flags are
already set, a path upstream otherwise removes without calling `closed()`.
Failed admission rolls back opened resources, and failed cleanup keeps its
socket/handler ownership for retry. Heartbeats drain with their owning plugin
generation, including when CherryPy initiates cleanup itself.

MeshCore's login marker is preserved as a byte rather than converted to a
boolean: marker 1 indicates admin, marker 2 indicates a read-only guest, and
marker 0 is ordinary access. The parser keeps a separate boolean `is_admin`
that is true only for marker 1. Companion request results and TCP login-success
frames retain the original marker; zero-permission room responses emit marker
2. This wire correction preserves the existing raw ACL numbers, callback
ownership, SENT ordering and reply-to-connection ownership. Room admission,
post-storage permissions and activation are handled separately below.

This proxy/login-marker pass used source review and static correctness,
compilation, shell syntax and whitespace checks only. It added no test cases
and did not execute the application, network clients, databases or radio.

Login starts now reject an already-pending login to the same full public key
before changing its callback or password. Different peers remain independent,
including peers sharing a routing byte. The first callback completion freezes
the returned login result. Registration cleanup is idempotent, handles initial
cancellation and also runs when a response task is cancelled before its first
step; a duplicate cleanup cannot clear a later same-peer request's state.
The legacy per-routing-byte password cache remains unchanged; actual response
authentication and pending-request ownership use full public keys.

The bridge owns initial login transmission before its first await, not only
the later response task. API-caller cancellation is shielded across both phases.
Shutdown closes login admission for all companions and drains admitted initial
sends before releasing radio resources, even when an HTTP worker has already
timed out. Later retries still use the daemon's existing shutdown gate and
response work drains with bridge shutdown. TCP SENT ordering and original-writer
reply ownership are unchanged. No generic request task wrapper was added to
binary/PATH operations, whose registration depends on their initiating task.

Startup now awaits the finite room-start tasks and verifies each staged room's
identity, ACL, login/text/protocol handlers and running sync task before allowing
readiness. Room sync tasks also participate in required-worker monitoring, with
a final worker check after HTTP startup and before READY. RoomServer remains
responsible for stopping its own sync task.

Hot room activation now runs as owned asynchronous work on the daemon loop,
serialized with companion activation. Provisional ACL/helper/room entries are
detached before awaiting startup; they become visible only after matching
readiness checks and a final saved-spec check under the configuration lock.
No await occurs during publication. Failure removes only owned registrations
and drains the attempt's tasks without undoing the saved configuration.
The API reports `activation_pending` if its 15-second wait expires; it does not
cancel the owned start or falsely declare activation/restart necessity. Shutdown
drains accepted starts before their dependencies, and one event-driven health
supervisor also observes rooms added after the daemon's initial worker snapshot.

Passwordless rooms now register normally. Blank-password guests receive the
read-only role only when `allow_read_only` permits it; existing writer/admin
ACL reconnects retain their role without granting new privileges. The existing
Python writer/admin role values remain unchanged. Room storage independently
requires an exact full-key ACL writer/admin match before rate-limit or SQL work;
read-only clients can still synchronize posts. Explicit server-author admission
requires the room's own key. Post admission returns success once insertion
succeeds even if later activity bookkeeping fails, and a failed insert does not
consume posting quota. Room helpers now read `settings.max_posts` from identity
entries, require a positive integer and cap it at the existing limit of 32.

Room text now checks the authenticated full-key client's role and wire message
type before application delivery or ACK construction. Read-only clients cannot
post; CLI_DATA requires admin access, while ordinary chat beginning with a CLI
word remains a post. New plain posts advance the replay watermark only after
successful database admission. One RAM receipt per client permits matching
retries to be ACKed without inserting again; equality with a login, CLI command
or different message is not proof of a saved post. Attempt bits and padding are
excluded from receipt matching, but the ACK hash uses the original timestamp,
full flags byte and raw text through its first NUL. Rejected posts remain
retryable while a newer accepted request has not advanced the session watermark.
Receipts are not persisted across restart. CLI timestamps are reserved before
execution, so a failed or cancelled reply cannot execute the command twice.

Room delivery ACKs use four-byte hashes and the client's stored return route,
including encoded two-/three-byte zero-hop paths, instead of chat's flood
PATH-return behavior. Unknown-route ACKs preserve the request's hash width.
Flood ACKs and room CLI replies recompute transport codes from a matching
allowed request-region key; missing scoped-request keys suppress the reply
instead of falling back to unscoped flooding. No room default-region setting
was added. Existing 200 ms ACK timing and optional 300 ms multi-ACK stagger are
retained. ACK tasks are owned by the text helper, admission closes before its
shutdown snapshot, and rejected sends are not logged as successful delivery.

Early identity loading now rejects malformed configured room entries instead
of skipping them. Missing/invalid names or keys, incorrect types, duplicate or
reserved names, routing-hash conflicts and rejected registrations fail startup
with the room name (or entry index when unnamed). Empty room lists and both
32-/64-byte keys remain supported. Successfully staged rooms still pass through
the helper/sync readiness gate before READY.

Room configuration now shares a non-mutating validator across API candidate
checks, ConfigManager/config saves, configuration loading and room login
registration. It checks the fully merged settings before persistence or live
publication: strict booleans, integer `max_clients` from 0 to 50, positive
integer `max_posts`, and distinct nonempty passwords limited to 15 UTF-8 bytes
without NUL. Null/empty passwords, unknown fields and omitted defaults are
preserved; post limits above 32 retain the existing runtime cap. Old invalid
settings can prevent saves/restart until corrected rather than being silently
coerced. Validation on load precedes identity-file creation.
An explicitly incorrect room type is also rejected before saving; omission
still defaults to a room server.

Default exports now redact room admin/guest passwords. Import restores those
sentinels from the latest locked saved room before validation; missing or
ambiguous rooms, absent fields and already-redacted source values are rejected
without writing. Explicit empty/null values are not replaced. Full-secret
backups remain unchanged. Login-server/ACL diagnostics no longer print password
values or decrypted login payloads, but existing logs, exports and diagnostic
archives are not scrubbed by this change. These backup/logging and validation
changes received source review and static checks only, not runtime verification.

Room login now requires the durable sync-state reset to succeed before reporting
success. A failed reset restores the exact previous ACL client/session or
removes only the new admission; no await exposes the provisional state. New
ACL clients are inserted only after timestamp validation, so rejected replay
attempts no longer occupy slots. Room CLI dispatch is separately restricted
to its own identity: shared-host credentials, reboot, radio changes and other
host commands cannot be reached with room-admin permission. Own editable
settings are read from saved state and clearly labelled as such; writes match
the active room's name and full public key under the configuration lock, then
save for restart without changing the live ACL. Adverts retain a captured
active settings snapshot. Login/CLI payloads and replies are not logged by
these handlers; the core text decoder logs type/length instead of content.
Core ACK tasks retain references and hosted text handlers use their existing
task owner, including cancellation of late ACK work during shutdown.

Room pushes now recheck current client/cursor/route after waiting for the shared
TX limiter, persist pending state before transmitting, and never overwrite a
cursor while starting a push. ACK and timeout updates match the pending CRC
and post timestamp, so a reset login invalidates an old completion. Confirmed
ACKs whose SQL update fails remain owned in RAM for retry before timeout or
eviction work; database read failures no longer look like empty/new sessions.
Shutdown stops room sync before HTTP/radio teardown and retries retained ACK
commits while storage is available, reporting unresolved failures. This is not
a durable ACK receipt log: forced termination can still lose those RAM receipts.
New posts and legacy rows are normalized through their first NUL before signed
text ACK hashing, matching the recipient's visible text.

Two confirmed room protocol gaps remain for a later pass: stored fractional
post timestamps do not match integer reconnect cursors, and room KEEP_ALIVE
still follows the generic response path rather than firmware's room-specific
ACK/count and sync-reset behavior. The latest fixes do not claim those paths
are complete or hardware-verified.

This login-lifecycle/room-readiness pass used independent static source review,
correctness lint, compilation, shell syntax and whitespace checks only. No new
test cases were added. Existing startup/API fixtures were updated for the actual
room mixin, health fields, async activation and current identity validation;
the mocked shutdown drains its started tasks. The existing import/vanity fixture
also exposes the real identity validator with deterministic key derivation;
no validation is bypassed and no new case was added. These latest room permission,
storage-admission and hot-lifecycle changes were also source-reviewed and
syntax/lint checked only. No tests, application, database, network-client or
radio execution were performed.

Hardware verification is still required: a clean Raspberry Pi installation,
an upgrade with existing configuration and packet history, and communication
with MeshCore firmware nodes. Check advertisements, group messages, direct
messages and acknowledgements, TRACE return paths, and cross-channel forwarding
using the intended radio settings. HAL timing, GPIO reset behavior, RF
performance, and power-loss durability cannot be established by this suite.
