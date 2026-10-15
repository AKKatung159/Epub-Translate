"""Round-trip tests: cutting a book into segments and writing the same text back must not change it."""
import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.epub import Book, Marker, check_tags, parse_xml, restore  # noqa: E402

SAMPLE = b"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>One</title></head><body>
<h1 id="c1">Chapter&nbsp;One</h1>
<p class="x">He said <i>no</i>.<br/>Then he <a href="#n1" id="r1"><sup>1</sup></a> left&mdash;quietly.</p>
<p><span class="calibre3">Wrapped all the way</span></p>
<div><p>Nested <b>bold <i>italic</i></b> text</p></div>
<p><img src="pic.jpg" alt="x"/></p>
</body></html>"""


def test_marker_roundtrip():
    root = parse_xml(SAMPLE)
    p = root.findall(".//{*}p")[0]
    m = Marker()
    marked = m.mark(p)
    assert marked == "He said [1]no[/1].[2/]Then he [3][4]1[/4][/3] left—quietly."
    restore(p, marked, m.map)
    m2 = Marker()
    assert m2.mark(p) == marked
    assert p.get("class") == "x"


def test_restore_reordered_and_dropped_void():
    root = parse_xml(SAMPLE)
    p = root.findall(".//{*}p")[0]
    m = Marker()
    m.mark(p)
    restore(p, "[3][4]1[/4][/3] แล้วเขาก็จากไป [1]ไม่[/1]", m.map)   # [2/] (line break) dropped
    text = "".join(p.itertext())
    assert "ไม่" in text and "แล้วเขาก็จากไป" in text
    assert p.find(".//{*}a").get("id") == "r1"


def test_check_tags():
    src = "a [1]b[/1] [2]c [3]d[/3][/2] [4/]"
    assert check_tags(src, "[2]c [3]d[/3][/2] x [1]b[/1]") is None          # reordered, void dropped: ok
    assert check_tags(src, "[1]b[/1] c d") is not None                       # pairs missing
    assert check_tags(src, "[1]b [2]c[/1][/2] [3]d[/3]") is not None          # crossed
    assert check_tags(src, "[1]b[/1] [2]c [3/]d[/2]") is not None             # pair made self-closing


def _epub(doc: bytes, spine: bytes = b'<itemref idref="c"/>') -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", b'<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        z.writestr("OEBPS/content.opf", b'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title><dc:language>en</dc:language></metadata><manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/><item id="i" href="pic.jpg" media-type="image/jpeg"/></manifest><spine>' + spine + b'</spine></package>')
        z.writestr("OEBPS/c.xhtml", doc)
        z.writestr("OEBPS/pic.jpg", b"\xff\xd8binary")
    return buf.getvalue()


def test_book_identity_build():
    book = Book.load(_epub(SAMPLE))
    segs = book.segments()
    kinds = [s.kind for s in segs]
    assert kinds.count("title") == 1
    texts = [s.plain for s in segs if s.kind == "text"]
    assert texts == ["Chapter One","He said no.Then he 1 left—quietly.", "Wrapped all the way", "Nested bold italic text"]
    out = Book.load(book.build({(s.doc, s.path): s.marked for s in segs}))
    assert [s.plain for s in out.segments() if s.kind == "text"] == texts
    assert out.files["OEBPS/pic.jpg"] == b"\xff\xd8binary"
    assert b"<dc:language>th</dc:language>" in out.files["OEBPS/content.opf"]
    with zipfile.ZipFile(io.BytesIO(book.build({}))) as z:
        assert z.infolist()[0].filename == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED


def test_spine_listing_a_file_twice():
    book = Book.load(_epub(SAMPLE, b'<itemref idref="c"/><itemref idref="c"/>'))
    assert book.docs == ["OEBPS/c.xhtml"]
    paths = [(s.doc, s.path) for s in book.segments()]
    assert len(paths) == len(set(paths))


def test_failed_insert_does_not_poison_the_db(tmp_path):
    from app.store import Store
    st = Store(tmp_path / "t.db")
    st.many("INSERT INTO docs(job_id,doc,ord,title) VALUES(?,?,?,?)", [("j", "d", 0, "t")])
    try:
        st.many("INSERT INTO docs(job_id,doc,ord,title) VALUES(?,?,?,?)", [("j", "x", 0, "t"), ("j", "x", 1, "t")])
    except Exception:  # noqa: BLE001
        pass
    st.many("INSERT INTO docs(job_id,doc,ord,title) VALUES(?,?,?,?)", [("j", "y", 2, "t")])   # still works
    assert [r["doc"] for r in st.q("SELECT doc FROM docs ORDER BY ord")] == ["d", "y"]


def test_bilingual_keeps_ids_unique():
    book = Book.load(_epub(SAMPLE))
    segs = [s for s in book.segments() if s.kind == "text"]
    out = book.build({(s.doc, s.path): s.marked for s in segs}, bilingual=True)
    doc = zipfile.ZipFile(io.BytesIO(out)).read("OEBPS/c.xhtml")
    assert doc.count(b'id="c1"') == 1 and doc.count(b'id="r1"') == 1
    assert doc.count(b"epubtr-src") >= 4


def test_literal_brackets_survive():
    doc = SAMPLE.replace(b"Wrapped all the way", b"See note [12] and [a]")
    book = Book.load(_epub(doc))
    seg = [s for s in book.segments() if "note" in s.plain][0]
    assert "[12]" not in seg.marked                     # escaped, so not mistaken for a tag
    out = Book.load(book.build({(seg.doc, seg.path): seg.marked}))
    assert any("See note [12] and [a]" == s.plain for s in out.segments())
