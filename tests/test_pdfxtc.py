"""PDF -> XTC for the Xteink X4: file format as the firmware reads it, cutting between lines, headers dropped."""
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pdfslice import Options, body_rows, convert, cut_points  # noqa: E402
from app.xtc import H, W, Chapter, Writer, decode_page, encode_page, read_info  # noqa: E402


def test_pages_decode_the_way_the_firmware_reads_them():
    g = np.full((H, W), 255, np.uint8)
    g[10:20, 0:5] = 0                       # black, top-left
    g[100, 479] = 0                         # black, right edge (XTH columns start from the right)
    g[700:710, 200:300] = 120               # dark grey
    g[400:410, 50:60] = 190                 # light grey
    two = decode_page(encode_page(g, 2), 2)
    assert two[15, 2] == 0 and two[100, 479] == 0 and two[0, 0] == 255
    assert two[705, 250] == 85 and two[405, 55] == 170
    one = decode_page(encode_page(g, 1), 1)
    assert one[15, 2] == 0 and one[405, 55] == 255
    data = encode_page(g, 2)
    magic, w, h, _, _, size, _ = struct.unpack_from("<4sHHBBIQ", data)
    assert (magic, w, h, size) == (b"XTH\0", 480, 800, 480 * 800 // 8 * 2)


def test_container_header_metadata_and_chapters(tmp_path):
    wr = Writer(2)
    for v in (0, 128, 255):
        wr.add(np.full((H, W), v, np.uint8))
    out = tmp_path / "b.xtch"
    wr.save(out, "อลิซในแดนมหัศจรรย์ " * 20, "Lewis Carroll", [Chapter("บทที่ 1", 0, 1), Chapter("Two", 2, 2)])
    info = read_info(out)
    raw = info["raw"]
    assert info["magic"] == b"XTCH" and info["version"] == (1, 0) and len(info["pages"]) == 3
    assert raw[0x0B] == 1 and struct.unpack_from("<Q", raw, 0x30)[0] == 0xF8      # firmware reads a u64 there
    assert info["title"].startswith("อลิซ") and len(info["title"].encode()) <= 127   # cut on a character
    assert [(c.name, c.first, c.last) for c in info["chapters"]] == [("บทที่ 1", 0, 1), ("Two", 2, 2)]
    off, size, w, h = info["pages"][1]
    assert (w, h) == (480, 800) and decode_page(raw[off:off + size], 2)[0, 0] == 85


def lines_image(n_lines: int, header: bool = True, height: int = 2000) -> np.ndarray:
    img = Image.new("L", (800, height), 255)
    d = ImageDraw.Draw(img)
    if header:
        d.rectangle((300, 30, 500, 40), fill=0)                     # running title
        d.rectangle((390, height - 50, 410, height - 40), fill=0)   # page number
    y = 140
    for _ in range(n_lines):
        d.rectangle((60, y, 740, y + 22), fill=0)                   # a line of text
        y += 36                                                     # 14 px gap between lines
    return np.asarray(img)


def test_strips_are_cut_between_lines_never_through_them():
    a = lines_image(40, header=False)
    a = a[130:140 + 40 * 36]
    strips = cut_points(a, 480)
    assert all(b - t <= 480 for t, b in strips)
    ink_rows = (a < 200).any(axis=1)
    for _, b in strips[:-1]:
        assert not ink_rows[b - 1] or not ink_rows[b]               # the cut row is white on one side


def test_running_header_and_page_number_are_dropped():
    a = lines_image(30)
    top, bottom = body_rows(a)
    assert top == 140 and bottom < a.shape[0] - 60


def test_pdf_to_xtc_end_to_end(tmp_path):
    pages = [Image.fromarray(lines_image(45)).convert("RGB") for _ in range(3)]
    pdf = tmp_path / "doc.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=150)
    out = tmp_path / "doc.xtch"
    seen = []
    n = convert(pdf, out, Options(mode="auto"), progress=lambda i, total, dev: seen.append((i, total)))
    info = read_info(out)
    assert n == len(info["pages"]) and seen[-1] == (3, 3)
    # each page body is ~1,870 px tall once 800 px wide: 3 pages flow into ~12 full strips of <= 480 px
    assert 12 <= n <= 14
    first = decode_page(info["raw"][info["pages"][0][0]:][:info["pages"][0][1]], 2)
    assert first.shape == (800, 480) and (first < 128).any()
    whole = tmp_path / "page.xtc"
    assert convert(pdf, whole, Options(mode="page", depth=1)) == 3
    assert read_info(whole)["magic"] == b"XTC\0"
