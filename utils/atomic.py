"""Atomic file write utilities — no dependencies on services to avoid circular imports."""
from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically write text to ``path``.

    Writes to a temporary file in the same directory, then ``os.replace`` it
    (atomic on POSIX). If the process crashes mid-write, the target file is
    left intact (either the old version or a complete new version, never a
    truncated half-written file).

    Docker single-file bind mounts (``./config.json:/app/config.json``) pin the
    target inode as a mount root, so ``os.replace`` returns ``EBUSY``. In that
    case the content is written in place over the existing file (keeps the
    original inode/mode) so settings saves keep working under both mount styles.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.stem}.",
        suffix=path.suffix + ".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp_name, str(path))
        except OSError as exc:
            # Mounted single file: rename over the mount root → EBUSY (16).
            if exc.errno != errno.EBUSY:
                raise
            with open(path, "wb") as out:
                out.write(content.encode(encoding))
                out.flush()
                os.fsync(out.fileno())
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def atomic_write_json(path: Path, data: Any, *, indent: int = 2, ensure_ascii: bool = False) -> None:
    """Atomically write JSON data to ``path``."""
    content = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent) + "\n"
    atomic_write_text(path, content)
