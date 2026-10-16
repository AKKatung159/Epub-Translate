"""Pictures inside EPUBs, reworked for the Xteink X4 (CrossPoint firmware, 480x800 grey e-ink).

The firmware's JPEG decoder (JPEGDEC on an ESP32-C3 without PSRAM) handles a progressive JPEG only by
reading its DC coefficients, i.e. at 1/8 resolution: a 500x750 cover shows as a 62x94 smear scaled up.
Most store EPUBs ship progressive, colour images several times larger than the screen, which also costs
seconds of decoding on every cover thumbnail and illustration.

for_x4() rewrites each picture once, keeping its file name and format (no href changes anywhere):
  JPEG -> baseline (non-progressive), greyscale, fitted inside 480x800, quality 85
  PNG  -> greyscale (alpha flattened on white), fitted inside 480x800
Pictures that are already small, grey and baseline are left byte-for-byte alone. Anything Pillow cannot
read is passed through unchanged.
"""
from __future__ import annotations

import hashlib
import io
from collections import OrderedDict

from PIL import Image, ImageOps

MAX_W, MAX_H = 480, 800
JPEG_QUALITY = 85
_VERSION = b"x4img-1"          # bump when the output changes, so cached results are rebuilt

_cache: OrderedDict[bytes, bytes] = OrderedDict()
_CACHE_BYTES = 64 * 1024 * 1024
_cache_size = 0


def is_picture(name: str) -> bool:
    return name.lower().endswith((".jpg", ".jpeg", ".png"))


def _fit(im: Image.Image) -> Image.Image:
    if im.width <= MAX_W and im.height <= MAX_H:
        return im
    scale = min(MAX_W / im.width, MAX_H / im.height)
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def _grey(im: Image.Image) -> Image.Image:
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    return im.convert("L")


def _rework(name: str, data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as src:
        fmt = src.format
        progressive = bool(src.info.get("progressive") or src.info.get("progression"))
        small = src.width <= MAX_W and src.height <= MAX_H
        if fmt == "JPEG":
            if src.mode == "L" and small and not progressive:
                return data
            im = _fit(_grey(src))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False)
            return out.getvalue()
        if fmt == "PNG":
            if src.mode in ("L", "1") and small:
                return data
            im = _fit(_grey(src))
            out = io.BytesIO()
            im.save(out, "PNG", optimize=True)
            new = out.getvalue()
            return new if len(new) < len(data) or not small or src.mode != "L" else data
    return data


def for_x4(name: str, data: bytes) -> bytes:
    """The picture `data` (file `name`) reworked for the X4; unchanged if not a JPEG/PNG or unreadable."""
    global _cache_size
    if not is_picture(name):
        return data
    key = hashlib.sha1(_VERSION + name.lower().rsplit(".", 1)[-1].encode() + data).digest()
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
        return hit
    try:
        out = _rework(name, data)
    except Exception:
        out = data
    _cache[key] = out
    _cache_size += len(out)
    while _cache_size > _CACHE_BYTES and _cache:
        _, old = _cache.popitem(last=False)
        _cache_size -= len(old)
    return out
