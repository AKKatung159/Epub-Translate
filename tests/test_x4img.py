"""Pictures reworked for the X4: baseline, grey, fitted to 480x800, names and formats kept."""
import io
import sys
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.epub import Book  # noqa: E402
from app.x4img import MAX_H, MAX_W, for_x4  # noqa: E402


def _jpeg(size, mode="RGB", progressive=True) -> bytes:
    im = Image.new(mode, size, "red" if mode == "RGB" else 128)
    out = io.BytesIO()
    im.save(out, "JPEG", progressive=progressive, quality=90)
    return out.getvalue()


def _png(size, mode="RGBA") -> bytes:
    im = Image.new(mode, size, (0, 0, 0, 0) if mode == "RGBA" else 0)
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_progressive_colour_cover_becomes_baseline_grey_and_fits():
    out = for_x4("images/cover.jpg", _jpeg((1000, 1500)))
    im = _open(out)
    assert im.format == "JPEG"
    assert not (im.info.get("progressive") or im.info.get("progression"))
    assert im.mode == "L"
    assert im.width <= MAX_W and im.height <= MAX_H
    assert (im.width, im.height) == (480, 720)   # aspect kept


def test_small_grey_baseline_jpeg_is_left_alone():
    data = _jpeg((300, 400), mode="L", progressive=False)
    assert for_x4("a.jpeg", data) is data or for_x4("a.jpeg", data) == data


def test_small_progressive_jpeg_still_rewritten_to_baseline():
    out = for_x4("small.jpg", _jpeg((318, 474)))      # the size of epub.lan's DCC cover
    im = _open(out)
    assert not (im.info.get("progressive") or im.info.get("progression"))
    assert im.size == (318, 474)


def test_transparent_png_is_flattened_on_white():
    out = for_x4("orn.png", _png((900, 200)))
    im = _open(out)
    assert im.format == "PNG" and im.mode == "L"
    assert im.width == MAX_W
    assert im.getpixel((10, 10)) == 255             # transparent -> white, not black


def test_non_pictures_and_garbage_pass_through():
    assert for_x4("chapter.xhtml", b"<html/>") == b"<html/>"
    assert for_x4("broken.jpg", b"not a jpeg") == b"not a jpeg"


def _epub(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title><dc:language>en</dc:language>
<dc:identifier id="id">x</dc:identifier></metadata>
<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
<item id="img" href="images/cover.jpg" media-type="image/jpeg"/></manifest>
<spine><itemref idref="c"/></spine></package>""")
        z.writestr("OEBPS/c.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head><body><p>Hello.</p>
<p><img src="images/cover.jpg" alt=""/></p></body></html>""")
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_build_for_x4_reworks_pictures_only_when_asked():
    original = _jpeg((1000, 1500))
    book = Book.load(_epub({"OEBPS/images/cover.jpg": original}))
    plain = zipfile.ZipFile(io.BytesIO(book.build({})))
    assert plain.read("OEBPS/images/cover.jpg") == original
    x4 = zipfile.ZipFile(io.BytesIO(book.build({}, for_x4=True)))
    im = _open(x4.read("OEBPS/images/cover.jpg"))
    assert im.mode == "L" and im.width <= MAX_W
    assert x4.read("OEBPS/c.xhtml") == plain.read("OEBPS/c.xhtml")
