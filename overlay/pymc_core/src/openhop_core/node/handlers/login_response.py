import struct
from typing import Callable, Optional

from ...protocol import CryptoUtils, Identity, Packet
from ...protocol.constants import PAYLOAD_TYPE_ANON_REQ, PAYLOAD_TYPE_PATH, PAYLOAD_TYPE_RESPONSE
from ...protocol.packet_utils import PathUtils
from ...util.callbacks import invoke_maybe_awaitable
from .base import BaseHandler
from .result import HandlerResult

# Response codes from C++ server
RESP_SERVER_LOGIN_OK = 0x80
# Alternative success code observed in practice
RESP_SERVER_LOGIN_SUCCESS_ALT = 0x00


class LoginResponseHandler(BaseHandler):
    """
    Handles PAYLOAD_TYPE_RESPONSE packets for login authentication responses.

    Expected response format from C++ server:
    - timestamp (4 bytes): Server response timestamp
    - response_code (1 byte): RESP_SERVER_LOGIN_OK (0x80) for success
    - keep_alive_interval (1 byte): Recommended keep-alive interval (secs / 16)
    - login_flags (1 byte): 1 if admin, 2 if read-only guest, otherwise 0
    - reserved (1 byte): Full ACL permissions from the server
    - random_blob (4 bytes): Random data for packet uniqueness

    """

    @staticmethod
    def payload_type() -> int:
        return PAYLOAD_TYPE_RESPONSE

    def __init__(self, local_identity, contacts, log_fn):
        self.local_identity = local_identity
        self.contacts = contacts
        self.log = log_fn
        # Pending login completions keyed by the target contact's full public key
        # (32 bytes). A response is dispatched only to the waiter whose target
        # matches the authenticated sender, mirroring the firmware sender gate
        # (companion_radio MyMesh.cpp: pending_login compared against the
        # responding contact's pubkey) generalized to concurrent logins.
        self._pending_logins = {}  # pubkey bytes -> callback
        # Store login passwords persistently (not tied to contact objects)
        self._active_login_passwords = {}  # dest_hash -> password
        # Protocol response handler for forwarding telemetry responses
        self._protocol_response_handler = None

    def set_protocol_response_handler(self, protocol_response_handler):
        """Set protocol response handler for forwarding telemetry responses."""
        self._protocol_response_handler = protocol_response_handler

    def has_pending_login(self, pubkey: bytes) -> bool:
        """Whether this exact peer still owns a login completion callback."""
        return bytes(pubkey) in self._pending_logins

    def register_login_callback(self, pubkey: bytes, callback: Callable[[bool, dict], None]):
        """Register a completion for a login to the contact with ``pubkey``.

        Keyed by the target's full public key (not the 1-byte dest hash, which
        collides). The callback accepts (success: bool, response_data: dict).
        """
        self._pending_logins[bytes(pubkey)] = callback

    def remove_login_callback(self, pubkey: bytes, callback) -> None:
        """Remove the pending login for ``pubkey`` only if it is ``callback``.

        Identity-guarded so a timed-out login never clears a concurrent login's
        pending completion.
        """
        key = bytes(pubkey)
        if self._pending_logins.get(key) is callback:
            del self._pending_logins[key]

    @staticmethod
    def _pubkey_bytes(contact) -> bytes:
        """Return a contact's public key as 32 raw bytes (hex str or bytes)."""
        pk = contact.public_key
        return pk if isinstance(pk, bytes) else bytes.fromhex(pk)

    def _has_pending_login_for_hash(self, lookup_hash: int) -> bool:
        """True when any pending login (or stored password) targets this hash."""
        if lookup_hash in self._active_login_passwords:
            return True
        return any(key and key[0] == lookup_hash for key in self._pending_logins)

    async def _forward_to_protocol_handler(self, packet) -> HandlerResult:
        """Route a RESPONSE that is not a pending login onward.

        Telemetry/status responses correlate in the protocol response handler.
        PATH packets are skipped: PathHandler already invoked the protocol
        response handler for them before we were called.
        """
        if self._protocol_response_handler:
            if packet.get_payload_type() == PAYLOAD_TYPE_PATH:
                return HandlerResult.not_for_us()
            try:
                result = await self._protocol_response_handler(packet)
                return result if isinstance(result, HandlerResult) else HandlerResult.not_for_us()
            except Exception as e:
                self.log("Error forwarding RESPONSE packet to " f"protocol response handler: {e}")
        return HandlerResult.not_for_us()

    def store_login_password(self, dest_hash: int, password: str):
        """Store password for response decryption by destination hash."""
        self._active_login_passwords[dest_hash] = password

    def clear_login_password(self, dest_hash: int):
        """Clear stored password for destination hash."""
        if dest_hash in self._active_login_passwords:
            del self._active_login_passwords[dest_hash]

    async def __call__(self, packet: Packet) -> HandlerResult:
        """Handle RESPONSE/ANON_REQ packets and report MAC ownership."""
        if len(packet.payload) < 4:
            return HandlerResult.not_for_us()

        # Determine packet structure: ANON_REQ has our pubkey at bytes 1-33
        if (
            len(packet.payload) >= 34
            and packet.payload[1:33] == self.local_identity.get_public_key()
        ):
            # ANON_REQ format: dest_hash(1) + pubkey(32) + encrypted_data
            dest_hash = packet.payload[0]
            encrypted_start = 33
            lookup_hash = dest_hash  # For ANON_REQ, look up by destination hash
        else:
            # RESPONSE format: dest_hash(1) + src_hash(1) + encrypted_data
            dest_hash = packet.payload[0]
            src_hash = packet.payload[1]
            encrypted_start = 2
            lookup_hash = src_hash  # For RESPONSE, look up by source hash

        # Check the on-air destination before trying any candidate secret.
        if dest_hash != self.local_identity.get_public_key()[0]:
            return HandlerResult.not_for_us()

        # Gate: only attempt login processing while a login is actually pending
        # for this 1-byte hash. The full-pubkey pending map is authoritative so
        # one login completing cannot close the gate on a concurrent login to a
        # hash-colliding contact; the legacy per-hash password store is kept as
        # a compatibility gate.
        if not self._has_pending_login_for_hash(lookup_hash):
            # Not a login response (e.g. telemetry/status); route onward.
            return await self._forward_to_protocol_handler(packet)

        # Collect all contacts whose public_key first byte matches (hash collision / multiple peers)
        candidates = []
        for contact in self.contacts.contacts:
            try:
                pk = contact.public_key
                contact_pubkey = pk if isinstance(pk, bytes) else bytes.fromhex(pk)
                if len(contact_pubkey) == 32 and contact_pubkey[0] == lookup_hash:
                    candidates.append(contact)
            except Exception:
                continue

        if not candidates:
            # No contact matches the responding key: nothing to correlate to a
            # waiter (firmware ignores a response from a non-matching contact).
            self.log("No contact found for login response (src_hash=0x%02x)" % lookup_hash)
            return HandlerResult.not_for_us()

        # Try each candidate until one decrypts successfully (same shared-secret as firmware)
        authenticated = False
        response_data = None
        matched_contact = None
        for contact in candidates:
            authenticated, response_data = await self._decrypt_response(
                packet, contact, encrypted_start
            )
            if authenticated:
                matched_contact = contact
                break

        if authenticated and matched_contact:
            if self._pubkey_bytes(matched_contact) not in self._pending_logins:
                # Authenticated, but this contact has no login pending: not a
                # login response. Mirrors firmware onContactResponse, where a
                # pending_login pubkey mismatch falls through to the status/
                # telemetry matchers instead of being treated as a login.
                return await self._forward_to_protocol_handler(packet)
            if response_data is None:
                self.log("Authenticated login response had invalid or incomplete contents")
                return HandlerResult.consumed()
            await self._process_login_response(response_data, matched_contact)
            self.clear_login_password(lookup_hash)
            return HandlerResult.consumed()
        # Decryption failed for every candidate: the response is not authentically
        # ours, so no waiter is resolved (firmware ignores it).
        self.log("Failed to decrypt login response")
        return HandlerResult.not_for_us()

    async def _decrypt_response(
        self, packet: Packet, contact, encrypted_start: int = 2
    ) -> tuple[bool, Optional[dict]]:
        """Decrypt the login response and separately report MAC authentication."""
        try:
            # Extract encrypted portion (skip the header part)
            encrypted_data = packet.payload[encrypted_start:]

            # Calculate X25519 ECDH shared secret
            pk = contact.public_key
            contact_pubkey = pk if isinstance(pk, bytes) else bytes.fromhex(pk)
            contact_identity = Identity(contact_pubkey)
            shared_secret = contact_identity.calc_shared_secret(
                self.local_identity.get_private_key()
            )

            # Verify MAC and decrypt using X25519 shared secret
            aes_key = shared_secret[:16]
            plaintext = CryptoUtils.mac_then_decrypt(aes_key, shared_secret, encrypted_data)

            if not plaintext:
                return False, None

            # If this is a PATH packet, unwrap the path-return envelope to get
            # the inner response.  PATH format after decryption:
            #   path_len(1) + path(N) + extra_type(1) + extra_data(M)
            pkt_type = packet.get_payload_type()
            if pkt_type == PAYLOAD_TYPE_PATH:
                if len(plaintext) < 1 or not PathUtils.is_valid_path_len(plaintext[0]):
                    return False, None
                path_len_byte = plaintext[0]
                path_byte_len = PathUtils.get_path_byte_len(path_len_byte)
                inner_offset = 1 + path_byte_len + 1  # skip path_len + path + extra_type
                if len(plaintext) < inner_offset:
                    return False, None
                extra_type = plaintext[1 + path_byte_len] & 0x0F
                if extra_type != PAYLOAD_TYPE_RESPONSE or len(plaintext) <= inner_offset:
                    return True, None
                plaintext = plaintext[inner_offset:]

            if len(plaintext) < 12:
                return True, None

            # Parse the C++ response format (handleLoginReq reply_data):
            # timestamp(4) + response_code(1) + keep_alive(1) + login_flags(1) +
            # permissions(1) + random(4) + [firmware_ver_level(1) at index 12]
            timestamp, response_code, keep_alive, login_flags, reserved = struct.unpack(
                "<IBBBB", plaintext[:8]
            )
            random_blob = plaintext[8:12]
            firmware_ver_level = int(plaintext[12]) if len(plaintext) >= 13 else None

            return True, {
                "timestamp": timestamp,
                "response_code": response_code,
                "keep_alive_interval": keep_alive,
                # Firmware forwards this byte unchanged: 2 is a read-only
                # guest, not a truthy admin flag. Keep the boolean API too.
                "is_admin": login_flags == 1,
                "login_flags": login_flags,
                "reserved": reserved,
                "random_blob": random_blob,
                "firmware_ver_level": firmware_ver_level,
                "contact": contact,
            }

        except Exception:
            return False, None

    async def _process_login_response(self, response_data: dict, contact):
        """Process the decrypted login response."""
        response_code = response_data["response_code"]
        success = response_code in (RESP_SERVER_LOGIN_OK, RESP_SERVER_LOGIN_SUCCESS_ALT)

        if success:
            self.log(f"Login successful to '{contact.name}' " f"(code: 0x{response_code:02X})")
            contact.last_login_success = response_data["timestamp"]
            contact.is_admin = response_data["is_admin"]
        else:
            self.log(f"Login failed to '{contact.name}' " f"(code: 0x{response_code:02X})")

        # Dispatch ONLY to the waiter whose target matches this authenticated
        # sender's full public key. No pending login for this contact → resolve
        # nothing (firmware ignores a response from a non-pending contact).
        callback = self._pending_logins.get(self._pubkey_bytes(contact))
        if callback is not None:
            await self._safe_callback(callback, success, response_data)
        else:
            self.log(f"No pending login for '{contact.name}' - login response ignored")

    async def _safe_callback(self, callback, success: bool, data: dict):
        """Safely invoke a login completion callback without blocking."""
        try:
            await invoke_maybe_awaitable(callback, success, data)
        except Exception as e:
            self.log(f"Error in login callback: {e}")


class AnonReqResponseHandler(BaseHandler):
    """Handler for ANON_REQ packets that might be login responses."""

    @staticmethod
    def payload_type() -> int:
        return PAYLOAD_TYPE_ANON_REQ

    def __init__(self, local_identity, contacts, log_fn):
        self.local_identity = local_identity
        self.contacts = contacts
        self.log = log_fn
        self.login_response_handler = LoginResponseHandler(local_identity, contacts, log_fn)

    def register_login_callback(self, pubkey: bytes, callback):
        self.login_response_handler.register_login_callback(pubkey, callback)

    def remove_login_callback(self, pubkey: bytes, callback):
        self.login_response_handler.remove_login_callback(pubkey, callback)

    def store_login_password(self, dest_hash: int, password: str):
        self.login_response_handler.store_login_password(dest_hash, password)

    def clear_login_password(self, dest_hash: int):
        self.login_response_handler.clear_login_password(dest_hash)

    async def __call__(self, packet: Packet) -> None:
        """Check if this ANON_REQ is actually a login response."""
        if (
            len(packet.payload) >= 34
            and packet.payload[1:33] == self.local_identity.get_public_key()
        ):
            await self.login_response_handler(packet)
