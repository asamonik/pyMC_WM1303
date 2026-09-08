#!/bin/bash
# =============================================================================
# pyMC_WM1303 One-Line Installer / Upgrade Bootstrap (with Region+Preset Wizard)
# =============================================================================
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/HansvanMeer/pyMC_WM1303/main/bootstrap.sh | sudo bash
#
# Optional environment overrides (skip interactive prompts):
#   WM1303_REGION=EU868|US915|AU915|AS923|IN865|JP920|KR920|CUSTOM
#   WM1303_PRESET=<preset-name-from-presets.json>
#   WM1303_SYNC_WORD=private|public   (device-wide LoRa network sync word;
#                                      default: private)
#
# Optional flag:
#   --non-interactive   skip all wizard prompts, fall back to EU-Default preset
#   --user=<name>       preserve the installed non-root service user
# INSTALL_DIR may select an existing integration checkout (used by the updater).
# WM1303_REPO_URL may select a GitHub fork named pyMC_WM1303; never another project.
#
# This script:
#   1. Installs git + jq (if not present)
#   2. Clones or updates the pyMC_WM1303 repository
#   3. On NEW install only: runs an interactive wizard to choose region + preset
#      and writes /etc/openhop_repeater/wm1303_ui.json with those defaults.
#   4. Detects existing installation:
#      - New install: runs install.sh
#      - Existing install: runs upgrade.sh (wizard is SKIPPED to preserve config)
# =============================================================================

set -e

wm1303_repository_slug() {
    local slug="${1,,}"
    slug="${slug%/}"
    slug="${slug%.git}"
    case "$slug" in
        https://github.com/*) slug="${slug#https://github.com/}" ;;
        git@github.com:*) slug="${slug#git@github.com:}" ;;
        ssh://git@github.com/*) slug="${slug#ssh://git@github.com/}" ;;
        *) return 1 ;;
    esac
    [[ "$slug" =~ ^[a-z0-9][a-z0-9-]*/pymc_wm1303$ ]] || return 1
    printf '%s/pyMC_WM1303\n' "${slug%/*}"
}

WM1303_REPOSITORY=$(wm1303_repository_slug "${WM1303_REPO_URL:-https://github.com/HansvanMeer/pyMC_WM1303.git}") || {
    echo 'WM1303_REPO_URL must name a GitHub owner/pyMC_WM1303 repository.' >&2
    exit 1
}
REPO_URL="https://github.com/${WM1303_REPOSITORY}.git"
BOOTSTRAP_RAW_URL="https://raw.githubusercontent.com/${WM1303_REPOSITORY}/main/bootstrap.sh"
CONFIG_DIR="/etc/openhop_repeater"
UI_JSON="${CONFIG_DIR}/wm1303_ui.json"

