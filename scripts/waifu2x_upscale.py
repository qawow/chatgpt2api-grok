#!/usr/bin/env python3
"""Upscale an image through the unofficial www.waifu2x.net client.

Example:
  python scripts/waifu2x_upscale.py input.png -o out.png --style art --scale 2x
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.waifu2x_backend import upscale  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Unofficial waifu2x.net upscaler")
    parser.add_argument("input", help="input image path or http(s) URL")
    parser.add_argument("-o", "--output", help="output path (default: <stem>_waifu2x.png)")
    parser.add_argument("--style", default="art", help="art | art_scan | photo")
    parser.add_argument("--noise", default="medium", help="none | low | medium | high | highest")
    parser.add_argument("--scale", default="2x", help="none | 1x | 1.6x | 2x")
    parser.add_argument("--format", default="png", choices=["png", "webp"])
    parser.add_argument("--turnstile", default="", help="one-shot Turnstile token from the website")
    args = parser.parse_args()

    source = str(args.input).strip()
    image = b""
    url = ""
    filename = "image.png"
    if source.startswith(("http://", "https://")):
        url = source
    else:
        path = Path(source)
        image = path.read_bytes()
        filename = path.name

    result = upscale(
        image=image or None,
        filename=filename,
        url=url,
        style=args.style,
        noise=args.noise,
        scale=args.scale,
        image_format=args.format,
        turnstile=args.turnstile,
    )
    output = Path(args.output) if args.output else Path(f"{Path(filename).stem}_{result.filename}")
    output.write_bytes(result.content)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
