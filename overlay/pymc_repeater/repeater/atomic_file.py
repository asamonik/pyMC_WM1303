"""Durable replacement of configuration files without partial writes."""

import os
import logging
from pathlib import Path
import stat
import tempfile

logger = logging.getLogger(__name__)


def atomic_write_text(
    filename, content: str, *, overwrite: bool = True, mode: int | None = None
) -> None:
    """Replace UTF-8 text atomically, preserving existing permissions and links.

    Serialize before calling this function. Failures before replacement leave
    the old file intact; readers see either the old or complete new content.
    A directory-flush failure after replacement is logged as a durability warning
    because the new file is already committed and must also be used in memory.
    New files are private because configuration can contain identity keys.
    An explicit mode overrides new or existing permissions before publication,
    while preserving existing ownership; None keeps the default behavior.
    With overwrite=False, publish a new complete file only if absent; an
    existing target raises FileExistsError without changing either its data
    or permissions. This lets identity creation safely handle concurrent starts.
    """
    target = Path(filename).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = target.stat()
    except FileNotFoundError:
        previous = None

    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    published = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if previous is not None:
                current = os.fstat(stream.fileno())
                if (current.st_uid, current.st_gid) != (previous.st_uid, previous.st_gid):
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            if mode is not None:
                os.fchmod(stream.fileno(), mode)
            elif previous is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            if overwrite:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
            published = True
            try:
                os.fsync(directory)
            except OSError as exc:
                logger.warning("Saved %s but could not flush its directory: %s", target, exc)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if not published:
                raise
            logger.warning("Saved %s but could not remove temporary file %s: %s",
                           target, temporary, exc)
