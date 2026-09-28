"""TCP compatibility checks with the real, overlaid OpenHop dependencies.

Run explicitly with the installed core/repeater on PYTHONPATH; the standalone
unit suite intentionally does not require those upstream packages or radios.
"""

import asyncio
import copy
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openhop_core import LocalIdentity
from openhop_core.companion import constants as codes
from openhop_core.companion.models import Channel, Contact, QueuedMessage
from openhop_core.hardware.wm1303_backend import WM1303Backend
from openhop_core.protocol.packet_builder import PacketBuilder
from repeater.companion.bridge import RepeaterCompanionBridge
from repeater.companion.frame_server_lifecycle import CompanionFrameServer
from repeater.data_acquisition.sqlite_handler import SQLiteHandler
from repeater.main import RepeaterDaemon
from repeater.packet_router import PacketRouter


class PreferenceStore:
    def __init__(self):
        self.rows = {}

    def companion_load_prefs(self, key):
        return copy.deepcopy(self.rows.get(key))

    def companion_save_prefs(self, key, prefs):
        self.rows[key] = copy.deepcopy(prefs)
        return True


class ClosingSQLiteStore:
    """Close test connections in the thread which opened them."""

    def __init__(self, handler):
        self.handler = handler

    def __getattr__(self, name):
        def call(*args, **kwargs):
            try:
                return getattr(self.handler, name)(*args, **kwargs)
            finally:
                self.handler.close_thread_connection()
        return call


class CompanionTCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = WM1303Backend.__new__(WM1303Backend)
        self.backend._runtime_radio_config = {
            "channels": [{"active": False, "frequency": 915000000}],
            "channel_e": dict(enabled=True, frequency=869618000, bandwidth=62500,
                              spreading_factor=8, coding_rate="4/8", tx_power=27),
        }
        self.daemon = RepeaterDaemon.__new__(RepeaterDaemon)
        self.daemon.radio = self.backend
        self.daemon.config = {}
        self.daemon.repeater_handler = None
        self.daemon.discovery_helper = None
        self.store = PreferenceStore()
        self.identity = LocalIdentity(seed=bytes(range(32)))
        self.injector = AsyncMock(return_value=True)
        self.bridge = self.make_bridge()
        await self.bridge.start()
        self.server = CompanionFrameServer(
            self.bridge, "01", port=0, bind_address="127.0.0.1")
        self.daemon.companion_frame_servers = [self.server]
        self.router = PacketRouter(self.daemon)
        await self.server.start()
        port = self.server._server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)

    def make_bridge(self):
        return RepeaterCompanionBridge(
            self.identity, self.injector, sqlite_handler=self.store,
            radio_config=self.daemon._get_companion_radio_settings(),
            radio_settings_getter=self.daemon._get_companion_radio_settings,
            max_tx_power_getter=self.daemon._get_companion_max_tx_power,
        )

    async def asyncTearDown(self):
        self.writer.close()
        await self.writer.wait_closed()
        await self.server.stop()
        await self.bridge.stop()

    async def read_frame(self):
        header = await asyncio.wait_for(self.reader.readexactly(3), 2)
        self.assertEqual(header[0], codes.FRAME_OUTBOUND_PREFIX)
        return await asyncio.wait_for(
            self.reader.readexactly(struct.unpack("<H", header[1:])[0]), 2)

    async def command(self, command, data=b""):
        payload = bytes([command]) + data
        self.writer.write(bytes([codes.FRAME_INBOUND_PREFIX])
                          + struct.pack("<H", len(payload)) + payload)
        await self.writer.drain()
        return await self.read_frame()

    def enable_sqlite(self):
        directory = tempfile.TemporaryDirectory(prefix="companion-tcp-")
        self.addCleanup(directory.cleanup)
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            database = SQLiteHandler(Path(directory.name))
        self.store = ClosingSQLiteStore(database)
        self.bridge._sqlite_handler = self.store
        self.server.sqlite_handler = self.store
        return database

    async def test_sqlite_preference_failure_preserves_state_and_allows_retry(self):
        database = self.enable_sqlite()
        self.assertEqual(await self.command(codes.CMD_SET_ADVERT_NAME, b"Saved"), b"\x00")
        key = self.bridge._companion_hash
        saved = self.store.companion_load_prefs(key)
        for failure in ({"return_value": False}, {"side_effect": OSError("disk failure")}):
            with self.subTest(failure=failure), patch.object(database, "companion_save_prefs", **failure):
                self.assertEqual(await self.command(codes.CMD_SET_ADVERT_NAME, b"Unsaved"),
                                 bytes([codes.RESP_CODE_ERR, codes.ERR_CODE_FILE_IO_ERROR]))
            self.assertEqual(self.bridge.prefs.node_name, "Saved")
            self.assertEqual(self.store.companion_load_prefs(key), saved)
        self.assertEqual(await self.command(codes.CMD_SET_ADVERT_NAME, b"Retry"), b"\x00")
        restored = self.make_bridge()
        try:
            self.assertEqual(restored.prefs.node_name, "Retry")
        finally:
            await restored.stop()

    async def test_unicode_scope_matches_wire_and_persisted_preferences(self):
        self.enable_sqlite()
        name, key = "x" + "ä" * 20, bytes(range(16))
        self.assertTrue(self.bridge.set_default_flood_scope(name, key))
        expected = "x" + "ä" * 14  # 29 bytes; the next character would be split.
        frame = await self.command(codes.CMD_GET_DEFAULT_FLOOD_SCOPE)
        self.assertEqual(frame[0], codes.RESP_CODE_DEFAULT_FLOOD_SCOPE)
        self.assertEqual(frame[1:32].split(b"\0", 1)[0].decode("utf-8"), expected)
        self.assertEqual(frame[32:], key)
        restored = self.make_bridge()
        try:
            self.assertEqual(restored.get_default_flood_scope(), (expected, key))
        finally:
            await restored.stop()

    async def test_failed_bind_can_retry_without_duplicate_callbacks(self):
        port = self.server._server.sockets[0].getsockname()[1]
        bridge = self.make_bridge()
        server = CompanionFrameServer(bridge, "02", port=port, bind_address="127.0.0.1")
        await bridge.start()
        try:
            with self.assertRaises(OSError):
                await server.start()
            callbacks = {event: tuple(items) for event, items in bridge._push_callbacks.items()}
            server.port = 0
            await server.start()
            self.assertTrue(server._server.is_serving())
            self.assertEqual(callbacks, {event: tuple(items)
                                        for event, items in bridge._push_callbacks.items()})
        finally:
            await server.stop()
            await bridge.stop()

    async def test_meshy_handshake_and_settings_save(self):
        frame = await self.command(codes.CMD_DEVICE_QUERY, b"\x03")
        self.assertEqual(frame[0], codes.RESP_CODE_DEVICE_INFO)
        frame = await self.command(codes.CMD_APP_START, bytes(7) + b"Meshy")
        self.assertEqual(frame[0], codes.RESP_CODE_SELF_INFO)
        self.assertEqual(frame[2:4], bytes([27, 27]))
        self.assertEqual(struct.unpack_from("<IIBB", frame, 48), (869618, 62500, 8, 8))

        for sf in (8, 11):  # EU Narrow and EU Long Range, same shared host radio
            reply = await self.command(codes.CMD_SET_RADIO_PARAMS,
                                       struct.pack("<IIBB", 869618, 62500, sf, 8))
            self.assertEqual(reply, bytes([codes.RESP_CODE_OK]))
        self.assertEqual(await self.command(codes.CMD_SET_RADIO_TX_POWER, b"\x1b"), b"\x00")
        self.assertEqual(await self.command(codes.CMD_SET_ADVERT_NAME, b"EU companion"), b"\x00")
        self.assertEqual(await self.command(codes.CMD_SET_ADVERT_LATLON,
                                           struct.pack("<ii", 48000000, 16000000)), b"\x00")
        self.assertEqual(await self.command(codes.CMD_SET_OTHER_PARAMS, b"\x01\x15\x01\x01"), b"\x00")
        self.assertEqual(await self.command(codes.CMD_SET_AUTOADD_CONFIG, b"\x0e\x02"), b"\x00")
        self.assertEqual(await self.command(codes.CMD_SET_PATH_HASH_MODE, b"\x00\x01"), b"\x00")
        # The host's actual RF state stays authoritative after client writes.
        self.assertEqual(self.bridge.get_self_info().spreading_factor, 8)
        self.assertEqual(self.backend._runtime_radio_config["channel_e"]["coding_rate"], "4/8")
        restored = self.make_bridge()
        try:
            self.assertEqual(restored.prefs.node_name, "EU companion")
            self.assertEqual(restored.prefs.latitude, 48)
            self.assertEqual(restored.prefs.autoadd_max_hops, 2)
            self.assertEqual(restored.prefs.path_hash_mode, 1)
        finally:
            await restored.stop()

    async def test_nearby_discovery_request_and_reply(self):
        tag = 0x12345678
        reply = await self.command(codes.CMD_SEND_CONTROL_DATA,
                                   b"\x81\xff" + struct.pack("<I", tag))
        self.assertEqual(reply, bytes([codes.RESP_CODE_OK]))
        sent = self.injector.await_args.args[0]
        self.assertTrue(sent.is_route_direct())
        self.assertEqual(sent.get_path_hash_count(), 0)
        self.assertEqual(bytes(sent.payload), b"\x81\xff" + struct.pack("<I", tag))
        peer = bytes(range(32, 64))
        packet = PacketBuilder.create_discovery_response(tag, 2, 4.5, peer, prefix_only=True)
        packet._snr, packet._rssi = 6.0, -80
        await self.router._route_packet(packet)
        frame = await self.read_frame()
        self.assertEqual(frame[:4], bytes([codes.PUSH_CODE_CONTROL_DATA, 24, 176, 0]))
        self.assertEqual(frame[4:6], bytes([0x92, 18]))
        self.assertEqual(struct.unpack_from("<I", frame, 6)[0], tag)
        self.assertEqual(frame[10:], peer[:8])
        # A refused RF send must remain visible, not become an empty successful scan.
        self.injector.return_value = False
        reply = await self.command(codes.CMD_SEND_CONTROL_DATA,
                                   b"\x81\xff" + struct.pack("<I", tag + 1))
        self.assertEqual(reply, bytes([codes.RESP_CODE_ERR, codes.ERR_CODE_TABLE_FULL]))

    async def test_legacy_radio_preferences_and_direct_bridge_construction(self):
        # Older bridges could save a HAL-format string in their JSON prefs.
        key = self.bridge._companion_hash
        self.store.rows[key] = {"coding_rate": "4/8", "frequency_hz": 915000000,
                                "node_name": "Saved name"}
        restored = RepeaterCompanionBridge(
            self.identity, self.injector, sqlite_handler=self.store,
            radio_config=self.backend.get_radio_settings(),
            radio_settings_getter=self.backend.get_radio_settings,
        )
        try:
            self.assertEqual(restored.prefs.coding_rate, 8)
            self.assertEqual(restored.get_self_info().frequency_hz, 869618000)
            restored.set_advert_name("Updated name")
            self.assertEqual(self.store.rows[key]["node_name"], "Updated name")
            self.assertEqual(self.store.rows[key]["coding_rate"], 8)
        finally:
            await restored.stop()

    async def test_radio_thread_notification_wakes_tcp_writer(self):
        await self.command(codes.CMD_APP_START, bytes(7))
        # IsolatedAsyncioTestCase enables loop debugging, so an off-loop Queue
        # write also raises instead of silently waiting for the next heartbeat.
        raw = b"\x3d\x00hello"
        await asyncio.to_thread(self.server.push_rx_raw, 4.5, -80, raw)
        self.assertEqual(await self.read_frame(), bytes([codes.PUSH_CODE_LOG_RX_DATA, 18, 176]) + raw)

    async def test_reconnect_resets_protocol_version_and_partial_signing(self):
        await self.command(codes.CMD_DEVICE_QUERY, b"\x03")
        await self.command(codes.CMD_SIGN_START)
        self.assertEqual(await self.command(codes.CMD_SIGN_DATA, b"unfinished"), b"\x00")
        old_writer = self.writer
        port = self.server._server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        await self.command(codes.CMD_APP_START, bytes(7))
        old_writer.close()
        await old_writer.wait_closed()
        self.assertEqual(await self.command(codes.CMD_SIGN_FINISH),
                         bytes([codes.RESP_CODE_ERR, codes.ERR_CODE_BAD_STATE]))
        self.bridge.message_queue.push(QueuedMessage(
            sender_key=bytes(range(32)), timestamp=100, text="legacy client"))
        message = await self.command(codes.CMD_SYNC_NEXT_MESSAGE)
        self.assertEqual(message[0], codes.RESP_CODE_CONTACT_MSG_RECV)
        self.assertTrue(message.endswith(b"legacy client"))

    async def test_repeated_start_keeps_listener_and_callbacks(self):
        listener = self.server._server
        callbacks = {event: tuple(items) for event, items in self.bridge._push_callbacks.items()}
        try:
            await asyncio.gather(self.server.start(), self.server.start())
            self.assertIs(self.server._server, listener)
            self.assertEqual(callbacks, {event: tuple(items)
                                        for event, items in self.bridge._push_callbacks.items()})
            self.assertEqual((await self.command(codes.CMD_APP_START, bytes(7)))[0], codes.RESP_CODE_SELF_INFO)
        finally:
            self.writer.close()
            await self.writer.wait_closed()
            listener.close()
            await listener.wait_closed()

    async def test_same_named_channels_keep_selected_slot_for_tx_and_rx(self):
        await self.command(codes.CMD_DEVICE_QUERY, b"\x03")
        first, second = bytes(range(16)), bytes(range(16, 32))
        self.bridge.channels.set(1, Channel("same name", first))
        self.bridge.channels.set(7, Channel("same name", second))
        self.assertTrue(await self.bridge.send_channel_message(7, "outbound", timestamp=123))
        sent = self.injector.await_args.args[0]
        expected = PacketBuilder.create_group_datagram(
            "same name", self.identity, "outbound", sender_name=self.bridge.prefs.node_name,
            channels_config=[{"name": "same name", "secret": second.hex()}], timestamp=123)
        self.assertEqual(bytes(sent.payload), bytes(expected.payload))
        packet = PacketBuilder.create_group_datagram(
            "same name", self.identity, "inbound", sender_name="Peer",
            channels_config=[{"name": "same name", "secret": second.hex()}], timestamp=124)
        await self.bridge.process_received_packet(packet)
        self.assertEqual(await self.read_frame(), bytes([codes.PUSH_CODE_MSG_WAITING]))
        received = await self.command(codes.CMD_SYNC_NEXT_MESSAGE)
        self.assertEqual(received[0], codes.RESP_CODE_CHANNEL_MSG_RECV_V3)
        self.assertEqual(received[4], 7)
        self.assertTrue(received.endswith(b"Peer: inbound"))

    async def test_contact_sync_sees_same_second_updates_and_clock_correction(self):
        await self.command(codes.CMD_APP_START, bytes(7))
        peer = bytes(range(32, 64))
        self.bridge.contacts.add(Contact(peer, name="Peer", adv_type=1,
                                         lastmod=100, last_advert_timestamp=42))
        with patch("repeater.companion.contact_commands.time.time", return_value=100):
            await self.server._handle_contact_path_update(peer, 0, b"")
        self.assertEqual((await self.read_frame())[0], codes.PUSH_CODE_PATH_UPDATED)
        self.assertEqual(self.bridge.contacts.get_by_key(peer).lastmod, 101)
        with patch("repeater.companion.contact_commands.time.time", return_value=90):
            await self.bridge._handle_new_message({
                "contact_pubkey": peer.hex(), "message_text": "hello", "timestamp": 100,
            })
        self.assertEqual(await self.read_frame(), bytes([codes.PUSH_CODE_MSG_WAITING]))
        self.assertEqual(self.bridge.contacts.get_by_key(peer).lastmod, 102)
        with patch("repeater.companion.contact_commands.time.time", return_value=90):
            await self.server._handle_advert_event({
                "public_key": peer, "name": "Renamed", "adv_type": 1,
                "advert_timestamp": 43, "timestamp": 90,
            })
        self.assertEqual((await self.read_frame())[0], codes.PUSH_CODE_ADVERT)
        self.assertEqual((await self.command(codes.CMD_GET_CONTACTS, struct.pack("<I", 101)))[0],
                         codes.RESP_CODE_CONTACTS_START)
        contact = await self.read_frame()
        self.assertEqual(contact[0], codes.RESP_CODE_CONTACT)
        self.assertEqual(struct.unpack("<I", contact[-4:])[0], 103)
        self.assertEqual(await self.read_frame(), bytes([codes.RESP_CODE_END_OF_CONTACTS])
                         + struct.pack("<I", 103))


if __name__ == "__main__":
    unittest.main()
