"""Owned activation and health monitoring for hosted room servers."""

import asyncio
import logging
from copy import deepcopy


logger = logging.getLogger("RepeaterDaemon")


class RoomLifecycleMixin:
    def _room_handler_maps(self):
        if not all((self.login_helper, self.text_helper, self.protocol_request_helper)):
            raise RuntimeError("Room helpers are not initialized")
        return {
            "acl": self.login_helper.acls,
            "login": self.login_helper.handlers,
            "text": self.text_helper.handlers,
            "protocol": self.protocol_request_helper.handlers,
            "room": self.text_helper.room_servers,
        }

    def _require_room_ready(self, name, identity, *, entries=None):
        """Validate the exact objects, not helper registration's None return."""
        room_hash = identity.get_public_key()[0]
        if entries is None:
            entries = {label: mapping.get(room_hash)
                       for label, mapping in self._room_handler_maps().items()}
        acl = entries.get("acl")
        if acl is None or getattr(entries.get("login"), "local_identity", None) is not identity:
            raise RuntimeError(f"Room server {name!r} has no matching login handler/ACL")
        for label in ("text", "protocol"):
            entry = entries.get(label) or {}
            handler = entry.get("handler")
            if (entry.get("identity") is not identity
                    or getattr(handler, "local_identity", None) is not identity
                    or getattr(getattr(handler, "contacts", None), "_acl", None) is not acl):
                raise RuntimeError(f"Room server {name!r} has no matching {label} handler/ACL")
        room = entries.get("room")
        sync_task = getattr(room, "_sync_task", None)
        if (getattr(room, "local_identity", None) is not identity
                or getattr(room, "acl", None) is not acl
                or not getattr(room, "_running", False)
                or sync_task is None or sync_task.done()):
            raise RuntimeError(f"Room server {name!r} has no running sync task")
        sync_task.set_name(f"room server {name!r} sync")
        return room

    def _monitor_room_sync(self, name, room):
        """Wake one supervisor for both startup rooms and later hot additions."""
        def finished(task):
            if (self._shutdown_started
                    or self.text_helper.room_servers.get(room.room_hash) is not room):
                return
            if self._room_sync_failure is None:
                self._room_sync_failure = (name, task)
                self._room_sync_failure_event.set()

        room._sync_task.add_done_callback(finished)

    async def _supervise_room_sync(self):
        await self._room_sync_failure_event.wait()
        name, task = self._room_sync_failure
        if task.cancelled():
            raise RuntimeError(f"Room server {name!r} sync was cancelled unexpectedly")
        try:
            task.result()
        except Exception as exc:
            raise RuntimeError(f"Room server {name!r} sync failed") from exc
        raise RuntimeError(f"Room server {name!r} sync stopped unexpectedly")

    async def add_room_from_config(self, room_config):
        """Keep accepted activation/rollback alive if its HTTP waiter leaves."""
        if self._shutdown_started:
            raise RuntimeError("Cannot add a room while the daemon is shutting down")
        task = asyncio.create_task(
            self._activate_room_from_config(deepcopy(room_config)),
            name="room server activation",
        )
        self._room_activation_tasks.add(task)

        def finished(done):
            self._room_activation_tasks.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logger.warning("Room activation failed (%s)", type(error).__name__)

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _drain_room_activations(self):
        # _shutdown_started closes admission before this is called. Workers
        # waiting for the shared lock recheck it before building anything.
        while getattr(self, "_room_activation_tasks", ()):
            await asyncio.gather(
                *(asyncio.shield(task) for task in tuple(self._room_activation_tasks)),
                return_exceptions=True,
            )

    def _check_saved_room_spec(self, config):
        """Caller holds the configuration lock; never keep it across awaits."""
        saved = self.config_manager.read_saved_config()
        entries = (saved.get("identities") or {}).get("room_servers") or []
        matches = [entry for entry in entries
                   if isinstance(entry, dict) and entry.get("name") == config["name"]]
        if len(matches) != 1 or matches[0] != config:
            raise RuntimeError(f"Saved room configuration for {config['name']!r} changed before activation")

    async def _activate_room_from_config(self, config):
        from openhop_core import LocalIdentity

        # Companion startup also publishes after awaits. A single lock keeps
        # pending rooms and companions from reserving the same name/hash.
        async with self._companion_activation_lock:
            if self._shutdown_started:
                raise RuntimeError("Cannot add a room while the daemon is shutting down")
            if not isinstance(config, dict):
                raise ValueError("Room configuration must be a mapping")
            name = config.get("name")
            if not isinstance(name, str) or not name.strip() or name == "repeater":
                raise ValueError("Room name is invalid or reserved")
            if not isinstance(config.get("settings", {}), dict):
                raise ValueError("Room settings must be a mapping")
            key = config.get("identity_key")
            try:
                key = bytes.fromhex(key) if isinstance(key, str) else key
            except ValueError:
                raise ValueError("Room identity key must be valid hexadecimal") from None
            if not isinstance(key, bytes) or len(key) not in (32, 64):
                raise ValueError("Room identity key must contain 32 or 64 bytes")
            identity = LocalIdentity(seed=key)
            room_hash = identity.get_public_key()[0]
            maps = self._room_handler_maps()
            manager = self.identity_manager
            if (name in manager.named_identities or manager.has_identity(room_hash)
                    or room_hash == self.local_hash or room_hash in self.companion_bridges
                    or any(room_hash in mapping for mapping in maps.values())):
                raise ValueError(f"Room server {name!r} conflicts with an active identity")
            if self.config_manager is None:
                raise RuntimeError("Room configuration manager is unavailable")
            with self.config_manager._lock:
                self._check_saved_room_spec(config)

            entries = {}
            start_tasks = set()
            pending_before = set(self.text_helper._pending_tasks)
            try:
                try:
                    self.login_helper.register_identity(
                        name=name, identity=identity, identity_type="room_server", config=config,
                    )
                    self.text_helper.register_identity(
                        name=name, identity=identity, identity_type="room_server", radio_config=config,
                    )
                    self.protocol_request_helper.register_identity(
                        name=name, identity=identity, identity_type="room_server",
                    )
                finally:
                    # These slots were all empty, and construction has not
                    # yielded. Detach even if a helper raised after publishing
                    # only part of its state. RX must not see a provisional room.
                    start_tasks.update(self.text_helper._pending_tasks - pending_before)
                    for label, mapping in maps.items():
                        if room_hash in mapping:
                            entries[label] = mapping.pop(room_hash)

                for task in start_tasks:
                    await asyncio.shield(task)
                await asyncio.sleep(0)
                room = self._require_room_ready(name, identity, entries=entries)
                if self._shutdown_started:
                    raise RuntimeError("Room activation interrupted by daemon shutdown")

                # Persisted edits/deletion may race startup. Recheck only this
                # spec, then publish helpers and manager with no await while the
                # same lock prevents a concurrent configuration replacement.
                with self.config_manager._lock:
                    self._check_saved_room_spec(config)
                    if (name in manager.named_identities or manager.has_identity(room_hash)
                            or any(room_hash in mapping for mapping in maps.values())):
                        raise ValueError(f"Room server {name!r} conflicts with an active identity")
                    for label, entry in entries.items():
                        maps[label][room_hash] = entry
                    if not manager.register_identity(
                        name=name, identity=identity, config=config, identity_type="room_server",
                    ):
                        raise RuntimeError(f"Room server {name!r} could not register its identity")
                    self._monitor_room_sync(name, room)
                logger.info("Room server %r activated", name)
                return True
            except BaseException:
                # Only remove the exact objects created by this activation.
                # The durable saved entry remains available for a later restart.
                for label, entry in entries.items():
                    if maps[label].get(room_hash) is entry:
                        maps[label].pop(room_hash)
                registered = manager.identities.get(room_hash)
                if registered is not None and registered[0] is identity:
                    manager.identities.pop(room_hash)
                    manager.registered_hashes.pop(room_hash, None)
                    named = manager.named_identities.get(name)
                    if named is not None and named[0] is identity:
                        manager.named_identities.pop(name)
                for task in start_tasks:
                    task.cancel()
                if start_tasks:
                    await asyncio.gather(*start_tasks, return_exceptions=True)
                room = entries.get("room")
                if room is not None:
                    try:
                        await room.stop()
                    except Exception as exc:
                        # A failed sync task is already stopped; retain the
                        # activation error rather than replacing it on join.
                        logger.warning("Room rollback stop failed (%s)", type(exc).__name__)
                raise
