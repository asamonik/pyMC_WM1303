"""Preserve MeshCore's tri-state login marker through companion responses.

These methods retain upstream wire, retries and frame ownership while rejecting
same-peer overlap, preserving the first completion, and covering cancellation
and pre-start task cleanup. Preserve login_flags: marker 2 means read-only, not
admin. Server ACL role numbers and admission policy are unchanged.
"""

import asyncio
import logging
import struct
from typing import Optional

from openhop_core.companion.constants import (
    ERR_CODE_ILLEGAL_ARG,
    FIRMWARE_VER_CODE,
    LOGIN_TIMEOUT_HINT_MS,
    PUB_KEY_SIZE,
    PUSH_CODE_LOGIN_FAIL,
    PUSH_CODE_LOGIN_SUCCESS,
)
from openhop_core.protocol import Packet, PacketBuilder

logger = logging.getLogger("CompanionFrameServer")


class LoginRequestsMixin:
    async def _start_login_request(self, pub_key: bytes, password: str) -> dict:
        """Start a login request and return SENT metadata plus its result task."""
        contact = self.contacts.get_by_key(pub_key)
        if not contact:
            return {"success": False, "error": "not_found", "reason": "Contact not found"}
        # Resolve by exact public key, not name: two contacts can share a name
        # (e.g. a re-keyed node) and get_by_name returns the first match, which
        # would encrypt/route to the wrong key.
        proxy = self.contacts.get_proxy_by_key(pub_key)
        if not proxy:
            return {"success": False, "error": "not_found", "reason": "Contact not found"}
        login_handler = self._get_login_response_handler()
        if not login_handler:
            return {
                "success": False,
                "error": "bad_state",
                "reason": "Login handler not available",
            }
        login_target_key = proxy.public_key_bytes
        # Responses correlate to a full peer key, not to a unique login tag.
        # Check before touching its password/callback. There is no await from
        # this check through registration, so another start cannot interleave.
        if login_handler.has_pending_login(login_target_key):
            return {
                "success": False,
                "error": "bad_state",
                "reason": "Login already pending for this contact",
            }
        dest_hash = proxy.dest_hash
        login_handler.store_login_password(dest_hash, password)
        login_result: dict = {"success": False, "data": {}}
        login_event = asyncio.Event()

        def _login_cb(success: bool, data: dict) -> None:
            # Firmware consumes pending_login on its first matching response.
            # Core can dispatch again before the response task cleans up.
            if login_event.is_set():
                return
            login_result["success"] = success
            login_result["data"] = data
            login_event.set()

        login_handler.register_login_callback(login_target_key, _login_cb)

        async def _wait_login(timeout_s: float) -> dict:
            try:
                await asyncio.wait_for(login_event.wait(), timeout=timeout_s)
                return {"timeout": False}
            except asyncio.TimeoutError:
                return {"timeout": True}

        def _build_login_packet() -> tuple[Packet, Optional[int]]:
            return (
                PacketBuilder.create_login_packet(
                    contact=proxy, local_identity=self._identity, password=password
                ),
                None,
            )

        login_sent_tag = int.from_bytes(proxy.public_key_bytes[:4], "little")
        cleaned = False

        def _cleanup_login() -> None:
            nonlocal cleaned
            if cleaned:
                return
            # Core cleanup and task completion may both call us. An old
            # completion must never clear a subsequently admitted password.
            cleaned = True
            login_handler.remove_login_callback(login_target_key, _login_cb)
            login_handler.clear_login_password(dest_hash)

        # MeshCore exposes the first four public-key bytes as the login SENT
        # tag, rather than the timestamp inside the login packet.
        login_log_label = f"login -> 0x{dest_hash:02X} ({contact.name})"
        try:
            started = await self._start_request(
                _build_login_packet,
                _wait_login,
                proxy,
                total_timeout_s=None,
                log_label=login_log_label,
                sent_tag=login_sent_tag,
                cleanup=_cleanup_login,
            )
        except BaseException:
            # No response task owns this registration yet. In particular,
            # CancelledError bypasses upstream's except Exception cleanup.
            _cleanup_login()
            raise
        if not started.get("success"):
            _cleanup_login()
            return started

        raw_task = started["task"]
        # A task cancelled before its first step never enters core's finally.
        # Once handed off, only actual completion may release the registration.
        raw_task.add_done_callback(lambda _task: _cleanup_login())

        async def _format_login_result() -> dict:
            await raw_task
            if not login_event.is_set():
                return {
                    "success": False,
                    "timeout": True,
                    "reason": "Login response timeout",
                }
            data = login_result["data"]
            if login_result["success"]:
                # Mirror firmware onContactResponse (MyMesh.cpp:686-690):
                # open a connection on a successful login response. This is the
                # single point every login response (frame server or direct API)
                # flows through, matching the firmware's onContactResponse.
                self.note_login_connection(pub_key, data.get("keep_alive_interval", 0))
            return {
                "success": login_result["success"],
                "repeater": contact.name,
                "is_admin": data.get("is_admin", False),
                "login_flags": data.get(
                    "login_flags", 1 if data.get("is_admin") else 0
                ),
                "keep_alive_interval": data.get("keep_alive_interval", 0),
                "tag": data.get("timestamp", 0),
                "acl_permissions": data.get("reserved", data.get("permissions", 0)),
                "firmware_ver_level": data.get("firmware_ver_level"),
                "reason": ("Login successful" if login_result["success"] else "Login failed"),
            }

        formatting = _format_login_result()
        try:
            started["task"] = self._spawn_background_task(
                formatting, "login result formatting"
            )
        except BaseException:
            formatting.close()
            # The raw task remains bridge-owned; its done callback retains
            # cleanup responsibility until its retry/wait work has finished.
            raise
        return started


class LoginCommandsMixin:
    async def _cmd_send_login(self, data: bytes) -> None:
        if len(data) < PUB_KEY_SIZE:
            self._write_err(ERR_CODE_ILLEGAL_ARG)
            return
        pubkey = data[:PUB_KEY_SIZE]
        password = (
            data[PUB_KEY_SIZE:].decode("utf-8", errors="replace").rstrip("\x00")
            if len(data) > PUB_KEY_SIZE
            else ""
        )
        started = await self.bridge._start_login_request(pubkey, password)
        if not started.get("success"):
            self._write_request_start_error(started)
            return
        self._write_sent_result(started["sent"], default_timeout_ms=LOGIN_TIMEOUT_HINT_MS)

        async def _write_login_result() -> None:
            result = await started["task"]
            if result.get("timeout"):
                logger.debug("Login request timed out for %s; no login push sent", pubkey[:6].hex())
                return
            if result.get("success"):
                # Layout matches MeshCore companion_radio onContactResponse
                fw_level = result.get("firmware_ver_level")
                if fw_level is None:
                    fw_level = FIRMWARE_VER_CODE  # fallback so app sees >= 2 for owner info
                self._write_frame(
                    bytes(
                        [
                            PUSH_CODE_LOGIN_SUCCESS,
                            result.get(
                                "login_flags", 1 if result.get("is_admin") else 0
                            ),
                        ]
                    )
                    + pubkey[:6]
                    + struct.pack("<I", result.get("tag", 0))
                    + bytes([result.get("acl_permissions", 0)])
                    + bytes([min(255, max(0, int(fw_level)))])
                )
            else:
                self._write_frame(bytes([PUSH_CODE_LOGIN_FAIL, 0]) + pubkey[:6])

        self._spawn_request_task(_write_login_result(), "companion login response")
