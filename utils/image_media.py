"""Side-effect-free media detection for original image bytes.

Only signatures are inspected: no decoding, transcoding, filesystem access or
service imports. Unknown payloads retain the historical PNG fallback.
"""
from __future__ import annotations

# Mirrors the web client's own detector (bundle 337882): it decodes the first
# 512 bytes, trims leading whitespace, lowercases, and treats the payload as
# SVG when it starts with ``<svg`` or is an XML declaration that contains
# ``<svg``.  Using the same rule keeps us from disagreeing with the client
# about a file it already accepted.
_SVG_PROBE_BYTES = 512
_SVG_MAX_DECLARATION_PREFIX = 200


def _looks_like_svg(payload: bytes) -> bool:
    """True when the head of ``payload`` matches the client's SVG rule."""
    head = payload[:_SVG_PROBE_BYTES].decode("utf-8", errors="ignore").lstrip().lower()
    if not head:
        return False
    if head.startswith("<svg"):
        return True
    # An XML declaration (``<?xml ... ?>``) may precede the root element, but
    # arbitrary XML (RSS, SOAP, XHTML) must not be labelled as an SVG image.
    if not head.startswith("<?xml"):
        return False
    prefix = head[:_SVG_MAX_DECLARATION_PREFIX]
    closing = prefix.find("?>")
    if closing < 0:
        return False
    return "<svg" in prefix[closing + 2:]


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
    if _looks_like_svg(payload):
        return "image/svg+xml", ".svg"
    return "image/png", ".png"
