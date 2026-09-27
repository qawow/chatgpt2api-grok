"""Atomic file write utilities — no dependencies on services to avoid circular imports."""
from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

# mkstemp already creates temp files with these permissions, so anything written
# through atomic_write_* inherits them via os.replace. This constant is for the
# files that predate those writers, or that a plain write_text() left readable.
PRIVATE_FILE_MODE = 0o600


def secure_file_mode(path: Path) -> bool:
    """Tighten an existing file to owner-only. Returns True if it changed."""
    try:
        current = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    if current == PRIVATE_FILE_MODE:
        return False
    try:
        os.chmod(path, PRIVATE_FILE_MODE)
    except OSError:
        return False
    return True


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


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Store an attachment atomically with private temporary-file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
