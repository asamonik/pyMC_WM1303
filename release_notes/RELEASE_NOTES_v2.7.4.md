# Release Notes — v2.7.4

**Release date:** 2026-08-17
**Type:** Patch release (bug fixes only)

This release fixes four latent bugs that were discovered by a full clean-install
test on a reference device (previously only upgrade paths had been exercised
since the migration from `pymc_repeater` → `openhop_repeater`). Every fix has
been verified end-to-end on the reference hardware. There are no functional
or UI changes in this release.

---

## Summary

| Item | Area | Status |
|------|------|--------|
| Clean install first-boot `PermissionError: /var/lib/pymc_repeater` | Config template + installer | ✅ Fixed |
| Spectrum collector fails to start on canonical install | Overlay Python code | ✅ Fixed |
| Upgrade rebuild fails with `cc1: fatal error: capture_thread.c: Permission denied` | `upgrade.sh` + overlay perms | ✅ Fixed |
| Systemd `Type=notify` timeout loop (`activating` forever) | Repeater daemon | ✅ Fixed |

---

## Fixes

### 1. Clean install: first-boot `PermissionError: /var/lib/pymc_repeater`

**Symptom.** On a truly clean Raspberry Pi OS install (no legacy state on
disk), the `openhop-repeater` service crashed on first boot with
`PermissionError: [Errno 13] Permission denied: '/var/lib/pymc_repeater'`
inside `repeater.web.http_server._init_auth_handlers`. The service entered a
9-cycle auto-restart loop before dropping into systemd failure state.

**Root cause.** The shipped `config/config.yaml.template` referenced the
legacy `/var/lib/pymc_repeater` path for `storage.storage_dir` and
`gps.source_path`, while both `install.sh` and `upgrade.sh` only pre-created
the canonical `${DATA_DIR}=/var/lib/openhop_repeater`. On a clean install the
config-referenced legacy path never existed, and the non-root service user
cannot create directories under `/var/lib/`.

**Fix.** Three-part:

1. Updated `config/config.yaml.template` to reference the canonical
   `/var/lib/openhop_repeater` for both `storage.storage_dir` and
   `gps.source_path`.
2. Added a defensive block to `install.sh` after the "Setting configuration
   file ownership" step that reads `storage.storage_dir` from the live
   config and, when it points at a non-existent non-canonical path,
   creates a `ln -sfn ${DATA_DIR} <path>` symlink so subsequent
   `os.makedirs(exist_ok=True)` calls succeed.
3. Added the same defensive block to `upgrade.sh` (between the last
   `chown -R` on `${CONFIG_DIR}` and the Phase 7b database migration), so
   legacy upgrades where the preserved `config.yaml` still references the
   legacy path also boot cleanly on the first post-upgrade service start.

Both scripts pass `bash -n` syntax check.

### 2. Spectrum collector fails to start on canonical install

**Symptom.** After the storage_dir path mismatch was fixed the service
booted first-try, but the journal still contained the warning
`wm1303_api WARNING Failed to start spectrum collector: [Errno 13]
Permission denied: '/var/lib/pymc_repeater'`. The SpectrumCollector never
started, so no SX1261 spectrum-history data was persisted in
`spectrum_history.db`. The service ran normally otherwise because the
crash had been downgraded to a warning by an earlier try/except.

**Root cause.** 40 hardcoded `/var/lib/pymc_repeater` occurrences across
11 overlay Python files (most notably `wm1303_api.py` with 20 hits,
`wm1303_backend.py` with 7 hits, and `debug_collector.py` with 4 hits),
all using the legacy path instead of reading `storage.storage_dir` from
the live `config.yaml`.

**Fix.** Bulk in-place replacement of `/var/lib/pymc_repeater` →
`/var/lib/openhop_repeater` in every overlay `.py` file. 40 hits → 0 hits;
`ast.parse` succeeds for every file. On the reference device the journal
now shows
`SpectrumCollector initialized, db=/var/lib/openhop_repeater/spectrum_history.db`
followed by the polling banner. All other endpoints that touched storage
paths (~20 in `wm1303_api.py` alone) are also normalized as a
side-effect, removing the same class of latent failure everywhere else
in the codebase.

### 3. Upgrade rebuild fails with `capture_thread.c: Permission denied`

**Symptom.** The first real upgrade-path test on the reference device
(after fixes 1 and 2 were in place) failed at Phase 6.4 with
`cc1: fatal error: src/capture_thread.c: Permission denied` and
`inc/capture_thread.h: Permission denied`. The upgrade aborted before
reaching Phase 11 service restart.

**Root cause.** Two layers combined into a real bug:

- **Shipped mode.** 72 overlay files in `overlay/` and `config/` were
  shipped with mode `0600` (`-rw------- root root`), notably
  `overlay/hal/packet_forwarder/src/capture_thread.c`.
