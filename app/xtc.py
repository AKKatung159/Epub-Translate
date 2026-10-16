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

import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

W, H = 480, 800
XTC, XTCH, XTG, XTH = b"XTC\0", b"XTCH", b"XTG\0", b"XTH\0"
HEADER = 56
META = 0x38            # title 128 + author 64 -> chapters start at 0xF8
CHAPTER = 96

# grey level (0 = black .. 255 = white) -> XTH value
DARK_GREY, LIGHT_GREY = 1, 2


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


def encode_page(gray: np.ndarray, depth: int) -> bytes:
    """gray: uint8 array (800, 480), 0 = black. Returns XTG/XTH page header + bitmap."""
    assert gray.shape == (H, W), gray.shape
    if depth == 1:
        bitmap = np.packbits(gray >= 128, axis=1).tobytes()           # 1 = white
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
    """Collect pages one by one (kept on disk), then write the finished file."""

    def __init__(self, depth: int = 2):
        self.depth = depth
        self.tmp = tempfile.TemporaryFile()
        self.sizes: list[int] = []

    def add(self, gray: np.ndarray) -> None:
        data = encode_page(gray, self.depth)
        self.tmp.write(data)
        self.sizes.append(len(data))

    def __len__(self) -> int:
        return len(self.sizes)

    def save(self, path: Path, title: str = "", author: str = "", chapters: list[Chapter] = ()) -> None:
        n = len(self.sizes)
        if not n:
            raise ValueError("no pages")
        chapters = [c for c in chapters if 0 <= c.first <= c.last < n][:500]
        chap_off = META + 128 + 64 if chapters else 0
        table_off = META + 128 + 64 + CHAPTER * len(chapters)
        data_off = table_off + 16 * n
        header = struct.pack("<4sBBHBBBBIQQQQQ", XTCH if self.depth == 2 else XTC, 1, 0, n, 0, 1, 0,
                             1 if chapters else 0, 0, META, table_off, data_off, 0, chap_off)
        assert len(header) == HEADER
        with open(path, "wb") as f:
            f.write(header + _fixed(title, 128) + _fixed(author, 64))
            for c in chapters:
                f.write(_fixed(c.name, 80) + struct.pack("<HH", c.first + 1, c.last + 1) + b"\0" * 12)
            off = data_off
            for size in self.sizes:
                f.write(struct.pack("<QIHH", off, size, W, H))
                off += size
            self.tmp.seek(0)
            shutil.copyfileobj(self.tmp, f, 1 << 20)
        self.tmp.close()


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
