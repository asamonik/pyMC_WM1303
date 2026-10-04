"""Advertised node names remain separate from Neighbor Links peer identities."""

import ast
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Optional
import unittest
from unittest.mock import Mock, patch

import cherrypy


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "overlay/pymc_repeater"))


def load_class(relative_path, class_name, methods):
    source = ROOT / "overlay/pymc_repeater/repeater" / relative_path
    cls = next(node for node in ast.parse(source.read_text()).body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    cls.body = [node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name in methods]
    namespace = dict(cherrypy=cherrypy, logger=Mock(), time=time, Optional=Optional)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[class_name]


class NeighborLinksTests(unittest.TestCase):
    def setUp(self):
        api_class = load_class("web/api_endpoints.py", "APIEndpoints", {
            "_success", "_nl_rx_score", "_nl_get_storage_safe", "_nl_friendly_name",
            "neighbor_links", "neighbor_link_history",
        })
        self.api = api_class()
        self.storage = SimpleNamespace(
            get_neighbors=Mock(return_value={}),
            get_node_name_by_pubkey=Mock(return_value=None),
            get_neighbour_samples=Mock(return_value=[]),
        )
        self.api._get_storage = lambda: self.storage
        self.public_key = "ab" * 32

    def test_advertised_name_is_trimmed_and_raw_identity_is_preserved(self):
        self.storage.get_neighbors.return_value = {
            self.public_key: dict(node_name="  Hügel Süd  ", friendly_name="Old name",
                                 rssi=-78, snr=6.25, last_seen=time.time(), advert_count=3),
        }
        result = self.api.neighbor_links()
        self.assertTrue(result["success"], result)
        link = result["data"]["links"][0]
        self.assertEqual(link["friendly_name"], "Hügel Süd")
        self.assertEqual(link["peer_hash"], self.public_key)
        self.assertEqual((link["last_rssi"], link["last_snr"]), (-78, 6.25))
        self.assertEqual(link["sample_count"], 3)
        self.assertTrue(link["active"])
        self.storage.get_node_name_by_pubkey.assert_not_called()
        json.dumps(result)

    def test_list_entries_use_public_key_and_ignore_blank_or_nonstring_names(self):
        self.storage.get_neighbors.return_value = [
            dict(pubkey=self.public_key, node_name="  ", friendly_name="  Hilltop  "),
            dict(public_key="cd" * 32, node_name=123),
        ]
        links = self.api.neighbor_links()["data"]["links"]
        self.assertEqual([(row["peer_hash"], row["friendly_name"]) for row in links],
                         [(self.public_key, "Hilltop"), ("cd" * 32, "")])

    def test_advert_lookup_uses_full_public_key_when_peer_has_separate_node_id(self):
        key = "0x" + self.public_key.upper()
        self.storage.get_neighbors.return_value = {key: dict(node_id="AB")}
        self.storage.get_node_name_by_pubkey.side_effect = (
            lambda candidate: "  Hilltop  " if candidate == self.public_key else None)
        link = self.api.neighbor_links()["data"]["links"][0]
        self.assertEqual(link["peer_hash"], "AB")
        self.assertEqual(link["friendly_name"], "Hilltop")
        self.assertEqual(self.storage.get_node_name_by_pubkey.call_args_list,
                         [unittest.mock.call(key), unittest.mock.call(self.public_key)])

    def test_unavailable_name_lookup_keeps_link_and_raw_hash_fallback(self):
        self.storage.get_neighbors.return_value = {self.public_key: dict(rssi=-92, snr=0)}
        self.storage.get_node_name_by_pubkey.side_effect = RuntimeError("database busy")
        result = self.api.neighbor_links()
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["total_links"], 1)
        self.assertEqual(result["data"]["links"][0]["friendly_name"], "")
        self.assertEqual(result["data"]["links"][0]["peer_hash"], self.public_key)
        del self.storage.get_node_name_by_pubkey
        self.assertEqual(self.api.neighbor_links()["data"]["total_links"], 1)

    def test_history_name_does_not_change_storage_query_or_time_filter(self):
        now = time.time()
        self.storage.get_node_name_by_pubkey.return_value = "Hilltop"
        self.storage.get_neighbour_samples.return_value = [
            dict(ts=now - 7200, rssi=-99, snr=1),
            dict(ts=now - 10, rssi=-82, snr=4.5, channel="E"),
        ]
        result = self.api.neighbor_link_history(peer_hash=self.public_key, hours="1", limit="50")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["peer_hash"], self.public_key)
        self.assertEqual(result["data"]["friendly_name"], "Hilltop")
        self.assertEqual(result["data"]["count"], 1)
        self.assertEqual(result["data"]["rows"][0]["channel"], "E")
        self.storage.get_neighbour_samples.assert_called_once_with(self.public_key, limit=50)

    def test_sqlite_advert_and_sample_names_survive_restart_and_key_normalization(self):
        from repeater.data_acquisition.sqlite_handler import SQLiteHandler

        directory = self.enterContext(tempfile.TemporaryDirectory())
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            db = SQLiteHandler(Path(directory))
        db.store_advert(dict(pubkey=self.public_key, node_name="Hügel Süd", zero_hop=True,
                             rssi=-83, snr=7, contact_type="Repeater"))
        db.record_neighbour_sample(self.public_key, -83, 7, "E")
        db.close_thread_connection()
        with patch.object(SQLiteHandler, "_start_wal_checkpoint_thread"):
            reopened = SQLiteHandler(Path(directory))
        self.addCleanup(reopened.close_thread_connection)
        storage_class = load_class("data_acquisition/storage_collector.py", "StorageCollector", {
            "get_neighbors", "get_node_name_by_pubkey", "get_neighbour_samples",
        })
        self.storage = storage_class()
        self.storage.sqlite_handler = reopened
        link = self.api.neighbor_links()["data"]["links"][0]
        self.assertEqual((link["peer_hash"], link["friendly_name"]),
                         (self.public_key, "Hügel Süd"))
        data = self.api.neighbor_link_history(peer_hash=self.public_key)["data"]
        self.assertEqual(data["friendly_name"], "Hügel Süd")
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.api._nl_friendly_name(self.storage, "0x" + self.public_key.upper()),
                         "Hügel Süd")

    def test_unavailable_storage_returns_empty_analytics(self):
        self.api._get_storage = Mock(side_effect=RuntimeError("storage unavailable"))
        self.assertEqual(self.api.neighbor_links()["data"]["links"], [])
        data = self.api.neighbor_link_history(peer_hash=self.public_key)["data"]
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["friendly_name"], "")


if __name__ == "__main__":
    unittest.main()
