"""Detached contact access for the retained text-message decoder."""

from dataclasses import replace

from openhop_core.companion.contact_store import ContactProxy


class MessageContacts:
    """Keep eager decoder sync updates local until the message owner saves them."""

    def __init__(self, contacts):
        self._contacts = contacts

    @property
    def contacts(self):
        # TextMessageHandler both reads proxy fields for decryption/ACK routing
        # and writes proxy.sync_since plus proxy._contact.sync_since. Detach
        # both objects, while preserving current routes and identity metadata.
        return [ContactProxy(replace(contact)) for contact in self._contacts.get_all()]

    def update(self, contact):
        # The decoder only updates its detached snapshot. Publication belongs
        # to the message persistence owner, using the subsequently emitted event.
        return True
