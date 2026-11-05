"""Side-effect-free media detection for original image bytes.

Only signatures are inspected: no decoding, transcoding, filesystem access or
service imports. Unknown payloads retain the historical PNG fallback.
"""
from __future__ import annotations


def sniff_image_media(payload: bytes) -> tuple[str, str]:
    """Return ``(MIME type, extension)`` from at most the first twelve bytes."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(payload) >= 12 and payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return "image/png", ".png"
