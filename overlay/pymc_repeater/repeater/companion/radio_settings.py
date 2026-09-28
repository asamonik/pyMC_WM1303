"""Normalize host radio settings before they enter companion preferences."""


def normalize_radio_settings(settings):
    result = dict(settings or {})
    if "coding_rate" in result:
        value = result["coding_rate"]
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("4/"):
                value = value[2:]
        if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
            raise ValueError("Invalid radio coding rate")
        value = int(value)
        # Some classic radio adapters expose the Semtech CR index (1..4).
        if 1 <= value <= 4:
            value += 4
        if not 5 <= value <= 8:
            raise ValueError("Invalid radio coding rate")
        result["coding_rate"] = value
    return result