- **Trigger.** `upgrade.sh` copies overlay files into `${HAL_DIR}` as
  root (`cp` preserves source mode for newly created files), then
  invokes `sudo -u ${PI_USER} make -C packet_forwarder`. The non-root
  build user cannot read files with `0600 root:root` permissions.
  `install.sh` avoided this in practice because it chowns `${HAL_DIR}`
  to `${PI_USER}` before its build; `upgrade.sh` did not.

**Fix.** Three-part:

1. **Source normalization.** All 72 restrictive files under
   `overlay/` and `config/` normalized to `u+rwX,go+rX` (`chmod -R`),
   so future tarballs and git checkouts ship with sane modes.
2. **Structural guard in `upgrade.sh`** (~line 795, inside the
   `if rebuild` block, before `make clean`): added a new step
   `"Normalizing HAL tree ownership for build user"` that performs
   `chown -R ${PI_USER}:${PI_USER}` + `chmod -R u+rwX,go+rX` on
   `${HAL_DIR}`, mirroring the pattern that `install.sh` already had.
   Any future overlay file with restrictive perms — whether shipped
   that way or introduced later — no longer aborts the upgrade.
3. `bash -n upgrade.sh` passes.

Verified on the reference device that the upgrade retest showed
`[6.1] Normalizing HAL tree ownership for build user ✓ Ownership pi:pi,
modes readable` followed by `[6.5] Building lora_pkt_fwd ✓ Built`, and
the upgrade proceeded through Phase 6 to Phase 11 successfully.

### 4. Systemd `Type=notify` timeout loop

**Symptom.** After a fully successful install or upgrade, `systemctl
status openhop-repeater` reported `ActiveState=activating,
SubState=start` indefinitely, and `NRestarts` climbed by 1 every ~120 s.
HTTP/DB/RX all worked; the daemon had logged its ready banner; but
systemd never saw the unit as `active`. `install.sh` and `upgrade.sh`
reported exit-code 1 at Phase 11.3 `systemctl start` even on runs where
the daemon was fully functional.

**Root cause.** The service unit uses `Type=notify` with
`TimeoutStartSec=120s`, but the daemon never called `sd_notify(READY=1)`
after finishing startup. Systemd therefore treated every start as a
timeout, marked the unit `Failed with result timeout` at the 120 s mark,
and restarted it in a slow infinite loop.

**Fix.** Added a defensive `sd_notify(READY=1)` call to
`overlay/pymc_repeater/repeater/main.py` immediately before the
`"Repeater daemon running (waiting for shutdown signal)"` log line —
the exact point at which the HTTP server and metrics retention have both
been started. The call is guarded by `try`/`except` so that a missing
`python-systemd` binding (e.g. during local development without systemd)
logs a warning instead of crashing the daemon. `main.py` is already in
the install/upgrade overlay copy loop, so no script changes were
needed — the fix is upgrade-safe by design.

Verified on the reference device after deploy: `ActiveState=active`,
`SubState=running`, `NRestarts=0`, and the journal shows
`RepeaterDaemon INFO Sent sd_notify READY=1 (Type=notify readiness
signalled)` immediately followed by the daemon-running banner.
`ast.parse` succeeds.

---

## Files changed

| File | Kind of change |
|------|----------------|
| `VERSION` | Bumped `2.7.3` → `2.7.4` |
| `TODO.md` | Added entries #224 – #227 as completed |
| `config/config.yaml.template` | `storage.storage_dir` and `gps.source_path` normalized to `/var/lib/openhop_repeater` |
| `install.sh` | Defensive `storage_dir` symlink block (Fix 1) |
| `upgrade.sh` | Defensive `storage_dir` symlink block (Fix 1) + HAL tree ownership normalization block (Fix 3) |
| `overlay/pymc_repeater/repeater/main.py` | `sd_notify(READY=1)` at daemon-ready point (Fix 4) |
| `overlay/pymc_repeater/repeater/config.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/metrics_retention.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/data_acquisition/storage_collector.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/web/wm1303_api.py` | Legacy path replaced (Fix 2, 20 hits) |
| `overlay/pymc_repeater/repeater/web/debug_collector.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/web/spectrum_collector.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/web/api_endpoints.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_repeater/repeater/web/update_endpoints.py` | Legacy path replaced (Fix 2) |
| `overlay/pymc_core/src/openhop_core/hardware/wm1303_backend.py` | Legacy path replaced (Fix 2, 7 hits) |
| Various overlay/config files | File-mode normalization to `u+rwX,go+rX` (Fix 3) |

## Upgrade instructions

Existing installations upgrade via the bootstrap one-liner exactly as
before; no manual steps are required. Fresh installations also upgrade
via the bootstrap one-liner and no longer require any post-install
intervention.

## Compatibility

- No changes to configuration file schema, database schema, or on-wire
  protocols. Existing `config.yaml`, `wm1303_ui.json`, `repeater.db`,
  and `spectrum_history.db` files are preserved untouched.
- HAL version remains 2.10.
- Python and system package requirements are unchanged.
