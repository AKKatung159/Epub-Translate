"""XTC / XTCH writer for the Xteink X4 (CrossPoint firmware): pre-rendered 480x800 pages.

Layout, from the firmware's own parser (lib/Xtc in crosspoint-reader, release 1.0.0 of the Thai fork):
  0x00  header, 56 bytes: magic "XTC\\0" (1-bit) or "XTCH" (2-bit), version 1.0, page count, flags,
        offsets of metadata / page table / page data, chapter offset (u64 at 0x30)
  0x38  title, 128 bytes, NUL-padded UTF-8;  0xB8 author, 64 bytes
  0xF8  chapters, 96 bytes each: name (80 bytes UTF-8), first page u16 @0x50, last page u16 @0x52 (1-based)
  ...   page table, 16 bytes per page: data offset u64, data size u32, width u16, height u16
  ...   pages: 22-byte XTG/XTH header + bitmap
XTG (1-bit): rows top to bottom, 8 px/byte, MSB = leftmost, 1 = white.
XTH (2-bit): two bit planes; each plane is column-major from the RIGHT edge, 8 vertical px/byte, MSB = top;
             value = bit1 << 1 | bit2 with 0 = white, 1 = dark grey, 2 = light grey, 3 = black.
"""
from __future__ import annotations

import json
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

W, H = 480, 800
XTC, XTCH, XTG, XTH = b"XTC\0", b"XTCH", b"XTG\0", b"XTH\0"
HEADER = 56
META = 0x38            # title 128 + author 64 -> chapters start at 0xF8
CHAPTER = 96

# grey level (0 = black .. 255 = white) -> XTH value
DARK_GREY, LIGHT_GREY = 1, 2


PART_CHAPTERS = 100     # chapter entries per file: every name is a small heap allocation on the device
NUM = re.compile(r"(บทที่|ตอนที่|ตอน|บท|chapter|ch\.?|part|เล่ม)\s*([0-9๐-๙]+)", re.I)
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")


def part_label(chapters: list["Chapter"]) -> str:
    """"บทที่ 1-93" from the chapter names in a part, or "" when they carry no numbers."""
    found = [(m.group(1), int(m.group(2).translate(THAI_DIGITS))) for c in chapters if (m := NUM.search(c.name))]
    if not found:
        return ""
    word = found[0][0]
    nums = [n for _, n in found]
    lo, hi = min(nums), max(nums)
    return f"{word} {lo}" if lo == hi else f"{word} {lo}-{hi}"


@dataclass
class Chapter:
    name: str
    first: int        # 0-based page indexes, inclusive
    last: int


def levels4(gray: np.ndarray) -> np.ndarray:
    """0..255 grey -> XTH values (0 white, 2 light, 1 dark, 3 black), thresholds tuned for text."""
    out = np.zeros(gray.shape, np.uint8)
    out[gray < 215] = LIGHT_GREY
    out[gray < 150] = DARK_GREY
    out[gray < 80] = 3
    return out


