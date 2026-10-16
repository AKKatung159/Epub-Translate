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
    n = convert(pdf, out, Options(mode="auto", depth=2), progress=lambda i, total, dev: seen.append((i, total)))
    info = read_info(out)
    assert n == len(info["pages"]) and seen[-1] == (3, 3)
    # each page body is ~1,870 px tall once 800 px wide: 3 pages flow into ~12 full strips of <= 480 px
    assert 12 <= n <= 14
    first = decode_page(info["raw"][info["pages"][0][0]:][:info["pages"][0][1]], 2)
    assert first.shape == (800, 480) and (first < 128).any()
    whole = tmp_path / "page.xtc"
    assert convert(pdf, whole, Options(mode="page", depth=1)) == 3
    assert read_info(whole)["magic"] == b"XTC\0"


def test_default_is_black_white_and_chapters_stay_few(tmp_path):
    """The X4 needs one malloc of a whole page: 48 KB (XTC) is far safer than 96 KB (XTCH); many chapter names
    fragment its heap. Defaults: XTC, at most 64 chapters (even without bookmarks on a long PDF)."""
    from app.pdfslice import MAX_CHAPTERS
    assert Options().depth == 1
    page = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(page).rectangle((20, 20, 180, 40), fill="black")
    pdf = tmp_path / "long.pdf"
    page.save(pdf, save_all=True, append_images=[page] * 699, resolution=72)
    assert convert(pdf, tmp_path / "long.xtc", Options(mode="page")) == 700
    # longer than one part (600): two files, each with its own page table and the chapters inside it
    a, b = read_info(tmp_path / "long 01.xtc"), read_info(tmp_path / "long 02.xtc")
    assert not (tmp_path / "long.xtc").exists()
    assert a["magic"] == b"XTC\0" and len(a["pages"]) == 600 and len(b["pages"]) == 100
    # part number first (file lists cut long names), then what is inside: PDF pages when there are no chapters
    assert a["title"] == "01/2 หน้า 1-600 · long" and b["title"].startswith("02/2 หน้า 601-700")
    import json
    index = json.loads((tmp_path / "long.parts.json").read_text(encoding="utf-8"))
    assert [(x["file"], x["label"], x["pages"]) for x in index] == [("long 01.xtc", "หน้า 1-600", 600),
                                                                   ("long 02.xtc", "หน้า 601-700", 100)]
    assert 1 < len(a["chapters"]) <= MAX_CHAPTERS and b["chapters"] and b["chapters"][0].first == 0
    assert all(c.last < 100 for c in b["chapters"])


def test_pictures_are_dithered_text_stays_sharp_and_a_cover_page_stays_whole(tmp_path):
    from app.xtc import picture_tiles
    yy, xx = np.mgrid[0:H, 0:W]
    photo = ((np.sin(xx / 30) + np.cos(yy / 45)) * 70 + 128).clip(0, 255).astype(np.uint8)
    text = np.asarray(Image.fromarray(lines_image(30, header=False)).resize((W, H)))
    assert picture_tiles(text).mean() == 0                         # text never dithered
    assert picture_tiles(photo).mean() > 0.9
    page = decode_page(encode_page(photo, 1), 1)
    grey = photo[200:216, 200:216].mean()
    # dithering: a 16x16 patch keeps about the same share of black as its grey level asks for
    assert abs((page[200:216, 200:216] == 255).mean() * 255 - grey) < 40
    cover = Image.fromarray(photo).convert("RGB").resize((600, 900))
    body = Image.fromarray(lines_image(45)).convert("RGB")
    pdf = tmp_path / "book.pdf"
    cover.save(pdf, save_all=True, append_images=[body, body], resolution=150)
    out = tmp_path / "book.xtc"
    convert(pdf, out, Options())
    info = read_info(out)
    first = decode_page(info["raw"][info["pages"][0][0]:][:info["pages"][0][1]], 1)
    assert picture_tiles(np.where(first > 0, 170, 60).astype(np.uint8)).mean() > 0.2   # page 1 = the cover, upright


def test_chapters_found_in_page_text_name_the_parts(tmp_path):
    """No bookmarks: "บทที่ N" at the top of a page becomes a chapter, and parts are labelled by them."""
    import pytest
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    from app.pdfslice import text_marks
    import pypdfium2 as pdfium
    pdf = tmp_path / "novel.pdf"
    c = canvas.Canvas(str(pdf), pagesize=(300, 400))
    for n in range(1, 13):
        c.drawString(30, 360, f"Chapter {n} The Road")
        for y in range(330, 60, -18):
            c.drawString(30, y, "Some text of the story goes here, line after line.")
        c.showPage()
    c.save()
    doc = pdfium.PdfDocument(str(pdf))
    marks = text_marks(doc)
    doc.close()
    assert [p for _, p in marks] == list(range(12)) and marks[3][0].startswith("Chapter 4")
    convert(pdf, tmp_path / "novel.xtc", Options(per_part=10))
    import json
    labels = [x["label"] for x in json.loads((tmp_path / "novel.parts.json").read_text(encoding="utf-8"))]
    assert labels[0].startswith("Chapter 1-") and all(lbl.startswith("Chapter") for lbl in labels)
