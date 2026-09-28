"""Distinguish failed preference persistence from invalid client arguments."""

import functools
import logging

from openhop_core.companion.constants import (
    CMD_SET_ADVERT_LATLON,
    CMD_SET_ADVERT_NAME,
    CMD_SET_AUTOADD_CONFIG,
    CMD_SET_DEFAULT_FLOOD_SCOPE,
    CMD_SET_OTHER_PARAMS,
    CMD_SET_PATH_HASH_MODE,
    CMD_SET_TUNING_PARAMS,
    ERR_CODE_FILE_IO_ERROR,
)

logger = logging.getLogger(__name__)


class PreferencePersistenceError(RuntimeError):
    """The preference value is valid, but its durable save did not complete."""


class PreferenceCommandsMixin:
    def _install_preference_commands(self):
        for command in (
            CMD_SET_ADVERT_LATLON, CMD_SET_ADVERT_NAME, CMD_SET_AUTOADD_CONFIG,
            CMD_SET_DEFAULT_FLOOD_SCOPE, CMD_SET_OTHER_PARAMS, CMD_SET_PATH_HASH_MODE,
            CMD_SET_TUNING_PARAMS,
        ):
            self._cmd_handlers[command] = functools.partial(
                self._run_preference_command, self._cmd_handlers[command],
            )

    async def _run_preference_command(self, handler, data):
        try:
            await handler(data)
        except PreferencePersistenceError:
            logger.warning("Companion preferences could not be persisted")
            self._write_err(ERR_CODE_FILE_IO_ERROR)
