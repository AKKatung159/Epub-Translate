"""Cover page for XTC / XTCH files: the X4 home screen and file list show the FIRST page of the file as its
cover (CrossPoint lib/Xtc generateCoverBmp / generateThumbBmp), so every file starts with one.

- art (a cover picture) is fitted whole into 480x800 with white margins, then dithered once here:
  Floyd-Steinberg to black/white for XTC, to 4 greys for XTCH (the page encoder keeps those values as they are);
- no art: a plain title page, the title in a Thai font;
- parts of a long book get their label ("02/16 · บทที่ 94-187") under the picture, so the parts tell apart.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, features

from .xtc import H, W

log = logging.getLogger("xtccover")

FONTS = [  # first that exists; Docker image: fonts-tlwg-laksaman-ttf
    "/usr/share/fonts/truetype/tlwg/Laksaman.ttf",
    "/usr/share/fonts/opentype/tlwg/Laksaman.otf",
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "C:/Windows/Fonts/LeelawUI.ttf",
    "C:/Windows/Fonts/tahoma.ttf",
]
MARGIN = 16
GREYS4 = (0, 85, 170, 255)        # the 4 levels of an XTH page (encode_page maps them to its own values)


@lru_cache(maxsize=16)
def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    engine = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC   # RAQM: Thai marks placed right
    for p in FONTS:
        if Path(p).exists():
            return ImageFont.truetype(p, size, layout_engine=engine)
    log.warning("no Thai font found, cover text may show boxes")
    return ImageFont.load_default(size)


def dither(img: Image.Image, depth: int) -> Image.Image:
    """Greyscale picture -> only the levels the page can show, error diffused (Floyd-Steinberg)."""
    img = img.convert("L")
    if depth == 1:
        return img.convert("1").convert("L")                   # PIL's "1" conversion is Floyd-Steinberg
    pal = Image.new("P", (1, 1))
    pal.putpalette([g for v in GREYS4 for g in (v, v, v)] + [0] * 3 * 252)
    q = img.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG)
    return Image.fromarray(np.array(GREYS4, np.uint8)[np.minimum(np.asarray(q), 3)])


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    s = min(w / img.width, h / img.height)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


# a line may break before any character except these (Thai vowels / tone marks above and below, sara am)
_JOINS = set(range(0x0E31, 0x0E32)) | set(range(0x0E33, 0x0E3B)) | set(range(0x0E47, 0x0E4F))


def wrap(text: str, f, width: int, max_lines: int) -> list[str]:
    """Lines no wider than width: at spaces, and inside a long word (Thai has no spaces) between characters."""
    def fits(s: str) -> bool:
        return f.getlength(s) <= width

    lines, cur = [], ""
    for word in text.split():
        cand = f"{cur} {word}".strip()
        if fits(cand):
            cur = cand
            continue
        if cur:
            lines.append(cur)
        cur = ""
        for i, ch in enumerate(word):
            if cur and ord(ch) not in _JOINS and not 0x0E40 <= ord(cur[-1]) <= 0x0E44 and not fits(cur + ch + "".join(
                    c for c in word[i + 1:i + 3] if ord(c) in _JOINS)):
                lines.append(cur)
                cur = ""
            cur += ch
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and not fits(lines[-1] + "…"):
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines


def _text_block(d: ImageDraw.ImageDraw, lines: list[str], f, top: int, gap: int) -> int:
    """Lines centred one under the other from top; returns the y below the last."""
    asc, desc = f.getmetrics()
    y = top
    for line in lines:
        d.text((W // 2, y), line, font=f, fill=0, anchor="ma")
        y += asc + desc + gap
    return y


def title_page(title: str, author: str = "") -> Image.Image:
    """No cover picture: the title large in the upper middle, a rule, the author under it."""
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    d.rectangle((MARGIN, MARGIN, W - MARGIN - 1, H - MARGIN - 1), outline=0, width=3)
    d.rectangle((MARGIN + 8, MARGIN + 8, W - MARGIN - 9, H - MARGIN - 9), outline=0, width=1)
    inner = W - 2 * (MARGIN + 40)
    title = title or "ไม่มีชื่อ"
    for size in (56, 48, 40, 34, 28):                          # largest size where no word has to be cut
        f = font(size)
        lines = wrap(title, f, inner, 6)
        if len(lines) <= 4 and all(f.getlength(w) <= inner for w in title.split()):
            break
    asc, desc = f.getmetrics()
    y = max(150, 330 - len(lines) * (asc + desc + 8) // 2)
    y = _text_block(d, lines, f, y, 8) + 24
    d.line((W // 2 - 60, y, W // 2 + 60, y), fill=0, width=2)
    if author:
        _text_block(d, wrap(author, font(28), inner, 2), font(28), y + 28, 4)
    return img


def cover_page(art: Image.Image | None, title: str, author: str, depth: int, label: str = "") -> np.ndarray:
    """The first page of a file: uint8 (800, 480), already reduced to the levels of depth (1 or 2)."""
    band = 0
    if label:
        f = font(26)
        lines = wrap(label, f, W - 2 * MARGIN, 2)
        asc, desc = f.getmetrics()
        band = len(lines) * (asc + desc + 4) + 2 * MARGIN
    if art is not None:
        canvas = Image.new("L", (W, H), 255)
        pic = _fit(art.convert("L"), W - 2 * MARGIN, H - 2 * MARGIN - band)
        canvas.paste(dither(pic, depth), ((W - pic.width) // 2, MARGIN + (H - 2 * MARGIN - band - pic.height) // 2))
    else:
        canvas = title_page(title, author)
    if label:
        d = ImageDraw.Draw(canvas)
        if art is not None:
            top = H - band
            d.line((MARGIN, top + 4, W - MARGIN - 1, top + 4), fill=0, width=2)
            _text_block(d, lines, f, top + MARGIN, 4)
        else:                                                  # inside the frame of the title page
            _text_block(d, lines, f, H - band - MARGIN - 8, 4)
    return np.asarray(canvas, np.uint8)
