"""Non-mutating validation for hosted-room settings at save and load boundaries."""

MAX_ROOM_CLIENTS = 50
MAX_LOGIN_PASSWORD_BYTES = 15


def validate_room_settings(settings, *, path="room.settings"):
    if not isinstance(settings, dict):
        raise ValueError(f"{path} must be a mapping")
    if "allow_read_only" in settings and type(settings["allow_read_only"]) is not bool:
        raise ValueError(f"{path}.allow_read_only must be a boolean")
    if "max_clients" in settings:
        capacity = settings["max_clients"]
        if type(capacity) is not int or not 0 <= capacity <= MAX_ROOM_CLIENTS:
            raise ValueError(f"{path}.max_clients must be an integer from 0 to {MAX_ROOM_CLIENTS}")
    if "max_posts" in settings:
        limit = settings["max_posts"]
        if type(limit) is not int or limit < 1:
            raise ValueError(f"{path}.max_posts must be a positive integer")
        # Values above the existing post retention cap remain valid: the room
        # applies that cap at construction, without rewriting desired settings.
    for field in ("admin_password", "guest_password"):
        password = settings.get(field)
        if password is None:
            continue  # Missing, null and empty passwords disable that role.
        if not isinstance(password, str):
            raise ValueError(f"{path}.{field} must be a string or null")
        try:
            encoded = password.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(f"{path}.{field} must contain valid UTF-8 text") from None
        if b"\x00" in encoded or len(encoded) > MAX_LOGIN_PASSWORD_BYTES:
            raise ValueError(
                f"{path}.{field} must fit {MAX_LOGIN_PASSWORD_BYTES} UTF-8 bytes without NUL"
            )
    if (settings.get("admin_password")
            and settings["admin_password"] == settings.get("guest_password")):
        raise ValueError(f"{path}.admin_password and guest_password must be different")


def validate_room_configuration(config):
    """Validate complete candidates, after merges/redacted-secret restoration."""
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a mapping")
    identities = config.get("identities")
    if identities is None:
        return
    if not isinstance(identities, dict):
        raise ValueError("identities must be a mapping")
    rooms = identities.get("room_servers")
    if rooms is None:
        return
    if not isinstance(rooms, list):
        raise ValueError("identities.room_servers must be a list")
    for index, room in enumerate(rooms):
        if not isinstance(room, dict):
            raise ValueError(f"identities.room_servers[{index}] must be a mapping")
        if room.get("type", "room_server") != "room_server":
            raise ValueError(f"identities.room_servers[{index}].type must be 'room_server'")
        validate_room_settings(
            room.get("settings", {}), path=f"identities.room_servers[{index}].settings",
        )