# Keep this small check inline: bootstrap also runs before a checkout exists.
require_expected_origin() {
    local target_dir="$1" expected_url="$2" origin slug expected_slug
    origin=$(git -c "safe.directory=${target_dir}" -C "${target_dir}" remote get-url origin 2>/dev/null) || {
        printf 'Cannot read origin for %s; repository was not changed.\n' "${target_dir}" >&2
        return 1
    }
    # GitHub owner/repository names are case-insensitive. Accept its usual
    # HTTPS, scp-style SSH and ssh:// spellings, with or without .git.
    slug="${origin,,}"
    slug="${slug%/}"
    slug="${slug%.git}"
    case "${slug}" in
        https://github.com/*) slug="${slug#https://github.com/}" ;;
        git@github.com:*) slug="${slug#git@github.com:}" ;;
        ssh://git@github.com/*) slug="${slug#ssh://git@github.com/}" ;;
        *) slug="" ;;
    esac
    expected_slug="${expected_url,,}"
    expected_slug="${expected_slug#https://github.com/}"
    expected_slug="${expected_slug%.git}"
    if [ "${slug}" != "${expected_slug}" ]; then
        # Do not print the actual URL: it might contain embedded credentials.
        printf 'Unexpected origin for %s. Expected %s (HTTPS or SSH). Repository and remote were not changed; inspect origin before retrying.\n' \
            "${target_dir}" "${expected_url}" >&2
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Detect target user
# Priority: existing service > SUDO_USER > common users > first regular account
# ---------------------------------------------------------------------------
_detect_user() {
    local service_file service_user
    for service_file in /etc/systemd/system/openhop-repeater.service /etc/systemd/system/pymc-repeater.service; do
        if [ -f "$service_file" ]; then
            service_user=$(sed -n 's/^User=//p' "$service_file" | head -n1)
            if [ -n "$service_user" ] && [ "$service_user" != root ] && id "$service_user" &>/dev/null; then
                echo "$service_user"; return
            fi
        fi
    done
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && id "$SUDO_USER" &>/dev/null; then
        echo "$SUDO_USER"; return
    fi
    for candidate in pi orangepi radxa rock dietpi; do
        if id "$candidate" &>/dev/null; then echo "$candidate"; return; fi
    done
    local found
    found=$(awk -F: '$3 >= 1000 && $3 < 65534 && $6 != "/" && $7 !~ /nologin|false/ {print $1; exit}' /etc/passwd)
    if [ -n "$found" ] && id "$found" &>/dev/null; then echo "$found"; return; fi
    echo ""
}

BOOTSTRAP_USER=""
for arg in "$@"; do
    case "$arg" in
        --user=*) BOOTSTRAP_USER="${arg#--user=}" ;;
        --non-interactive|-y) ;;
        *) echo "  ✗ Unknown argument: $arg" >&2; exit 2 ;;
    esac
done
PI_USER="${BOOTSTRAP_USER:-$(_detect_user)}"
if [ -z "$PI_USER" ]; then
    echo "  ✗ Could not detect a non-root user. Create one first or run install.sh with --user=<name>."
    exit 1
fi
if ! id "$PI_USER" &>/dev/null || [ "$(id -u "$PI_USER")" = 0 ]; then
    echo "  ✗ The selected service user must be an existing non-root account." >&2
    exit 1
fi
# Resolve the account database without evaluating the account name.
PI_GROUP=$(id -gn "${PI_USER}")
PI_HOME=$(getent passwd "${PI_USER}" 2>/dev/null | cut -d: -f6)
if [ -z "${PI_HOME}" ] || [ "${PI_HOME}" = "~${PI_USER}" ]; then
    echo "  ✗ Could not determine home directory for user '${PI_USER}'."
    exit 1
fi
if [ ! -d "${PI_HOME}" ]; then
    echo "  ✗ Home directory '${PI_HOME}' for user '${PI_USER}' does not exist."
    exit 1
