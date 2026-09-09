"""Local regression checks; never invoke installation or real GPIO operations."""

import ast
import importlib.util
import json
import logging
import os
from pathlib import Path
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def shell_function(script, name):
    text = (ROOT / script).read_text()
    start = text.index(name + "() {")
    return text[start : text.index("\n}\n", start) + 3]


def python_block(script, delimiter):
    text = (ROOT / script).read_text()
    match = re.search(r"<<\s*'?" + delimiter + r"'?[^\n]*\n(.*?)\n" + delimiter, text, re.S)
    if match is None:
        raise AssertionError(f"Missing {delimiter} block in {script}")
    return match.group(1)


@unittest.skipUnless(shutil.which("rsync"), "rsync is required for deployment tests")
class OverlayDeploymentTests(unittest.TestCase):
    def run_helper(self, command, *args):
        helper = ROOT / "config/deploy_overlay.sh"
        return subprocess.run(
            ["bash", "-eu", "-c", 'source "$1"; shift; ' + command, "test", str(helper), *map(str, args)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def test_complete_overlay_deploys_and_preserves_upstream_files(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "deployment"
            target.mkdir()
            (target / "upstream_only.py").write_text("# upstream\n")
            source = ROOT / "overlay/pymc_repeater/repeater"
            self.run_helper('deploy_overlay "$1" "$2"', source, target)
            for file in source.rglob("*"):
                if file.is_file() and "__pycache__" not in file.parts and file.suffix not in (".pyc", ".pyo"):
                    self.assertEqual(file.read_bytes(), (target / file.relative_to(source)).read_bytes())
            self.assertTrue((target / "upstream_only.py").exists())
            self.assertTrue((target / "data_acquisition/storage_collector.py").exists())
            self.assertEqual(self.run_helper('overlay_diff_count "$1" "$2"', source, target), "0")

    def test_same_timestamp_changes_are_deployed_and_caches_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            source.mkdir()
            target.mkdir()
            (source / "module.py").write_text("new")
            (target / "module.py").write_text("old")
            timestamp = (source / "module.py").stat().st_mtime
            os.utime(target / "module.py", (timestamp, timestamp))
            (source / "__pycache__").mkdir()
            (source / "__pycache__/module.pyc").write_bytes(b"stale bytecode")
            self.assertEqual(self.run_helper('overlay_diff_count "$1" "$2"', source, target), "1")
            self.run_helper('deploy_overlay "$1" "$2"', source, target)
            self.assertEqual((target / "module.py").read_text(), "new")
            self.assertFalse((target / "__pycache__").exists())


class InstallerControlFlowTests(unittest.TestCase):
    def test_upgrade_repairs_missing_repeater_distribution_without_source_changes(self):
        source = (ROOT / "upgrade.sh").read_text()
        source = source.split("    # A runnable Python alone", 1)[1].split("fi  # end VENV_REBUILD_NEEDED", 1)[0]
        source = "    # A runnable Python alone" + source
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pyMC_Repeater").mkdir()
            calls = root / "calls"
            mocks = '''
step() { :; }
ok() { :; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
sudo() {
    shift 2
    printf '%s\\n' "$*" >> "$CALLS"
    case "$1" in
        */bin/python3) return "$METADATA_STATUS" ;;
        */bin/pip) return "$PIP_STATUS" ;;
        *) return 99 ;;
    esac
}
'''
            for metadata_status, pip_status in (("0", "0"), ("1", "0"), ("1", "1")):
                calls.write_text("")
                result = subprocess.run(["bash", "-eu", "-c", mocks + source], capture_output=True, text=True,
                                        env=dict(os.environ, REPEATER_UPDATED="false", FORCE_REBUILD="false",
                                                 REPO_DIR=str(root), VENV_DIR=str(root / "venv"), PI_USER="service-user",
                                                 LOG_FILE=str(root / "log"), CALLS=str(calls),
                                                 METADATA_STATUS=metadata_status, PIP_STATUS=pip_status))
                self.assertEqual(result.returncode, int(pip_status), result.stderr)
                commands = calls.read_text().splitlines()
                self.assertIn('bin/python3 -I -c from importlib.metadata import version; version("openhop_repeater")', commands[0])
                self.assertEqual(len(commands), 1 if metadata_status == "0" else 2)
                if metadata_status != "0":
                    self.assertTrue(commands[1].endswith("bin/pip --no-input install -e ."))

    def test_legacy_migration_failures_stop_before_startup(self):
        helper = shell_function("install.sh", "_migrate_legacy_vardir")
        self.assertEqual(helper, shell_function("upgrade.sh", "_migrate_legacy_vardir"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, target = root / "legacy", root / "target"
            legacy.mkdir()
            target.mkdir()
            (legacy / "history.db").write_bytes(b"history")
            for nonempty in (False, True):
                if nonempty:
                    (target / "new.db").write_bytes(b"new")
                script = '''
ok() { :; }
fail() { printf '%s\\n' "$*" >&2; exit 1; }
rsync() { return 23; }
cp() { return 1; }
''' + helper + '\n_migrate_legacy_vardir "$1" "$2" "data dir"\nprintf continued'
                result = subprocess.run(["bash", "-eu", "-c", script, "test", str(legacy), str(target)],
                                        capture_output=True, text=True, env=dict(os.environ, LOG_FILE=str(root / "log")))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("failed", result.stderr)
                self.assertNotIn("continued", result.stdout)
                self.assertEqual((legacy / "history.db").read_bytes(), b"history")

    def test_detached_updater_assembles_fixed_job_and_reports_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted, checkout = root / "trusted", root / "checkout with spaces"
            trusted.mkdir()
            (checkout / ".git").mkdir(parents=True)
            (trusted / "bootstrap.sh").write_text("exit 99\n")  # Never executed.
            (trusted / "updater.conf").write_text(
                "WM1303_UPDATE_USER=service-user\nWM1303_UPDATE_REPOSITORY=example/pyMC_WM1303\n"
                + "WM1303_UPDATE_CHECKOUT=" + shlex.quote(str(checkout)) + "\n")
            log = root / "update.log"
            args, calls = root / "args", root / "calls"
            source = (ROOT / "config/wm1303-upgrade").read_text()
            source = source.replace("/usr/local/lib/pymc-wm1303", str(trusted))
            source = source.replace("/var/log/wm1303-update.log", str(log))
            source = source.replace("/run/lock/pymc-wm1303", str(root / "lock"))
            source = source.replace("export PATH=/usr/sbin:/usr/bin:/sbin:/bin", "export PATH=" + shlex.quote(os.environ["PATH"]))
            mocks = '''
id() { if [ "$#" = 1 ]; then printf '0\\n'; else printf '1001\\n'; fi; }
stat() { if [ "$2" = '%u' ]; then printf '0\\n'; else command stat "$@"; fi; }
flock() { return "$FLOCK_STATUS"; }
systemctl() {
    printf '%s\\n' "$*" >> "$CONTROL_LOG"
    if [ "$1" = show ]; then printf '%s\\n' "$MOCK_STATE"; return "$SHOW_STATUS"; fi
}
systemd-run() { printf '%s\\n' "$@" > "$ARGS_LOG"; return "$RUN_STATUS"; }
'''
            env = dict(os.environ, CONTROL_LOG=str(calls), ARGS_LOG=str(args),
                       FLOCK_STATUS="0", SHOW_STATUS="4", RUN_STATUS="0",
                       MOCK_STATE="LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0")

            def invoke(operation, **overrides):
                return subprocess.run(["bash", "-c", mocks + source, "test", operation],
                                      env=dict(env, **overrides), capture_output=True, text=True)

            result = invoke("status")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Repository=example/pyMC_WM1303", result.stdout)
            result = invoke("start")
            self.assertEqual(result.returncode, 0, result.stderr)
            argv = args.read_text().splitlines()
            self.assertIn("--property=Type=oneshot", argv)
            self.assertIn("--property=RemainAfterExit=yes", argv)
            self.assertIn("--property=TimeoutStartSec=0", argv)
            self.assertIn("--setenv=INSTALL_DIR=" + str(checkout), argv)
            self.assertIn("--setenv=WM1303_REPO_URL=https://github.com/example/pyMC_WM1303.git", argv)
            self.assertIn("--setenv=WM1303_UPDATE_JOB=1", argv)
            self.assertEqual(argv[-3:], [str(trusted / "bootstrap.sh"), "--non-interactive", "--user=service-user"])
            log.write_text("keep previous log\n")
            for overrides in ({"FLOCK_STATUS": "1"}, {"SHOW_STATUS": "0", "MOCK_STATE": "LoadState=loaded\nActiveState=activating\nSubState=start"}):
                result = invoke("start", **overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(log.read_text(), "keep previous log\n")
            result = invoke("start", SHOW_STATUS="0", MOCK_STATE="LoadState=loaded\nActiveState=active\nSubState=exited")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("stop wm1303-update.service", calls.read_text())
            self.assertEqual(log.read_text(), "")
            result = invoke("start", RUN_STATUS="1")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not start", result.stderr)
            log.write_text("\n".join(str(number) for number in range(600)) + "\n")
            self.assertEqual(invoke("log").stdout.splitlines(), [str(number) for number in range(100, 600)])
            self.assertNotEqual(invoke("arbitrary-command").returncode, 0)

    def test_updater_install_captures_fork_and_shell_quotes_paths(self):
        source = shell_function("config/deploy_overlay.sh", "wm1303_repository_slug")
        self.assertEqual(source, shell_function("bootstrap.sh", "wm1303_repository_slug"))
        source += shell_function("config/deploy_overlay.sh", "install_wm1303_updater")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / 'checkout with spaces $and "quotes"'
            (checkout / "config").mkdir(parents=True)
            (checkout / "bootstrap.sh").write_text("exit 99\n")
            (checkout / "config/wm1303-upgrade").write_text("exit 99\n")
            lib, sbin = root / "lib", root / "sbin"
            source = source.replace("/usr/local/lib/pymc-wm1303", str(lib)).replace("/usr/local/sbin", str(sbin))
            mocks = '''
id() { printf '1001\\n'; }
git() { printf 'git@github.com:Example/pyMC_WM1303.git\\n'; }
chown() { :; }
install() {
    local args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in -o|-g) shift 2;; *) args+=("$1"); shift;; esac
    done
    command install "${args[@]}"
}
'''
            result = subprocess.run(["bash", "-eu", "-c", mocks + source + '\ninstall_wm1303_updater "$1" service-user', "test", str(checkout)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(["bash", "-eu", "-c", 'source "$1"; printf "%s\\n" "$WM1303_UPDATE_USER" "$WM1303_UPDATE_CHECKOUT" "$WM1303_UPDATE_REPOSITORY"',
                                     "test", str(lib / "updater.conf")], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.splitlines(), ["service-user", str(checkout), "example/pyMC_WM1303"])
            self.assertEqual((lib / "updater.conf").stat().st_mode & 0o777, 0o600)
            self.assertTrue((sbin / "wm1303-upgrade").is_file())

    def test_origin_check_accepts_equivalent_urls_and_preserves_mismatched_repo(self):
        helper = shell_function("config/deploy_overlay.sh", "require_expected_origin")
        self.assertEqual(helper, shell_function("bootstrap.sh", "require_expected_origin"))
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            untouched = repo / "local-work.txt"
            untouched.write_text("uncommitted work\n")
            expected = "https://github.com/HansvanMeer/pyMC_core.git"
            for origin, status in ((expected, 0), ("git@github.com:hansvanmeer/pymc_core", 0),
                                   ("ssh://git@github.com/HansvanMeer/pyMC_core.git", 0),
                                   ("https://github.com/openhop-dev/openhop_core.git", 1),
                                   ("https://token-secret@github.com/other/repo.git", 1)):
                subprocess.run(["git", "-C", str(repo), "config", "remote.origin.url", origin], check=True)
                before = (repo / ".git/config").read_bytes()
                result = subprocess.run(["bash", "-eu", "-c", helper + '\nrequire_expected_origin "$1" "$2"',
                                         "test", str(repo), expected], capture_output=True, text=True)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual((repo / ".git/config").read_bytes(), before)
                self.assertEqual(untouched.read_text(), "uncommitted work\n")
                self.assertNotIn("token-secret", result.stderr)

            # Exercise the actual bootstrap update block with harmless git and
            # ownership mocks: a detached SSH checkout must fetch HTTPS into
            # the exact ref subsequently reset, while CLI transport is retained.
            expected = "https://github.com/example/pyMC_WM1303.git"
            subprocess.run(["git", "-C", str(repo), "config", "remote.origin.url",
                            "git@github.com:Example/pyMC_WM1303.git"], check=True)
            before = (repo / ".git/config").read_bytes()
            bootstrap = (ROOT / "bootstrap.sh").read_text()
            update_block = bootstrap.split("# Clone or update repository\n", 1)[1].split(
                "# Detect existing installation up-front", 1)[0]
            calls = repo / "mock-calls"
            mocks = '''
chown() { :; }
sudo() {
    shift 2
    printf '%s\\n' "$*" >> "$FETCH_LOG"
    if [ "$1" = env ]; then shift 2; fi
    case "$*" in
        'git fetch '*) return "$FETCH_RESULT" ;;
        'git status --porcelain') printf ' M local-work.txt\\n' ;;
    esac
}
'''
            script = mocks + helper + '\nrequire_expected_origin "$INSTALL_DIR" "$REPO_URL"\n' + update_block
            for detached, fetch_result in (("1", "0"), ("0", "0"), ("1", "9")):
                calls.write_text("")
                result = subprocess.run(["bash", "-eu", "-c", script], capture_output=True, text=True,
                                        env=dict(os.environ, INSTALL_DIR=str(repo), REPO_URL=expected,
                                                 PI_USER="service-user", PI_GROUP="service-group",
                                                 WM1303_UPDATE_JOB=detached, FETCH_LOG=str(calls),
                                                 FETCH_RESULT=fetch_result))
                self.assertEqual(result.returncode, int(fetch_result), result.stderr)
                commands = calls.read_text().splitlines()
                fetch = ("env GIT_TERMINAL_PROMPT=0 git fetch " + expected
                         + " +refs/heads/main:refs/remotes/origin/main") if detached == "1" else "git fetch origin"
                self.assertEqual(commands[0], fetch)
                if fetch_result == "0":
                    self.assertTrue(commands[2].startswith("git stash push --include-untracked -m "))
                    self.assertEqual(commands[3:], ["git reset --hard origin/main", "git clean -fd"])
                else:
                    self.assertEqual(commands, [fetch])
                self.assertEqual((repo / ".git/config").read_bytes(), before)
                self.assertEqual(untouched.read_text(), "uncommitted work\n")

    def test_web_probe_reads_web_port_not_an_earlier_mqtt_port(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is required for web-port tests")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            for web, expected in (({"port": 8123}, "8123"), ({}, "8000")):
                config.write_text(yaml.safe_dump({"mqtt": {"port": 1883}, "web": web}))
                for script in ("install.sh", "upgrade.sh"):
                    with self.subTest(script=script, web=web):
                        result = subprocess.run(
                            [sys.executable, "-c", python_block(script, "PYWEBPORT"), str(config)],
                            capture_output=True, text=True, check=True,
                        )
                        self.assertEqual(result.stdout.strip(), expected)

    def test_empty_journal_and_noninteractive_reboot_are_successful(self):
        source = (ROOT / "upgrade.sh").read_text()
        journal = next(line for line in source.splitlines() if line.startswith("JOURNAL_ERRORS="))
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c",
             'journalctl() { printf "%s\\n" "-- No entries --"; }; ' + journal + '\ntest -z "$JOURNAL_ERRORS"'],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        source = (ROOT / "install.sh").read_text()
        reboot = source[source.rindex('if [ "$REBOOT_REQUIRED" = true ]; then'):]
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c",
             'REBOOT_REQUIRED=true; BOLD=; YELLOW=; CYAN=; NC=; reboot() { exit 99; };\n' + reboot],
            input="", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run: sudo reboot", result.stdout)
        self.assertNotIn("Rebooting...", result.stdout)

    def test_sudo_rule_uses_selected_user_and_validates_before_publication(self):
        for script in ("install.sh", "upgrade.sh"):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                source = (ROOT / script).read_text()
                start = source.index('SUDOERS_FILE="')
                block = source[start:source.index('\nstep ', start)]
                block = block.replace("/etc/sudoers.d", directory)
                setup = '''
PI_USER=service.user
LOG_FILE=/dev/null
id() { printf '1234\\n'; }
chown() { :; }
ok() { :; }
fail() { exit 1; }
visudo() { return "$VALIDATION_STATUS"; }
'''
                target = Path(directory) / "090_wm1303-1234"
                target.write_text("previous validated rule\n")
                for status in (1, 0):
                    result = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", setup + block],
                        env=dict(os.environ, VALIDATION_STATUS=str(status)),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertEqual(target.read_text(), "previous validated rule\n" if status else
                                     "service.user ALL=(ALL) NOPASSWD: ALL\n")
                    self.assertEqual(list(Path(directory).glob(".wm1303.*")), [])

    def test_embedded_python_blocks_compile(self):
        for script in ("install.sh", "upgrade.sh"):
            source = (ROOT / script).read_text()
            for match in re.finditer(r"python3[^\n]*<<\s*'?(\w+)'?[^\n]*\n(.*?)\n\1\b", source, re.S):
                with self.subTest(script=script, block=match.group(1)):
                    compile(match.group(2), f"{script}:{match.group(1)}", "exec")

    @unittest.skipUnless(shutil.which("jq"), "jq is required for wizard tests")
    def test_wizard_enter_selects_eu868_and_preset_override_selects_its_region(self):
        function = shell_function("bootstrap.sh", "run_wizard")
        for preset, expected in (("", "EU868"), ("AU915", "AU915")):
            with self.subTest(preset=preset):
                env = dict(os.environ, INSTALL_DIR=str(ROOT), NON_INTERACTIVE="0",
                           WM1303_REGION="", WM1303_PRESET=preset, WM1303_SYNC_WORD="")
                result = subprocess.run(
                    ["bash", "-e", "-c", function + '\nwrite_wizard_config() { printf "SELECTED=%s\\n" "$WM1303_REGION"; }; run_wizard'],
                    input="\n\n",
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertIn(f"SELECTED={expected}", result.stdout)

    def test_help_and_invalid_arguments_exit_before_installation(self):
        for script in ("install.sh", "upgrade.sh"):
            for argument, status in (("--help", 0), ("--invalid-option", 2)):
                with self.subTest(script=script, argument=argument):
                    result = subprocess.run(["bash", str(ROOT / script), argument], capture_output=True, text=True)
                    self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                    self.assertNotIn("Phase 1:", result.stdout)

    def test_bootstrap_returns_child_status_and_cleans_up_tail(self):
        function = shell_function("bootstrap.sh", "run_protected")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status in (0, 7):
                child = root / "child.sh"
                child.write_text(f"exit {status}\n")
                env = dict(os.environ, BOOTSTRAP_LOG=str(root / "output.log"), PI_USER="test-user")
                result = subprocess.run(
                    ["bash", "-e", "-c", function + '\nrun_protected "$1"', "test", str(child)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                # A systemd-detached update forwards output without a nested
                # nohup/tail job, and must preserve failures in the same way.
                child.write_text(f"printf 'final child output\\n'\nexit {status}\n")
                result = subprocess.run(
                    ["bash", "-e", "-c", function + '\nrun_protected "$1"', "test", str(child)],
                    env=dict(env, WM1303_UPDATE_JOB="1"), capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "final child output\n")

    def test_failed_fetch_aborts_even_when_update_function_is_in_if(self):
        function = shell_function("upgrade.sh", "update_repo")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            log = root / "git-calls.log"
            setup = r'''
SKIP_PULL=false
PI_USER=test-user
PI_GROUP=test-group
LOG_FILE=/dev/null
git() { case "$*" in 'rev-parse HEAD') printf 'oldcommit\n';; esac; }
require_expected_origin() { :; }
chown() { :; }
sudo() {
    printf '%s\n' "$*" >> "$CALL_LOG"
    case "$*" in *'git fetch --all'*) return 8;; esac
}
fail() { printf '%s\n' "$*" >&2; exit 1; }
ok() { :; }
warn() { :; }
'''
            result = subprocess.run(
                ["bash", "-eu", "-c", setup + function + '\nif update_repo "$1" dev https://github.com/HansvanMeer/pyMC_core.git; then :; fi', "test", str(root)],
                env=dict(os.environ, CALL_LOG=str(log)),
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Failed to fetch", result.stderr)
            self.assertNotIn("git reset", log.read_text())
            self.assertNotIn("git checkout", log.read_text())

    def test_upgrade_backup_includes_committed_wal_transactions(self):
        code = python_block("upgrade.sh", "PYBACKUP")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data, backup = root / "data", root / "backup"
            data.mkdir()
            backup.mkdir()
            live = sqlite3.connect(data / "repeater.db")
            self.addCleanup(live.close)
            live.execute("PRAGMA journal_mode=WAL")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE messages (message TEXT)")
            live.execute("INSERT INTO messages VALUES ('committed in WAL')")
            live.commit()
            self.assertTrue((data / "repeater.db-wal").exists())
            subprocess.run([sys.executable, "-c", code, str(data), str(backup)], check=True)
            with sqlite3.connect(backup / "repeater.db") as saved:
                self.assertEqual(saved.execute("SELECT message FROM messages").fetchone()[0], "committed in WAL")
            live.close()

    def test_migrations_preserve_user_radio_cache_and_delay_settings(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is required for configuration migration tests")
        config = {
            "bridge": {"dedup_ttl": 15, "bridge_rules": [{"name": "custom", "tx_delay_ms": 50}]},
            "repeater": {"cache_ttl": 25, "max_cache_size": 128, "tx_delay_factor": 2},
            "delays": {"direct_tx_delay_factor": 3},
            "wm1303": {"tx_queue": {"tx_delay_ms": 75}},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(config))
            code = python_block("upgrade.sh", "PYMIGRATE")
            subprocess.run([sys.executable, "-c", code, str(ROOT), str(path)], check=True, capture_output=True)
            saved = yaml.safe_load(path.read_text())
            expected = dict(config)
            expected["bridge"] = {"dedup_ttl_seconds": 15, "bridge_rules": config["bridge"]["bridge_rules"]}
            self.assertEqual(saved, expected)

            # Template defaults must not select an empty canonical Observer
            # configuration over existing legacy connections or metadata.
            code = python_block("upgrade.sh", "PYYAML")
            for section in ("mqtt", "letsmesh"):
                for observer in ({}, {"mqtt_brokers": None}, {"mqtt_brokers": {}}, {"mqtt_brokers": {"brokers": []}}):
                    legacy = {"enabled": True, "broker": "broker.invalid", "owner": "fixture"}
                    original = {section: legacy, **observer}
                    path.write_text(yaml.safe_dump(original))
                    subprocess.run([sys.executable, "-c", code, str(ROOT), str(path)], check=True, capture_output=True)
                    saved = yaml.safe_load(path.read_text())
                    self.assertEqual(saved[section]["owner"], "fixture")
                    self.assertEqual(saved[section]["broker"], "broker.invalid")
                    self.assertEqual(bool(saved.get("mqtt_brokers")), bool(observer.get("mqtt_brokers")))
                    if not observer.get("mqtt_brokers"):
                        self.assertEqual(saved[section], legacy)
                        self.assertEqual(saved.get("letsmesh"), original.get("letsmesh"))

            # Upgrade template defaults must not shadow legacy radio fields
            # before the normalization stage has renamed them.
            radio_path = Path(directory) / "wm1303_ui.json"
            for script in ("install.sh", "upgrade.sh"):
                radio_path.write_text(json.dumps({
                    "channel_e": {"bw": 62500, "sf": 10, "cr": "4/6"},
                    "channel_f": {"bw": 500000, "sf": 11, "cr": "4/8"},
                }))
                blocks = ("PYMERGE", "PYNORM") if script == "upgrade.sh" else ("PYNORM",)
                for block in blocks:
                    code = python_block(script, block)
                    subprocess.run([sys.executable, "-c", code, str(ROOT), str(radio_path)], check=True, capture_output=True)
                saved = json.loads(radio_path.read_text())
                for channel, values in (("channel_e", (62500, 10, "4/6")), ("channel_f", (500000, 11, "4/8"))):
                    self.assertEqual(tuple(saved[channel][key] for key in ("bandwidth", "spreading_factor", "coding_rate")), values)
                    self.assertFalse({"bw", "sf", "cr"}.intersection(saved[channel]))


class ResetScriptTests(unittest.TestCase):
    def test_ui_gpio_update_preserves_installed_reset_and_drain_behavior(self):
        source = ROOT / "overlay/pymc_repeater/repeater/web/wm1303_api.py"
        function = next(node for node in ast.parse(source.read_text()).body
                        if isinstance(node, ast.FunctionDef) and node.name == "_regenerate_gpio_scripts")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            for filename in ("reset_lgw.sh", "power_cycle_lgw.sh"):
                shutil.copyfile(ROOT / "config" / filename, destination / filename)
            namespace = {"_PKTFWD_DIR": destination,
                         "_safe_write": lambda path, text: path.write_text(text),
                         "logger": logging.getLogger("test")}
            exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
            namespace["_regenerate_gpio_scripts"]({"gpio_base_offset": 0, "sx1302_reset": 23})
            reset = (destination / "reset_lgw.sh").read_text()
            self.assertIn("SX1302_RESET_PIN=23", reset)
            self.assertIn("deep_reset)", reset)
            self.assertIn("sleep 10", (destination / "power_cycle_lgw.sh").read_text())
            subprocess.run(["sh", "-n", str(destination / "reset_lgw.sh")], check=True)
            subprocess.run(["sh", "-n", str(destination / "power_cycle_lgw.sh")], check=True)

    @unittest.skipUnless(shutil.which("gcc"), "gcc is required for the isolated HAL status check")
    def test_hal_firmware_status_waits_are_bounded(self):
        source = (ROOT / "overlay/hal/libloragw/src/loragw_sx1302.c").read_text()
        functions = []
        for name in ("sx1302_agc_wait_status", "sx1302_arb_wait_status"):
            start = source.index(f"int {name}(")
            functions.append(source[start : source.index("\n}\n", start) + 3])
        fixture = r'''
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#define LGW_REG_SUCCESS 0
#define LGW_REG_ERROR -1
static int polls, sleeps, ready_after, read_error;
int sx1302_agc_status(uint8_t *status) {
    *status = (++polls >= ready_after) ? 1 : 0;
    return read_error ? LGW_REG_ERROR : LGW_REG_SUCCESS;
}
int sx1302_arb_status(uint8_t *status) { return sx1302_agc_status(status); }
void wait_ms(int milliseconds) { sleeps += milliseconds; }
'''
        checks = r'''
int main(void) {
    int (*waiters[])(uint8_t) = { sx1302_agc_wait_status, sx1302_arb_wait_status };
    for (int i = 0; i < 2; i++) {
        polls = sleeps = read_error = 0; ready_after = 1;
        assert(waiters[i](1) == 0 && polls == 1 && sleeps == 0);
        polls = sleeps = 0; ready_after = 300;
        assert(waiters[i](1) == 0 && polls == 300 && sleeps == 299);
        polls = sleeps = 0; ready_after = 301;
        assert(waiters[i](1) == -1 && polls == 300 && sleeps == 300);
        polls = sleeps = 0; read_error = 1;
        assert(waiters[i](1) == -1 && polls == 1 && sleeps == 0);
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as directory:
            binary = str(Path(directory) / "status-check")
            # Only the status loops are compiled; all SPI and delay calls are mocks.
            subprocess.run(["gcc", "-std=c99", "-Wall", "-Werror", "-x", "c", "-", "-o", binary],
                           input=fixture + "\n".join(functions) + checks, text=True, check=True)
            subprocess.run([binary], check=True, capture_output=True, timeout=5)

    def test_deep_reset_releases_peripherals_and_honors_short_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            gpio = Path(directory)
            for pin in (529, 530, 517, 525):
                (gpio / f"gpio{pin}").mkdir()
            script = (ROOT / "config/reset_lgw.sh").read_text().replace("/sys/class/gpio", str(gpio))
            # Intercept sleeps and redirect all GPIO paths into the fixture.
            result = subprocess.run(
                ["sh", "-s", "--", "deep_reset", "7"],
                input='sleep() { printf "SLEEP=%s\\n" "$1"; }\n' + script,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual((gpio / "gpio529/value").read_text().strip(), "0")
            for pin in (530, 517, 525):
                self.assertEqual((gpio / f"gpio{pin}/value").read_text().strip(), "1")
            self.assertIn("SLEEP=7", result.stdout)
            self.assertNotIn("SLEEP=10", result.stdout)

    def test_invalid_drain_fails_before_gpio_access(self):
        result = subprocess.run(
            ["sh", str(ROOT / "config/reset_lgw.sh"), "deep_reset", "invalid"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-negative integer", result.stderr)
        self.assertNotIn("/sys/", result.stderr)


class RuntimeFileOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("prepare_runtime_files", ROOT / "config/prepare_runtime_files.py")
        cls.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.helper)

    def test_existing_config_keeps_content_and_receives_expected_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text('{"enabled": true}')
            path.chmod(0o600)
            self.helper.prepare_runtime_file(path, os.getuid(), os.getgid())
            self.assertEqual(path.read_text(), '{"enabled": true}')
            self.assertEqual(path.stat().st_mode & 0o777, 0o664)

    def test_links_are_refused_without_changing_the_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("preserve")
            target.chmod(0o600)
            symbolic = root / "symbolic"
            symbolic.symlink_to(target)
            with self.assertRaises(OSError):
                self.helper.prepare_runtime_file(symbolic, os.getuid(), os.getgid())
            hard = root / "hard"
            os.link(target, hard)
            with self.assertRaises(ValueError):
                self.helper.prepare_runtime_file(hard, os.getuid(), os.getgid())
            self.assertEqual(target.read_text(), "preserve")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_missing_runtime_file_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            self.helper.prepare_runtime_file(path, os.getuid(), os.getgid())
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_gid, os.getgid())


if __name__ == "__main__":
    unittest.main()