# 8x8 Bayer matrix: an ordered dither keeps tiles independent, so dithered pictures sit next to sharp text
_BAYER = np.array([[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
                   [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
                   [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
                   [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]], np.float32)
_BAYER_T = np.tile((_BAYER + 0.5) * 4, (H // 8, W // 8))          # thresholds 2..254 over the page
TILE = 16
TEXT_THRESHOLD = 170        # not 128: anti-aliased strokes of small text stay whole instead of breaking up


def picture_tiles(gray: np.ndarray) -> np.ndarray:
    """Per 16x16 tile: True inside a picture (photo, illustration, cover).

    Dense tiles (>= 60 % not white) form regions (4-connected). A region is a picture when at least a third of its tiles are
    mostly mid-grey; then all of it is dithered, its near-black and near-white parts too (deciding tile by tile
    left solid blobs there). Text is black on white with thin anti-aliased edges: its tiles are never mostly
    mid-grey, so a paragraph stays sharp even right next to a picture."""
    h, w = gray.shape
    t = gray.reshape(h // TILE, TILE, w // TILE, TILE)
    mid = (((t > 40) & (t < 215)).mean(axis=(1, 3))) >= 0.5
    inked = (t < 215).mean(axis=(1, 3)) >= 0.6       # dense: text tiles are mostly white paper, pictures are not
    rows, cols = inked.shape
    out = np.zeros_like(inked)
    seen = np.zeros_like(inked)
    for r0 in range(rows):
        for c0 in range(cols):
            if not inked[r0, c0] or seen[r0, c0]:
                continue
            region, stack = [], [(r0, c0)]
            seen[r0, c0] = True
            while stack:
                r, c = stack.pop()
                region.append((r, c))
                for rr, cc in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if 0 <= rr < rows and 0 <= cc < cols and inked[rr, cc] and not seen[rr, cc]:
                        seen[rr, cc] = True
                        stack.append((rr, cc))
            if sum(mid[r, c] for r, c in region) >= max(2, len(region) / 3):
                for r, c in region:
                    out[r, c] = True
    # pictures fade out into light areas; grow into neighbouring tiles without dark strokes (so never into text)
    light = t.min(axis=(1, 3)) > 120
    for _ in range(6):
        grow = np.zeros_like(out)
        grow[1:] |= out[:-1]
        grow[:-1] |= out[1:]
        grow[:, 1:] |= out[:, :-1]
        grow[:, :-1] |= out[:, 1:]
        grow &= light & ~out
        if not grow.any():
            break
        out |= grow
    return out


def to_bw(gray: np.ndarray) -> np.ndarray:
    """1-bit page (True = white): text thresholded sharp, pictures dithered so they read as grey."""
    photo = np.kron(picture_tiles(gray), np.ones((TILE, TILE), bool))
    return gray >= np.where(photo, _BAYER_T, TEXT_THRESHOLD)


def encode_page(gray: np.ndarray, depth: int) -> bytes:
    """gray: uint8 array (800, 480), 0 = black. Returns XTG/XTH page header + bitmap."""
    assert gray.shape == (H, W), gray.shape
    if depth == 1:
        bitmap = np.packbits(to_bw(gray), axis=1).tobytes()          # 1 = white
        magic = XTG
    else:
        v = levels4(gray)
        cols = v[:, ::-1].T                                           # column 0 = rightmost, top first
        b1 = np.packbits((cols >> 1) & 1, axis=1).tobytes()
        b2 = np.packbits(cols & 1, axis=1).tobytes()
        bitmap = b1 + b2
        magic = XTH
    return struct.pack("<4sHHBBIQ", magic, W, H, 0, 0, len(bitmap), 0) + bitmap


def decode_page(data: bytes, depth: int) -> np.ndarray:
    """Inverse of encode_page, the way the firmware reads it (for tests and previews): 0..255 grey."""
    magic, w, h, _, _, size, _ = struct.unpack_from("<4sHHBBIQ", data)
    bm = np.frombuffer(data, np.uint8, size, 22)
    if depth == 1:
        bits = np.unpackbits(bm).reshape(h, (w + 7) // 8 * 8)[:, :w]
        return np.where(bits == 1, 255, 0).astype(np.uint8)
    plane = (w * h + 7) // 8
    col_bytes = (h + 7) // 8
    b1 = np.unpackbits(bm[:plane]).reshape(w, col_bytes * 8)[:, :h]
    b2 = np.unpackbits(bm[plane:]).reshape(w, col_bytes * 8)[:, :h]
    v = ((b1 << 1) | b2).T[:, ::-1]
    return np.array([255, 85, 170, 0], np.uint8)[v]


def _fixed(text: str, size: int) -> bytes:
    raw = text.encode("utf-8")[: size - 1]
    raw = raw.decode("utf-8", "ignore").encode("utf-8")                # never cut a character in half
    return raw.ljust(size, b"\0")


class Writer:
    """Collect pages one by one (kept on disk), then write the finished file.

    cover(label) -> page, when set, makes the first page of every written file (the X4 shows a file's first page
    as its cover); label is "" for a single file, "02/16 · บทที่ 94-187" for a part. Chapters and page counts given
    to save / save_parts count the added pages only: each moves one page down in the file."""

    def __init__(self, depth: int = 2, cover: Callable[[str], np.ndarray] | None = None):
        self.depth = depth
        self.cover = cover
        self.tmp = tempfile.TemporaryFile()
        self.sizes: list[int] = []

    def add(self, gray: np.ndarray) -> None:
        data = encode_page(gray, self.depth)
        self.tmp.write(data)
        self.sizes.append(len(data))

    def __len__(self) -> int:
        return len(self.sizes)

    def save(self, path: Path, title: str = "", author: str = "", chapters: list[Chapter] = ()) -> None:
        """Everything in one file (see save_parts for long books)."""
        self._write(path, 0, len(self.sizes), title, author, chapters)
        self.tmp.close()

    def save_parts(self, folder: Path, stem: str, title: str = "", author: str = "",
                   chapters: list[Chapter] = (), per_part: int = 600,
                   page_label=None) -> list[Path]:
        """Long books as several files of at most per_part pages: "<stem>.xtc" or "<stem> 01.xtc", "<stem> 02.xtc"...

        The firmware reads the whole page table into RAM (16 bytes a page) next to the page buffer: a 9,372-page
        file wants ~150 KB of a ~380 KB heap. Each part keeps the chapters that fall inside it (at most
        PART_CHAPTERS, evenly spread) and gets a label from them ("บทที่ 1-93"), or from page_label(start, end)
        ("หน้า 1-245"). Labels go to "<stem>.parts.json" next to the files."""
        n = len(self.sizes)
        if not n:
            raise ValueError("no pages")
        parts = -(-n // per_part)
        ext = ".xtch" if self.depth == 2 else ".xtc"
        out, index = [], []
        for k in range(parts):
            s, e = k * per_part, min(n, (k + 1) * per_part)
            mine = [Chapter(c.name, max(c.first, s) - s, min(c.last, e - 1) - s)
                    for c in chapters if c.first < e and c.last >= s]
            if parts > 1 and (not mine or mine[0].first > 0):
                lead = next((c for c in reversed(chapters) if c.first <= s), None)   # chapter running into this part
                mine.insert(0, Chapter(lead.name if lead else title, 0, (mine[0].first - 1) if mine else e - s - 1))
            label = part_label(mine) or (page_label(s, e) if page_label else "")
            if len(mine) > PART_CHAPTERS:                   # keep the first, then an even spread
                step = len(mine) / PART_CHAPTERS
                kept = [mine[int(i * step)] for i in range(PART_CHAPTERS)]
                mine = [Chapter(c.name, c.first, (kept[i + 1].first - 1) if i + 1 < len(kept) else e - s - 1)
                        for i, c in enumerate(kept)]
            name = f"{stem}{ext}" if parts == 1 else f"{stem} {k + 1:02d}{ext}"
            shown = title if parts == 1 else f"{k + 1:02d}/{parts} {label + ' · ' if label else ''}{title}"
            on_cover = "" if parts == 1 else f"{k + 1:02d}/{parts}{' · ' + label if label else ''}"
            pages = self._write(folder / name, s, e, shown, author, mine, on_cover)
            out.append(folder / name)
            index.append({"file": name, "label": label, "pages": pages})
        (folder / f"{stem}.parts.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        self.tmp.close()
        return out

    def _write(self, path: Path, start: int, end: int, title: str, author: str, chapters, label: str = "") -> int:
        """Pages start..end-1 (after a cover page, if any) as one file; returns its page count."""
        sizes = self.sizes[start:end]
        n = len(sizes)
        if not n:
            raise ValueError("no pages")
        chapters = [c for c in chapters if 0 <= c.first <= c.last < n][:500]
        cover = encode_page(self.cover(label), self.depth) if self.cover else b""
        if cover:
            chapters = [Chapter(c.name, c.first + 1, c.last + 1) for c in chapters]
            sizes = [len(cover), *sizes]
            n += 1
        chap_off = META + 128 + 64 if chapters else 0
        table_off = META + 128 + 64 + CHAPTER * len(chapters)
        data_off = table_off + 16 * n
        header = struct.pack("<4sBBHBBBBIQQQQQ", XTCH if self.depth == 2 else XTC, 1, 0, n, 0, 1, 0,
                             1 if chapters else 0, 0, META, table_off, data_off, 0, chap_off)
        assert len(header) == HEADER
        with open(path, "wb") as f:
            f.write(header + _fixed(title, 128) + _fixed(author, 64))
            for c in chapters:
                f.write(_fixed(c.name, 80) + struct.pack("<HH", c.first + 1, c.last + 1) + bytes(12))
            off = data_off
            for size in sizes:
                f.write(struct.pack("<QIHH", off, size, W, H))
                off += size
            f.write(cover)
            self.tmp.seek(sum(self.sizes[:start]))
            left = sum(self.sizes[start:end])
            while left:
                chunk = self.tmp.read(min(left, 1 << 20))
                f.write(chunk)
                left -= len(chunk)
        return n


def read_info(path: Path) -> dict:
    """Header, title and chapters back from a file (tests, sanity checks)."""
    raw = Path(path).read_bytes()
    magic, vmaj, vmin, n, _rd, has_meta, _th, has_ch, _cur, meta, table, data, _thumb, chap = \
        struct.unpack_from("<4sBBHBBBBIQQQQQ", raw)
    chapters = []
    if has_ch and chap:
        for i in range((table - chap) // CHAPTER):
            rec = raw[chap + i * CHAPTER: chap + (i + 1) * CHAPTER]
            first, last = struct.unpack_from("<HH", rec, 0x50)
            chapters.append(Chapter(rec[:80].rstrip(b"\0").decode("utf-8"), first - 1, last - 1))
    pages = [struct.unpack_from("<QIHH", raw, table + 16 * i) for i in range(n)]
    return {"magic": magic, "version": (vmaj, vmin), "pages": pages, "chapters": chapters,
            "title": raw[META:META + 128].rstrip(b"\0").decode("utf-8") if has_meta else "",
            "author": raw[META + 128:META + 192].rstrip(b"\0").decode("utf-8") if has_meta else "",
            "raw": raw}
