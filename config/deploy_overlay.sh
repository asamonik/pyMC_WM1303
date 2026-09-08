#!/bin/bash
# Shared by install.sh and upgrade.sh. Source this file; it has no side effects.

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

deploy_overlay() {
    local source_dir="$1"
    local target_dir="$2"
    if [ ! -d "$source_dir" ]; then
        printf 'Overlay source is missing: %s\n' "$source_dir" >&2
        return 1
    fi
    mkdir -p "$target_dir" || return 1
    # Keep upstream files that are not overlaid, and omit local build caches.
    # --checksum also repairs files whose size and timestamp happen to match.
    rsync -a --checksum --exclude='__pycache__/' --exclude='*.py[co]' \
        --exclude='.git/' "$source_dir/" "$target_dir/"
}

overlay_diff_count() {
    local source_dir="$1"
    local target_dir="$2"
    local source_file relative count
    [ -d "$source_dir" ] || return 1
    # A failed traversal must not print 0 and pass the deployment check.
    # Keep pipefail local, and preserve NUL-delimited filenames through find.
    count=$(
        set -o pipefail
        find "$source_dir" -type d \( -name __pycache__ -o -name .git \) -prune -o \
            -type f ! -name '*.pyc' ! -name '*.pyo' -print0 | {
            count=0
            while IFS= read -r -d '' source_file; do
                relative="${source_file#"$source_dir/"}"
                if ! cmp -s "$source_file" "$target_dir/$relative"; then
                    count=$((count + 1))
                fi
            done
            printf '%s\n' "$count"
        }
    ) || return 1
    printf '%s\n' "$count"
}

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

install_wm1303_updater() {
    local source_dir="$1" service_user="$2" lib_dir=/usr/local/lib/pymc-wm1303
    local staged_config staged_bootstrap staged_launcher service_uid origin repository
    service_uid=$(id -u "$service_user") || return 1
    [ "$service_uid" != 0 ] || return 1
    [[ "$source_dir" == /* ]] && [ -f "$source_dir/bootstrap.sh" ] || return 1
    origin=$(git -c "safe.directory=$source_dir" -C "$source_dir" remote get-url origin) || return 1
    repository=$(wm1303_repository_slug "$origin") || {
        printf 'WM1303 updater requires a GitHub origin whose repository is pyMC_WM1303.\n' >&2
        return 1
    }
    [ ! -L "$lib_dir" ] || return 1
    install -d -o root -g root -m 755 "$lib_dir" /usr/local/sbin || return 1
    staged_config=$(mktemp "$lib_dir/.updater.conf.XXXXXX") || return 1
    staged_bootstrap=$(mktemp "$lib_dir/.bootstrap.XXXXXX") || return 1
    staged_launcher=$(mktemp /usr/local/sbin/.wm1303-upgrade.XXXXXX) || return 1
    # %q preserves spaces/metacharacters without evaluating either value later.
    printf 'WM1303_UPDATE_USER=%q\nWM1303_UPDATE_CHECKOUT=%q\nWM1303_UPDATE_REPOSITORY=%q\n' \
        "$service_user" "$source_dir" "$repository" > "$staged_config" || return 1
    chown root:root "$staged_config" || return 1
    chmod 600 "$staged_config" || return 1
    install -o root -g root -m 755 "$source_dir/bootstrap.sh" "$staged_bootstrap" || return 1
    install -o root -g root -m 755 "$source_dir/config/wm1303-upgrade" "$staged_launcher" || return 1
    # Atomic replacements also preserve the running bootstrap's open inode.
    mv -f -- "$staged_config" "$lib_dir/updater.conf" || return 1
    mv -f -- "$staged_bootstrap" "$lib_dir/bootstrap.sh" || return 1
    mv -f -- "$staged_launcher" /usr/local/sbin/wm1303-upgrade
}
