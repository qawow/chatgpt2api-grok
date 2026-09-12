"""Private immutable input blobs; task metadata only stores validated references."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from utils.atomic import atomic_write_bytes


class ImageTaskInputs:
    _NAME = re.compile(r"[0-9a-f]{64}-(?:images|mask)-[0-9]+-[0-9a-f]{64}\.bin")

    def __init__(self, directory: Path):
        self.directory = directory

    def _path(self, name: str) -> Path:
        if not self._NAME.fullmatch(name):
            raise ValueError("invalid image task input reference")
        path = self.directory / name
        if path.is_symlink():
            raise ValueError("image task input must not be a symlink")
        return path

    def encode(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        saved = dict(payload)
        prefix = hashlib.sha256(key.encode()).hexdigest()
        for field in ("images", "mask"):
            if field not in saved:
                continue
            entries = []
            for index, (data, filename, mime) in enumerate(saved[field]):
                digest = hashlib.sha256(data).hexdigest()
                name = f"{prefix}-{field}-{index}-{digest}.bin"
                path = self._path(name)
                if not path.exists():
                    atomic_write_bytes(path, data)
                entries.append({"blob": name, "filename": filename, "mime": mime})
            saved[field] = entries
        return saved

    def decode(self, payload: dict[str, Any]) -> dict[str, Any]:
        restored = dict(payload)
        for field in ("images", "mask"):
            if field not in restored:
                continue
            entries = []
            for entry in restored[field]:
                if isinstance(entry, dict):
                    path = self._path(str(entry.get("blob") or ""))
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != path.stem.rsplit("-", 1)[1]:
                        raise ValueError("image task input checksum mismatch")
                    entries.append((data, entry["filename"], entry["mime"]))
                else:
                    # Backward compatibility with inline base64 tasks.
                    data, filename, mime = entry
                    entries.append((base64.b64decode(data, validate=True), filename, mime))
            restored[field] = entries
        return restored

    def migrate(self, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        if any(not isinstance(entry, dict) for field in ("images", "mask") for entry in payload.get(field, [])):
            return self.encode(key, self.decode(payload))
        return payload

    def delete(self, payload: dict[str, Any], *, keep: set[str] | None = None) -> None:
        for field in ("images", "mask"):
            for entry in payload.get(field, []):
                if isinstance(entry, dict) and entry.get("blob") not in (keep or set()):
                    self._path(str(entry.get("blob") or "")).unlink(missing_ok=True)