fi
INSTALL_DIR="${INSTALL_DIR:-${PI_HOME}/pyMC_WM1303}"
if [[ "$INSTALL_DIR" != /* ]] || [ "$INSTALL_DIR" = / ] || [ "$INSTALL_DIR" = "$PI_HOME" ]; then
    echo "  ✗ INSTALL_DIR must be an absolute, dedicated repository path." >&2
    exit 1
fi

# --- Early parse of --non-interactive flag and WM1303_REGION env var ---------
# Must happen BEFORE self-reexec so we can skip reexec when not needed.
_EARLY_NON_INTERACTIVE=0
for arg in "$@"; do
    if [ "${arg}" = "--non-interactive" ] || [ "${arg}" = "-y" ]; then
        _EARLY_NON_INTERACTIVE=1
    fi
done
# If WM1303_REGION is set, the user already chose a region — no need for interactive wizard
if [ -n "${WM1303_REGION}${WM1303_PRESET}${WM1303_SYNC_WORD}" ]; then
    _EARLY_NON_INTERACTIVE=1
fi

# --- Self-reexec for interactive wizard when piped from curl -----------------
# When run as `curl ... | sudo bash`, stdin is the pipe (no TTY), so the
# interactive wizard cannot prompt the user. Fix: download ourselves to a temp
# file and re-exec with the real TTY as stdin.
# Skip reexec if: already reexeced, non-interactive requested, or region preset via env.
if [ ! -t 0 ] && [ -z "${_WM1303_REEXEC}" ] && [ "${_EARLY_NON_INTERACTIVE}" -eq 0 ]; then
    _SELF=$(mktemp /tmp/wm1303_bootstrap.XXXXXX.sh)
    _DOWNLOADED=0
    # We're being piped — download a fresh copy for re-execution
    if command -v curl &>/dev/null; then
        if curl -fsSL "${BOOTSTRAP_RAW_URL}" -o "${_SELF}" 2>/dev/null; then
            _DOWNLOADED=1
        fi
    elif command -v wget &>/dev/null; then
        if wget -qO "${_SELF}" "${BOOTSTRAP_RAW_URL}" 2>/dev/null; then
            _DOWNLOADED=1
        fi
    fi
    # Only reexec if /dev/tty is truly readable (not just exists)
    if [ "${_DOWNLOADED}" -eq 1 ] && [ -s "${_SELF}" ] && [ -r /dev/tty ] && bash -c 'echo ok </dev/tty' &>/dev/null; then
        chmod +x "${_SELF}"
        export _WM1303_REEXEC="${_SELF}"
        exec bash "${_SELF}" "$@" </dev/tty
    else
        # /dev/tty not usable — clean up and continue non-interactive
        rm -f "${_SELF}" 2>/dev/null
    fi
fi
# Remove the downloaded script after the new shell has opened it.
case "${_WM1303_REEXEC:-}" in
    /tmp/wm1303_bootstrap.*.sh) rm -f -- "${_WM1303_REEXEC}" ;;
esac
unset _WM1303_REEXEC 2>/dev/null || true

# --- Parse --non-interactive flag --------------------------------------------
NON_INTERACTIVE=0
for arg in "$@"; do
    if [ "${arg}" = "--non-interactive" ] || [ "${arg}" = "-y" ]; then
        NON_INTERACTIVE=1
    fi
done
# If WM1303_REGION is set via env, also non-interactive
if [ -n "${WM1303_REGION}${WM1303_PRESET}${WM1303_SYNC_WORD}" ]; then
    NON_INTERACTIVE=1
fi
# If stdin is STILL not a TTY after reexec attempt, fall back to non-interactive.
if [ ! -t 0 ]; then
    NON_INTERACTIVE=1
fi

echo ""
echo "  ╔══════════════════════════════════════════════════════════╗"
echo "  ║     pyMC_WM1303 Bootstrap                                ║"
echo "  ╚══════════════════════════════════════════════════════════╝"
echo ""

# Ensure running as root
if [ "$(id -u)" -ne 0 ]; then
    echo "  ✗ This script must be run as root (use sudo)"
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive

# Install git if not present
if ! command -v git &>/dev/null; then
    echo "  ℹ Installing git..."
    apt-get update -qq
    apt-get install -y -qq git
    echo "  ✓ git installed"
else
    echo "  ✓ git already available"
fi

# Install jq if not present (used by the wizard for JSON merging)
if ! command -v jq &>/dev/null; then
    echo "  ℹ Installing jq..."
    apt-get update -qq
    apt-get install -y -qq jq
    echo "  ✓ jq installed"
else
    echo "  ✓ jq already available"
fi

# Check before modifying the checkout, ownership, or persistent git settings.
if [ -e "${INSTALL_DIR}/.git" ]; then
    require_expected_origin "${INSTALL_DIR}" "${REPO_URL}" || exit 1
fi

# Fix git 'dubious ownership' error (CVE-2022-24765)
git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null
sudo -u "${PI_USER}" git config --global --add safe.directory "${INSTALL_DIR}" 2>/dev/null

# Clone or update repository
if [ -e "${INSTALL_DIR}/.git" ]; then
    echo "  ℹ Repository already exists, pulling latest changes..."
    chown -R "${PI_USER}:${PI_GROUP}" "${INSTALL_DIR}"
    cd "${INSTALL_DIR}"
    if [ "${WM1303_UPDATE_JOB:-}" = 1 ]; then
        # The detached service has no interactive SSH agent. Fetch the same
        # validated fork over HTTPS without changing its configured origin.
        # An explicit destination keeps the reset below on the fetched main.
        sudo -u "${PI_USER}" env GIT_TERMINAL_PROMPT=0 git fetch "${REPO_URL}" \
            '+refs/heads/main:refs/remotes/origin/main'
    else
        sudo -u "${PI_USER}" git fetch origin
    fi
    CHECKOUT_STATUS=$(sudo -u "${PI_USER}" git status --porcelain) || {
        echo "  ✗ Cannot read repository status; local changes were not reset or cleaned." >&2
        exit 1
    }
    if [ -n "${CHECKOUT_STATUS}" ]; then
        sudo -u "${PI_USER}" git stash push --include-untracked -m "wm1303 bootstrap backup $(date -Iseconds)"
        echo "  ℹ Local changes preserved in git stash before updating"
    fi
    sudo -u "${PI_USER}" git reset --hard origin/main
    sudo -u "${PI_USER}" git clean -fd
    echo "  ✓ Repository updated"
else
    echo "  ℹ Cloning repository..."
    sudo -u "${PI_USER}" git clone "${REPO_URL}" "${INSTALL_DIR}"
    echo "  ✓ Repository cloned"
fi

cd "${INSTALL_DIR}"

# ---------------------------------------------------------------------------
# Detect existing installation up-front (we need this for wizard skip logic)
# ---------------------------------------------------------------------------
IS_UPGRADE=0
# Detect an existing install via either the new openhop-repeater unit or the
# legacy pymc-repeater unit (pre-openhop migration).
if [ -d "/opt/pymc_repeater" ] && {
    [ -f "${CONFIG_DIR}/config.yaml" ] || [ -f "/etc/pymc_repeater/config.yaml" ] ||
    systemctl is-enabled openhop-repeater &>/dev/null || systemctl is-enabled pymc-repeater &>/dev/null;
}; then
    IS_UPGRADE=1
fi

# ---------------------------------------------------------------------------
# Wizard: choose regulatory region + channel preset (new installs only)
# ---------------------------------------------------------------------------
run_wizard() {
    local presets_file="${INSTALL_DIR}/config/presets.json"
    if [ ! -f "${presets_file}" ]; then
        echo "  ⚠ presets.json not found at ${presets_file}, skipping wizard"
        return 0
    fi

    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  Installation Wizard: Region + Preset selection          ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""

    # Track whether the user supplied ANY override env var. If none were set
    # and we're running non-interactively, we'll show a clear summary warning
    # at the end with the full override syntax (issue #7 Bug 1).
    local _all_defaulted=1
    [ -n "${WM1303_REGION}" ] && _all_defaulted=0
    [ -n "${WM1303_PRESET}" ] && _all_defaulted=0
    [ -n "${WM1303_SYNC_WORD}" ] && _all_defaulted=0

    # Build region list from presets.json (unique region codes)
    local regions
    regions=$(jq -r '[.presets[].region] | unique | sort_by(. != "EU868", .)[]' "${presets_file}")
    if [ -z "${regions}" ]; then
        echo "  ✗ No regions found in presets.json" >&2
        return 1
    fi

    if [ -n "${WM1303_PRESET}" ]; then
        local preset_region
        preset_region=$(jq -r --arg n "${WM1303_PRESET}" \
            '.presets[] | select(.name==$n or .region==$n) | .region' "${presets_file}" | head -n1)
        if [ -z "$preset_region" ]; then
            echo "  ✗ Unknown WM1303_PRESET: ${WM1303_PRESET}" >&2
            return 1
        fi
        if [ -n "${WM1303_REGION}" ] && [ "${WM1303_REGION}" != "$preset_region" ]; then
            echo "  ✗ WM1303_PRESET does not belong to WM1303_REGION" >&2
            return 1
        fi
        WM1303_REGION=$preset_region
    fi
    if [ -n "${WM1303_REGION}" ] && ! jq -e --arg r "${WM1303_REGION}" \
        'any(.presets[]; .region == $r)' "${presets_file}" >/dev/null; then
        echo "  ✗ Unknown WM1303_REGION: ${WM1303_REGION}" >&2
        return 1
    fi

    # --- Region selection -------------------------------------------------
    if [ -n "${WM1303_REGION}" ]; then
        echo "  ✓ Region from env: ${WM1303_REGION}"
    elif [ "${NON_INTERACTIVE}" -eq 1 ]; then
        WM1303_REGION="EU868"
        echo ""
        echo "  ╔══════════════════════════════════════════════════════════╗"
        echo "  ║  ⚠  NON-INTERACTIVE MODE                                ║"
        echo "  ║  Region defaulting to EU868.                             ║"
        echo "  ║  To set a different region, re-run with:                 ║"
        echo "  ║    curl -sSL ... | sudo env WM1303_REGION=AU915 bash     ║"
        echo "  ║  Supported: EU868 US915 AU915 AS923 IN865 JP920 KR920    ║"
        echo "  ╚══════════════════════════════════════════════════════════╝"
        echo ""
    else
        echo "  Available regions:"
        local i=1
        local region_arr=()
        while IFS= read -r rg; do
            region_arr+=("${rg}")
            echo "    ${i}) ${rg}"
            i=$((i+1))
        done <<< "${regions}"
        echo ""
        read -rp "  Select region [1-${#region_arr[@]}, default 1=EU868]: " reg_choice
        reg_choice=${reg_choice:-1}
        if ! [[ "${reg_choice}" =~ ^[0-9]+$ ]] || [ "${reg_choice}" -lt 1 ] || [ "${reg_choice}" -gt "${#region_arr[@]}" ]; then
            echo "  ⚠ Invalid choice, defaulting to EU868"
            WM1303_REGION="EU868"
        else
            WM1303_REGION="${region_arr[$((reg_choice-1))]}"
        fi
    fi
    echo "  ► Selected region: ${WM1303_REGION}"

    # --- Preset auto-select (1 preset per region in presets v3) -----------
    # Presets v3 contain only region + rf_center_freq_mhz, no channel configs.
    # Preset name matches region code, so we auto-select.
    WM1303_PRESET="${WM1303_PRESET:-${WM1303_REGION}}"
    local preset_desc
    preset_desc=$(jq -r --arg n "${WM1303_PRESET}" \
        '.presets[] | select(.name==$n or .region==$n) | .description // ""' \
        "${presets_file}" | head -n1)
    if [ -n "${preset_desc}" ]; then
        echo "  ► Region preset: ${WM1303_PRESET} — ${preset_desc}"
    else
        echo "  ► Region preset: ${WM1303_PRESET}"
    fi

    # --- Device-wide LoRa network Sync Word ------------------------------
    # WM1303_SYNC_WORD env var accepts only 'private' or 'public'.
    # SX1302 hardware supports ONLY these two values via the board-level
    # lorawan_public flag (lgw_conf_board_t). Custom sync words are NOT
    # hardware-supported and any invalid value falls back to Private.
    WM1303_SYNC_WORD_MODE="private"
    WM1303_SYNC_WORD_VALUE=5156   # 0x1424 Private
    if [ -n "${WM1303_SYNC_WORD}" ]; then
        local _sw_in
        _sw_in=$(echo "${WM1303_SYNC_WORD}" | tr '[:upper:]' '[:lower:]')
        case "${_sw_in}" in
            private)
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
            public)
                WM1303_SYNC_WORD_MODE="public"; WM1303_SYNC_WORD_VALUE=13380 ;;
            *)
                echo "  ⚠ Invalid WM1303_SYNC_WORD='${WM1303_SYNC_WORD}' (only 'private' or 'public' supported), falling back to Private (0x1424)" ;;
        esac
        echo "  ✓ Sync word from env: ${WM1303_SYNC_WORD_MODE} (value=${WM1303_SYNC_WORD_VALUE})"
    elif [ "${NON_INTERACTIVE}" -eq 1 ]; then
        echo "  ℹ Non-interactive mode: defaulting sync word to Private (0x1424)"
    else
        echo ""
        echo "  LoRa Network Sync Word (device-wide):"
        echo "    1) Private (0x1424, default)  [most networks incl. MeshCore]"
        echo "    2) Public  (0x3444)           [LoRaWAN public networks]"
        echo ""
        read -rp "  Select sync word [1-2, default 1=Private]: " sw_choice
        sw_choice=${sw_choice:-1}
        case "${sw_choice}" in
            1)
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
            2)
                WM1303_SYNC_WORD_MODE="public"; WM1303_SYNC_WORD_VALUE=13380 ;;
            *)
                echo "  ⚠ Invalid choice, falling back to Private (0x1424)"
                WM1303_SYNC_WORD_MODE="private"; WM1303_SYNC_WORD_VALUE=5156 ;;
        esac
    fi
    printf "  ► Selected sync word: %s (0x%04X)\n" "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"

    # Issue #7 Bug 1 — Final summary warning when running fully unattended.
    # If the install is non-interactive AND the user did not supply any env
    # var, print a single prominent block showing what was defaulted and the
    # exact override command for next time. Keeps headless installs easy to
    # diagnose when the wrong region is silently selected.
    if [ "${NON_INTERACTIVE}" -eq 1 ] && [ "${_all_defaulted}" -eq 1 ]; then
        echo ""
        echo "  ╔══════════════════════════════════════════════════════════════╗"
        echo "  ║  ⚠  NON-INTERACTIVE DEFAULTS APPLIED                         ║"
        echo "  ║                                                              ║"
        printf "  ║    Region:    %-46s ║\n" "${WM1303_REGION}"
        printf "  ║    Preset:    %-46s ║\n" "${WM1303_PRESET}"
        printf "  ║    Sync word: %-46s ║\n" "${WM1303_SYNC_WORD_MODE} (0x$(printf %04X "${WM1303_SYNC_WORD_VALUE}"))"
        echo "  ║                                                              ║"
        echo "  ║  To install for a different region, re-run with:             ║"
        echo "  ║    curl -sSL https://raw.githubusercontent.com/HansvanMeer/ ║"
        echo "  ║      pyMC_WM1303/main/bootstrap.sh |                        ║"
        echo "  ║      sudo env WM1303_REGION=AU915 bash                     ║"
        echo "  ║                                                              ║"
        echo "  ║  Supported: EU868 US915 AU915 AS923 IN865 JP920 KR920 CUSTOM ║"
        echo "  ╚══════════════════════════════════════════════════════════════╝"
        echo ""
    fi

    write_wizard_config
}

# Write wizard output to /etc/openhop_repeater/wm1303_ui.json.
# Presets v3 contain only region + rf_center_freq_mhz (no channel configs).
# All channels start disabled — the user configures them via the UI after
# installation. The template wm1303_ui.json provides the skeleton with
# disabled channel defaults.
write_wizard_config() {
    local presets_file="${INSTALL_DIR}/config/presets.json"
    local template="${INSTALL_DIR}/config/wm1303_ui.json"
    if [ ! -f "${template}" ]; then
        echo "  ⚠ Template wm1303_ui.json missing, cannot write wizard config"
        return 0
    fi
    mkdir -p "${CONFIG_DIR}"

    # Start from template (all channels disabled)
    cp "${template}" "${UI_JSON}"

    # Extract rf_center_freq_mhz from the chosen preset (if available)
    local rf_center="null"
    if [ -f "${presets_file}" ]; then
        local _rc
        _rc=$(jq -r --arg n "${WM1303_PRESET:-${WM1303_REGION}}" \
            '.presets[] | select(.name==$n or .region==$n) | .rf_center_freq_mhz // empty' \
            "${presets_file}" | head -n1)
        if [ -n "${_rc}" ] && [ "${_rc}" != "null" ]; then
            rf_center="${_rc}"
        fi
    fi

    # Merge region, sync_word, and rf_center_freq_mhz into the config
    jq --arg code "${WM1303_REGION}" \
       --arg sw_mode "${WM1303_SYNC_WORD_MODE}" \
       --argjson sw_value "${WM1303_SYNC_WORD_VALUE}" \
       --argjson rf_center "${rf_center}" \
       '.region = {"code": $code, "tx_freq_min": null, "tx_freq_max": null}
        | .sync_word = {"value": $sw_value, "mode": $sw_mode}
        | if $rf_center != null then .rf_center_freq_mhz = $rf_center else . end' \
       "${UI_JSON}" > "${UI_JSON}.tmp" && mv "${UI_JSON}.tmp" "${UI_JSON}"

    chown "${PI_USER}:${PI_GROUP}" "${UI_JSON}"
    if [ "${rf_center}" != "null" ]; then
        printf "  ✓ Wrote %s — region=%s, rf_center=%.3f MHz, sync_word=%s (0x%04X)\n" \
            "${UI_JSON}" "${WM1303_REGION}" "${rf_center}" \
            "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"
    else
        printf "  ✓ Wrote %s — region=%s, sync_word=%s (0x%04X)\n" \
            "${UI_JSON}" "${WM1303_REGION}" \
            "${WM1303_SYNC_WORD_MODE}" "${WM1303_SYNC_WORD_VALUE}"
    fi
    echo "  ℹ All channels start disabled. Configure channels via the WM1303 Manager UI."
}

if [ "${IS_UPGRADE}" -eq 0 ]; then
    if [ -f "${UI_JSON}" ]; then
        echo "  ℹ Existing ${UI_JSON} found — skipping wizard to preserve config"
    else
        run_wizard
    fi
else
    echo "  ℹ Upgrade detected — wizard skipped, existing config preserved"
fi

# Legacy data migration is handled by install.sh/upgrade.sh after services stop.
# Moving live SQLite files here can lose WAL transactions and corrupt backups.

# ---------------------------------------------------------------------------
# SSH timeout protection: run install/upgrade with nohup so the process
# survives if the SSH session disconnects (HAL build can take >10 minutes).
# Output is logged and tailed so the user still sees live progress.
# ---------------------------------------------------------------------------
BOOTSTRAP_LOG=$(mktemp /tmp/wm1303_bootstrap.XXXXXX.log)

run_protected() {
    local script="$1"
    if [ "${WM1303_UPDATE_JOB:-}" = 1 ]; then
        # Already detached by systemd. Stream every line directly into its
        # persistent log, including fast failures and final completion output.
        bash "${script}" "--user=${PI_USER}"
        return "$?"
    fi
    echo "  ℹ Running ${script} (nohup-protected against SSH timeout)..."
    echo "  ℹ Log: ${BOOTSTRAP_LOG}"
    local bg_pid tail_pid exit_code
    nohup bash "${script}" "--user=${PI_USER}" > "${BOOTSTRAP_LOG}" 2>&1 &
    bg_pid=$!
    tail -f "${BOOTSTRAP_LOG}" &
    tail_pid=$!
    # Guard wait explicitly: set -e must not bypass tail cleanup on failure.
    if wait "$bg_pid"; then
        exit_code=0
    else
        exit_code=$?
    fi
    kill "$tail_pid" 2>/dev/null || true
    wait "$tail_pid" 2>/dev/null || true
    return "$exit_code"
}

if [ "${IS_UPGRADE}" -eq 1 ]; then
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  Existing installation detected — running UPGRADE        ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    run_protected upgrade.sh
else
    echo ""
    echo "  ╔══════════════════════════════════════════════════════════╗"
    echo "  ║  No installation found — running INSTALL                 ║"
    echo "  ╚══════════════════════════════════════════════════════════╝"
    echo ""
    run_protected install.sh
fi
