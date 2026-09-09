"""Room wire cursors and keep-alive responses, using an isolated SQLite store."""

import ast
import hashlib
import logging
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'overlay/pymc_repeater'))
from repeater.data_acquisition.sqlite_handler import SQLiteHandler


class Result(SimpleNamespace):
    @classmethod
    def consumed(cls, response=None):
        return cls(authenticated=True, response=response)

    @classmethod
    def not_for_us(cls):
        return cls(authenticated=False, response=None)


class CoreHandlerFixture:
    """Core collaborators only; the room adapter below is the actual source."""
    def __init__(self, local_identity, clients):
        self.local_identity, self.clients = local_identity, clients

    def _get_clients(self, prefix):
        return [c for c in self.clients if c.id.get_public_key()[0] == prefix]

    def _get_shared_secret(self, client):
        return client.shared_secret

    def _get_last_req_ts(self, client):
        return client.last_timestamp

    def _advance_client_watermark(self, client, timestamp):
        client.last_timestamp = timestamp

    async def __call__(self, packet):
        return Result.consumed('generic response')


def room_handler_class():
    source = ROOT / 'overlay/pymc_repeater/repeater/handler_helpers/protocol_request.py'
    cls = next(n for n in ast.parse(source.read_text()).body
               if isinstance(n, ast.ClassDef) and n.name == 'RoomProtocolRequestHandler')
    def ack(data, **kwargs):
        return SimpleNamespace(payload=data, **kwargs)
    namespace = dict(
        ProtocolRequestHandler=CoreHandlerFixture, HandlerResult=Result, struct=struct,
        REQ_TYPE_KEEP_ALIVE=2, logger=logging.getLogger('room-test'),
        CryptoUtils=SimpleNamespace(mac_then_decrypt=Mock(), sha256=lambda b: hashlib.sha256(b).digest()),
        PathUtils=SimpleNamespace(is_valid_path_len=lambda n: 0 <= n < 192,
                                  get_path_byte_len=lambda n: ((n >> 6) + 1) * (n & 63)),
        PacketBuilder=SimpleNamespace(create_ack_from_bytes=ack),
    )
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['RoomProtocolRequestHandler'], namespace['CryptoUtils']


class RoomSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with patch('threading.Thread.start'):
            self.db = SQLiteHandler(Path(tmp.name))
        self.addCleanup(self.db.close_thread_connection)
        self.room = '0x42'
        self.pubkey = b'c' * 32
        self.client = SimpleNamespace(
            id=SimpleNamespace(get_public_key=lambda: self.pubkey),
            shared_secret=b's' * 32, last_timestamp=10, sync_since=0,
            out_path_len=65, out_path=b'xy',
        )
        cls, self.crypto = room_handler_class()
        self.handler = cls(local_identity=SimpleNamespace(get_public_key=lambda: b'B' * 32),
                           clients=[self.client], sqlite_handler=self.db)
        self.packet = SimpleNamespace(payload=b'Bc' + b'ciphertext', is_route_direct=lambda: True)

    def post(self, timestamp, author='another client'):
        return self.db.insert_room_message(self.room, author, 'fixture text', timestamp)

    async def keep_alive(self, timestamp=11, cursor=None):
        plaintext = struct.pack('<IB', timestamp, 2)
        if cursor is not None:
            plaintext += struct.pack('<I', cursor)
        self.crypto.mac_then_decrypt.return_value = plaintext
        return await self.handler(self.packet)

    def test_posts_have_distinct_wire_cursors_across_clock_changes_and_restart(self):
        for timestamp in (100.1, 100.2, 99.8):
            self.assertTrue(self.post(timestamp))
        posts = self.db.get_unsynced_messages(self.room, self.pubkey.hex(), 0)
        self.assertEqual([p['post_timestamp'] for p in posts], [100, 101, 102])
        self.db.close_thread_connection()
        self.assertTrue(self.post(98))
        remaining = self.db.get_unsynced_messages(self.room, self.pubkey.hex(), 101)
        self.assertEqual([p['post_timestamp'] for p in remaining], [102, 103])
        self.assertEqual(self.db.get_unsynced_count(self.room, self.pubkey.hex(), 103), 0)
        for operation, expected in (('delete', 104), ('clear', 105), ('purge', 106)):
            if operation == 'delete':
                latest = self.db.get_room_messages(self.room, 1)[0]['id']
                self.assertTrue(self.db.delete_room_message(self.room, latest))
            elif operation == 'clear':
                self.db.clear_room_messages(self.room)
            else:
                self.db.purge_table('room_messages')
            self.db.close_thread_connection()
            self.db._run_migrations()
            self.assertTrue(self.post(98))
            delivered = self.db.get_unsynced_messages(self.room, self.pubkey.hex(), expected - 1)
            self.assertEqual([p['post_timestamp'] for p in delivered], [expected])

    def test_legacy_migration_preserves_history_and_ack_progress(self):
        conn = self.db._connect()
        with conn:
            conn.execute("DELETE FROM migrations WHERE migration_name = 'room_integer_post_timestamps'")
            for timestamp in (100.1, 100.2, 103.4):
                conn.execute('INSERT INTO room_messages '
                             '(room_hash, author_pubkey, message_text, post_timestamp, txt_type, created_at) '
                             'VALUES (?, ?, ?, ?, 0, ?)', (self.room, 'author', 'retained', timestamp, timestamp))
        self.db.upsert_client_sync(self.room, self.pubkey.hex(), sync_since=100.2,
                                   pending_ack_crc=99, push_post_timestamp=103.4, ack_timeout_time=200)
        self.db._run_migrations()
        self.db._run_migrations()  # Restart is idempotent.
        rows = self.db.get_unsynced_messages(self.room, self.pubkey.hex(), 0)
        self.assertEqual([r['post_timestamp'] for r in rows], [100, 101, 103])
        self.assertEqual([r['created_at'] for r in rows], [100.1, 100.2, 103.4])
        state = self.db.get_client_sync(self.room, self.pubkey.hex())
        self.assertEqual((state['sync_since'], state['pending_ack_crc']), (101, 0))
        self.assertEqual(self.db.get_unsynced_count(self.room, self.pubkey.hex(), state['sync_since']), 1)
        self.db.clear_room_messages(self.room)
        self.assertTrue(self.post(90))
        self.assertEqual(self.db.get_room_messages(self.room, 1)[0]['post_timestamp'], 104)

    def test_room_history_api_reports_committed_deletions_and_failures(self):
        import cherrypy
        source = ROOT / 'overlay/pymc_repeater/repeater/web/api_endpoints.py'
        cls = next(n for n in ast.parse(source.read_text()).body
                   if isinstance(n, ast.ClassDef) and n.name == 'APIEndpoints')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef)
                    and n.name in ('_success', '_error', 'room_message', 'room_messages_clear')]
        for method in cls.body:
            method.decorator_list = []
        namespace = dict(cherrypy=cherrypy, logger=Mock())
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), 'exec'), namespace)
        api = namespace['APIEndpoints']()
        api._set_cors_headers = lambda: None
        api._get_room_server_by_name_or_hash = lambda *args: {
            'room_server': SimpleNamespace(db=self.db), 'hash': 0x42, 'name': 'fixture'}
        first = self.post(100)
        self.db.insert_room_message('0x43', 'other', 'other room', 100)
        with patch.object(cherrypy, 'request', SimpleNamespace(method='DELETE')):
            with self.db._connect() as conn:
                conn.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON room_messages "
                             "BEGIN SELECT RAISE(ABORT, 'fixture deletion failure'); END")
            for operation in (lambda: api.room_message(message_id=first), api.room_messages_clear):
                with self.assertLogs('SQLiteHandler', level='ERROR'):
                    result = operation()
                self.assertFalse(result['success'])
                self.assertIn('fixture deletion failure', result['error'])
                self.assertEqual(self.db.get_room_message_count(self.room), 1)
            with self.db._connect() as conn:
                conn.execute('DROP TRIGGER fail_delete')
            result = api.room_messages_clear()
            self.assertTrue(result['success'])
            self.assertEqual(result['data']['deleted_count'], 1)
            self.assertEqual(api.room_messages_clear()['data']['deleted_count'], 0)
            self.assertEqual(self.db.get_room_message_count('0x43'), 1)
            self.assertFalse(api.room_message(message_id=first)['success'])

    async def test_direct_keep_alive_resets_stall_and_returns_ack_count(self):
        for timestamp, author in ((100, 'other'), (101, self.pubkey.hex()), (102, 'other')):
            self.post(timestamp, author)
        self.db.upsert_client_sync(self.room, self.pubkey.hex(), sync_since=0,
                                   pending_ack_crc=99, push_post_timestamp=100,
                                   push_failures=3, ack_timeout_time=300, last_activity=0)
        result = await self.keep_alive(cursor=100)
        preimage = struct.pack('<IBI', 11, 2, 100) + self.pubkey
        self.assertEqual(result.response.payload, hashlib.sha256(preimage).digest()[:4] + b'\x01')
        self.assertEqual((result.response.path, result.response.path_len_encoded), (b'xy', 65))
        state = self.db.get_client_sync(self.room, self.pubkey.hex())
        self.assertEqual([state[k] for k in ('sync_since', 'pending_ack_crc', 'push_failures', 'ack_timeout_time')],
                         [100, 0, 0, 0])
        self.assertGreater(state['last_activity'], 0)
        self.assertFalse(self.db.finish_room_push(self.room, self.pubkey.hex(), 99, 100, acknowledged=True))
        # Omitted/zero cursor preserves progress, including an equal-timestamp retry.
        for cursor in (None, 0):
            result = await self.keep_alive(cursor=cursor)
            expected = hashlib.sha256(struct.pack('<IBI', 11, 2, 0) + self.pubkey).digest()[:4] + b'\x01'
            self.assertEqual(result.response.payload, expected)
            self.assertEqual(self.db.get_client_sync(self.room, self.pubkey.hex())['sync_since'], 100)

    async def test_keep_alive_uses_known_direct_route_only(self):
        self.client.out_path_len = -1
        self.assertIsNone((await self.keep_alive()).response)
        self.assertIsNotNone(self.db.get_client_sync(self.room, self.pubkey.hex()))
        self.packet.is_route_direct = lambda: False
        with patch.object(self.db, 'refresh_room_keep_alive') as refresh:
            self.assertIsNone((await self.keep_alive(timestamp=12)).response)
            refresh.assert_not_called()

    async def test_failed_commit_does_not_acknowledge_or_advance_client(self):
        with patch.object(self.db, 'refresh_room_keep_alive', side_effect=sqlite3.OperationalError('disk unavailable')):
            with self.assertLogs('room-test', level='WARNING'):
                result = await self.keep_alive(cursor=100)
        self.assertTrue(result.authenticated)
        self.assertIsNone(getattr(result, 'response', None))
        self.assertEqual((self.client.last_timestamp, self.client.sync_since), (10, 0))
        self.assertIsNone(self.db.get_client_sync(self.room, self.pubkey.hex()))

    async def test_other_requests_retain_generic_handler(self):
        self.crypto.mac_then_decrypt.return_value = struct.pack('<IB', 11, 1)
        self.assertEqual((await self.handler(self.packet)).response, 'generic response')


if __name__ == '__main__':
    unittest.main()
