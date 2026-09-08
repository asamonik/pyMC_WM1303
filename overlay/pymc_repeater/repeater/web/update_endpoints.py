"""WM1303 updates through the installed, fixed systemd launcher.

The WM installation includes editable core/repeater overlays and a patched HAL.
A generic pip upgrade cannot preserve it. The launcher runs bootstrap in its
own unit so stopping the repeater does not kill the upgrade. Job status and
logs come from systemd, including after this Python process has restarted.
Importing this module never changes packages, services, or files.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone

import cherrypy

logger = logging.getLogger("HTTPServer")
_LAUNCHER = "/usr/local/sbin/wm1303-upgrade"
_VERSION_FILES = (Path("/etc/openhop_repeater/version"), Path("/etc/pymc_repeater/version"))
_CHANNEL = "main"  # bootstrap updates this branch; core/repeater refs belong to its scripts
CHECK_CACHE_TTL = 600
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9_.-]+/pyMC_WM1303", re.IGNORECASE)
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}")


def _get_installed_version() -> str:
    for path in _VERSION_FILES:
        try:
            version = path.read_text(encoding="utf-8").strip().removeprefix("v")
            if _VERSION_RE.fullmatch(version):
                return version
        except OSError:
            continue
    return "unknown"


def _has_update(installed: str, latest: str) -> bool:
    if not _VERSION_RE.fullmatch(installed) or not _VERSION_RE.fullmatch(latest):
        return False
    def parts(value):
        numbers = tuple(int(part) for part in value.split("."))
        return numbers + (0,) * (4 - len(numbers))
    return parts(latest) > parts(installed)


def _run_helper(operation: str) -> str:
    if operation not in ("start", "status", "log"):
        raise ValueError("Unknown updater operation")
    if not os.path.isfile(_LAUNCHER):
        raise RuntimeError("WM1303 update launcher is not installed. Run this project's bootstrap/upgrade once over SSH.")
    command = [_LAUNCHER, operation]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "WM1303 update launcher failed")
    return result.stdout


def _fetch_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "pyMC-WM1303-updater"})
    with urllib.request.urlopen(request, timeout=12) as response:
        content = response.read(2_000_001)
    if len(content) > 2_000_000:
        raise ValueError("Update response is too large")
    return content.decode("utf-8")


class _UpdateState:
    def __init__(self):
        self._lock = threading.RLock()
        self.latest_version = None
        self.last_checked = None
        self.error = None
        self.checking = False
        self._job = {}
        self._job_checked = float("-inf")
        self._repository = None

    def _read_job(self):
        if time.monotonic() - self._job_checked < 2:
            return self._job
        job = {"state": "idle", "install_available": False, "error": None,
               "repository": None}
        try:
            properties = dict(line.split("=", 1) for line in _run_helper("status").splitlines() if "=" in line)
            repository = properties.get("Repository", "")
            if not _REPOSITORY_RE.fullmatch(repository):
                raise RuntimeError("WM1303 launcher returned no valid installed repository")
            job.update(repository=repository, install_available=True)
            active, substate = properties.get("ActiveState"), properties.get("SubState")
            if active == "activating" or substate in ("start", "start-pre", "running"):
                job["state"] = "installing"
            elif active == "failed":
                job.update(state="error", error="WM1303 upgrade failed; see the update log.")
            elif active == "active" and substate == "exited":
                if properties.get("ExecMainStatus", "0") == "0" and properties.get("Result", "success") == "success":
                    job["state"] = "complete"
                else:
                    job.update(state="error", error="WM1303 upgrade failed; see the update log.")
            elif active == "deactivating":
                job["state"] = "installing"
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            job["install_unavailable_reason"] = str(exc)
        self._job = job
        self._job_checked = time.monotonic()
        return job

    def snapshot(self):
        with self._lock:
            job = dict(self._read_job())
            if self._repository is not None and self._repository != job["repository"]:
                self.latest_version = None
                self.last_checked = None
            self._repository = job["repository"]
            state = job["state"]
            if self.checking and state != "installing":
                state = "checking"
            elif self.error and state == "idle":
                state = "error"
            current = _get_installed_version()
            return {
                **job, "state": state, "error": job["error"] or self.error,
                "current_version": current, "latest_version": self.latest_version,
                "has_update": bool(self.latest_version and _has_update(current, self.latest_version)),
                "last_checked": self.last_checked.isoformat() if self.last_checked else None,
                "channel": _CHANNEL, "project": "pyMC_WM1303", "rate_limit_until": None,
            }

    def check(self, force=False):
        with self._lock:
            snap = self.snapshot()
            if self.checking or snap["state"] == "installing":
                return False
            if not snap["repository"]:
                raise RuntimeError(snap.get("install_unavailable_reason", "Cannot identify the installed WM1303 repository"))
            if not force and self.last_checked and (datetime.now(timezone.utc) - self.last_checked).total_seconds() < CHECK_CACHE_TTL:
                return False
            self.checking = True
            self.error = None
            thread = threading.Thread(target=self._check_version, args=(snap["repository"],),
                                      daemon=True, name="wm1303-update-check")
            try:
                thread.start()
            except BaseException:
                self.checking = False
                raise
            return True

    def _check_version(self, repository):
        try:
            latest = _fetch_url(f"https://raw.githubusercontent.com/{repository}/{_CHANNEL}/VERSION").strip().removeprefix("v")
            if not _VERSION_RE.fullmatch(latest):
                raise ValueError("Repository returned an invalid WM1303 VERSION")
            with self._lock:
                self.latest_version = latest
                self.error = None
        except Exception as exc:
            logger.warning("WM1303 version check failed: %s", exc)
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.last_checked = datetime.now(timezone.utc)
                self.checking = False

    def install(self, force=False):
        with self._lock:
            snap = self.snapshot()
            if not snap["install_available"]:
                raise RuntimeError(snap.get("install_unavailable_reason", "WM1303 update launcher unavailable"))
            if snap["state"] == "installing" or self.checking:
                raise ValueError("An update or version check is already in progress")
            if not force and snap["latest_version"] is not None and not snap["has_update"]:
                raise ValueError("No newer WM1303 version found. Use force to reinstall.")
            _run_helper("start")  # launches a separate systemd unit; never runs pip in this process
            self.error = None
            self._job = {**self._job, "state": "installing", "error": None}
            self._job_checked = time.monotonic()


_state = _UpdateState()


class UpdateAPIEndpoints:
    @staticmethod
    def _ok(data):
        return {"success": True, **data}

    @staticmethod
    def _err(message, status=400):
        cherrypy.response.status = status
        return {"success": False, "error": str(message)}

    @staticmethod
    def _body():
        body = getattr(cherrypy.request, "json", None)
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise cherrypy.HTTPError(400, "Expected a JSON object")
        if "force" in body and not isinstance(body["force"], bool):
            raise cherrypy.HTTPError(400, "'force' must be a boolean")
        return body

    @staticmethod
    def _method(*allowed):
        if cherrypy.request.method not in (*allowed, "OPTIONS"):
            raise cherrypy.HTTPError(405, "Method Not Allowed")

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def status(self, **kwargs):
        self._method("GET")
        if cherrypy.request.method == "OPTIONS":
            return ""
        return self._ok(_state.snapshot())

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in(force=False)
    def check(self, **kwargs):
        self._method("GET", "POST")
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            started = _state.check(self._body().get("force", False))
        except RuntimeError as exc:
            return self._err(exc, 503)
        return self._ok({**_state.snapshot(), "message": "WM1303 version check started" if started else "Cached result or updater busy"})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def install(self, **kwargs):
        self._method("POST")
        if cherrypy.request.method == "OPTIONS":
            return ""
        try:
            _state.install(self._body().get("force", False))
        except ValueError as exc:
            return self._err(exc, 409)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            return self._err(exc, 503)
        return self._ok({"state": "installing", "message": "WM1303 upgrade started. The web interface will disconnect while the repeater restarts; reconnect for the final status and log."})

    @cherrypy.expose
    def progress(self, **kwargs):
        self._method("GET")
        if cherrypy.request.method == "OPTIONS":
            return ""
        cherrypy.response.headers.update({
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })

        def event(data):
            return f"data: {json.dumps(data)}\n\n"

        def generate():
            yield event({"type": "connected", "message": "WM1303 update log"})
            # The shipped Console uses this phrase to reconnect after the
            # service disappears, which happens near the start of an upgrade.
            yield event({"type": "line", "line": "Restarting service is part of a WM1303 upgrade; a disconnected interface can take several minutes to return."})
            previous = []
            while True:
                try:
                    snap = _state.snapshot()
                    lines = _run_helper("log").splitlines()
                    # The launcher returns a rolling tail. Match its overlapping
                    # suffix/prefix instead of losing new lines once it reaches 500.
                    overlap = min(len(previous), len(lines))
                    while overlap and previous[-overlap:] != lines[:overlap]:
                        overlap -= 1
                    for line in lines[overlap:]:
                        yield event({"type": "line", "line": line})
                    previous = lines
                    yield event({"type": "status", "state": snap["state"]})
                    if snap["state"] in ("complete", "error", "idle"):
                        yield event({"type": "done", "state": snap["state"], "error": snap["error"]})
                        return
                    yield event({"type": "keepalive"})
                    time.sleep(2)
                except GeneratorExit:
                    return
                except Exception as exc:
                    yield event({"type": "done", "state": "error", "error": str(exc)})
                    return

        return generate()

    progress._cp_config = {"response.stream": True}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def channels(self, **kwargs):
        self._method("GET")
        if cherrypy.request.method == "OPTIONS":
            return ""
        return self._ok({"channels": [_CHANNEL], "current_channel": _CHANNEL})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    @cherrypy.tools.json_in()
    def set_channel(self, **kwargs):
        self._method("POST")
        if cherrypy.request.method == "OPTIONS":
            return ""
        if self._body().get("channel") != _CHANNEL:
            return self._err("WM1303 upgrades follow main. Core/repeater/HAL branches are managed together by the WM1303 scripts.")
        return self._ok({"channel": _CHANNEL, "message": "WM1303 update channel is main."})

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def changelog(self, **kwargs):
        self._method("GET")
        if cherrypy.request.method == "OPTIONS":
            return ""
        snap = _state.snapshot()
        if not snap["repository"]:
            return self._err(snap.get("install_unavailable_reason", "Cannot identify the installed WM1303 repository"), 503)
        try:
            limit = max(1, min(int(kwargs.get("max", 40)), 100))
            data = json.loads(_fetch_url(f"https://api.github.com/repos/{snap['repository']}/commits?sha={_CHANNEL}&per_page={limit}"))
            if not isinstance(data, list):
                raise ValueError("Invalid commit history response")
            commits = []
            for item in data:
                commit = item.get("commit", {})
                message = commit.get("message", "").strip()
                sha = item.get("sha", "")
                commits.append({"sha": sha, "short_sha": sha[:7], "title": message.split("\n")[0],
                                "body": "\n".join(message.split("\n")[1:]).strip(),
                                "author": (commit.get("author") or {}).get("name", ""),
                                "date": (commit.get("author") or {}).get("date", ""),
                                "url": item.get("html_url", "")})
        except (ValueError, TypeError, OSError) as exc:
            return self._err(f"Could not read WM1303 history: {exc}", 502)
        return self._ok({"channel": _CHANNEL, "installed": snap["current_version"],
                         "latest": snap["latest_version"] or "", "commits": commits,
                         "message": "Recent WM1303 commits, not an exact installed-version diff."})
