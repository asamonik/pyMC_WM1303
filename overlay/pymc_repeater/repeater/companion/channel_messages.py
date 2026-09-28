"""Keep channel slot identity through group-message TX and authenticated RX."""

import logging
import time

from openhop_core.companion.models import ChannelMessageEvent, QueuedMessage
from openhop_core.protocol import PacketBuilder

logger = logging.getLogger(__name__)


class ChannelMessagesMixin:
    async def send_channel_message(self, channel_idx, text, timestamp=None):
        channel = self.channels.get(channel_idx)
        if channel is None or self._stop_task is not None:
            return False
        try:
            # The packet builder looks up by name. Names are not unique, so
            # pass only the exact slot the client selected.
            packet = PacketBuilder.create_group_datagram(
                group_name=channel.name, local_identity=self._identity,
                message=text, sender_name=self.prefs.node_name,
                channels_config=[{"name": channel.name, "secret": channel.secret.hex()}],
                timestamp=timestamp,
            )
            self._apply_flood_scope(packet)
            self._apply_path_hash_mode(packet)
            self._check_and_track_group_packet(packet)
            sent = await self._send_packet(packet, wait_for_ack=False)
            if sent:
                self.stats.record_tx(is_flood=True)
            else:
                self.stats.record_tx_error()
            return sent
        except Exception as exc:
            logger.warning("Companion channel send failed (%s)", type(exc).__name__)
            self.stats.record_tx_error()
            return False

    async def _handle_new_channel_message(self, data):
        index = data.get("channel_idx")
        if index is None:
            # Retain compatibility with legacy producers without slot metadata.
            return await super()._handle_new_channel_message(data)
        if data.get("is_outgoing"):
            return
        if type(index) is not int or self.channels.get(index) is None:
            return
        packet_hash = data.get("packet_hash")
        if packet_hash and self._seen_grp_txt.check_and_add(packet_hash):
            return
        text = (data.get("full_content", data.get("message_text", "")) or "").rstrip("\x00")
        network = data.get("network_info") or {}
        snr, rssi = network.get("snr"), network.get("rssi")
        message = QueuedMessage(
            sender_key=b"", timestamp=data.get("timestamp", int(time.time())),
            text=text, is_channel=True, channel_idx=index, path_len=data.get("path_len", 0),
            snr=snr if snr is not None else 0.0, rssi=rssi if rssi is not None else 0,
        )
        queued = self.message_queue.push(message)
        await self._fire_callbacks("channel_message_event", ChannelMessageEvent(
            channel_name=data.get("channel_name", ""), sender_name=data.get("sender_name", ""),
            text=text, timestamp=message.timestamp, path_len=message.path_len,
            channel_idx=index, packet_hash=packet_hash, snr=snr, rssi=rssi,
            queued=queued, queue_entry=message if queued else None,
        ))
