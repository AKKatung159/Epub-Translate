"""Library of ready-made EPUBs: upload, OPDS sections, pictures reworked for the X4, send to X4, rename, delete."""
import dataclasses
import io
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NS = {"a": "http://www.w3.org/2005/Atom"}
ACQ = "http://opds-spec.org/acquisition"


def _jpeg(size=(1000, 1500), progressive=True) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "red").save(out, "JPEG", progressive=progressive, quality=90)
    return out.getvalue()


def _epub(title="หมาปิศาจแห่งบาสเกอร์วิลส์", author="อาเธอร์ โคนัน ดอยล์", epub3=True) -> bytes:
    cover_meta = "" if epub3 else '<meta name="cover" content="cov"/>'
    props = ' properties="cover-image"' if epub3 else ""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>""")
        z.writestr("OEBPS/content.opf", f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>
<dc:language>th</dc:language><dc:identifier id="id">x</dc:identifier>{cover_meta}</metadata>
<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/>
<item id="cov" href="images/pic%201.jpg" media-type="image/jpeg"{props}/></manifest>
<spine><itemref idref="c"/></spine></package>""")
        z.writestr("OEBPS/c.xhtml", """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c</title></head><body><p>สวัสดี</p>
<p><img src="images/pic%201.jpg" alt=""/></p></body></html>""")
        z.writestr("OEBPS/images/pic 1.jpg", _jpeg())
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import config, library, main, opds, x4
    from app.store import Store
    db = Store(tmp_path / "app.db")
    monkeypatch.setattr(library, "store", db)
    monkeypatch.setattr(opds, "store", db)
    monkeypatch.setattr(library, "settings", dataclasses.replace(config.settings, data_dir=tmp_path))
    sent = []
    monkeypatch.setattr(x4, "enqueue", lambda key, title, files, folder="": sent.append((key, title, files)) or
                        {"state": "queued", "title": title})
    c = TestClient(main.app)          # no `with`: the translation workers stay off
    c.sent = sent
    c.dir = tmp_path / "library"
    return c


def _upload(client, *files):
    return client.post("/api/library", files=[("files", (n, d, "application/epub+zip")) for n, d in files])


def test_upload_reads_title_author_cover(client):
    original = _epub()
    r = _upload(client, ("hound.epub", original), ("old.epub", _epub("Old Book", "Someone", epub3=False)))
    assert r.status_code == 200
    books = [x["book"] for x in r.json()]
    assert books[0]["name"] == "หมาปิศาจแห่งบาสเกอร์วิลส์" and books[0]["author"] == "อาเธอร์ โคนัน ดอยล์"
    assert books[1]["name"] == "Old Book" and books[1]["cover"] == 1      # EPUB 2 <meta name="cover">
    assert (client.dir / f"{books[0]['id']}.epub").read_bytes() == original  # kept as uploaded
    cover = client.get(f"/api/library/{books[0]['id']}/cover")
    assert cover.status_code == 200 and Image.open(io.BytesIO(cover.content)).height <= 360
    assert [d["id"] for d in client.get("/api/library").json()] == [books[1]["id"], books[0]["id"]]


def test_upload_rejects_non_epub_but_keeps_the_good_ones(client):
    assert _upload(client, ("x.epub", b"not a zip")).status_code == 400
    r = _upload(client, ("x.epub", b"not a zip"), ("ok.epub", _epub()))
    assert r.status_code == 200
    assert "error" in r.json()[0] and "book" in r.json()[1]
    assert len(client.get("/api/library").json()) == 1
    assert not list(client.dir.glob("*.tmp"))


def test_opds_root_has_two_sections_and_library_lists_epubs(client):
    bid = _upload(client, ("hound.epub", _epub()))
    bid = bid.json()[0]["book"]["id"]
    root = ET.fromstring(client.get("/opds").content)
    links = [e.find("a:link", NS).attrib for e in root.findall("a:entry", NS)]
    assert [l["href"] for l in links] == ["/opds/translated", "/opds/library"]
    assert all("application/atom+xml" in l["type"] for l in links)       # CrossPoint: navigation entries
    feed = ET.fromstring(client.get("/opds/library").content)
    (entry,) = feed.findall("a:entry", NS)
    assert entry.find("a:title", NS).text == "หมาปิศาจแห่งบาสเกอร์วิลส์"
    assert entry.find("a:author/a:name", NS).text == "อาเธอร์ โคนัน ดอยล์"
    acq = [l.attrib for l in entry.findall("a:link", NS) if l.get("rel") == ACQ]
    assert acq == [{"rel": ACQ, "type": "application/epub+zip", "href": f"/opds/library/{bid}.epub"}]


def test_opds_library_pages(client, monkeypatch):
    from app import opds
    monkeypatch.setattr(opds, "LIBRARY_PAGE", 2)
    _upload(client, *[(f"{i}.epub", _epub(f"Book {i}")) for i in range(5)])
    p1 = ET.fromstring(client.get("/opds/library").content)
    assert len(p1.findall("a:entry", NS)) == 2
    nxt = [l.get("href") for l in p1.findall("a:link", NS) if l.get("rel") == "next"]
    assert nxt == ["/opds/library?page=2"]
    p3 = ET.fromstring(client.get("/opds/library?page=3").content)
    assert len(p3.findall("a:entry", NS)) == 1
    assert not [l for l in p3.findall("a:link", NS) if l.get("rel") == "next"]


def test_opds_download_has_x4_pictures_and_is_cached(client):
    original = _epub()
    bid = _upload(client, ("hound.epub", original)).json()[0]["book"]["id"]
    r = client.get(f"/opds/library/{bid}.epub")
    assert r.status_code == 200 and r.headers["content-type"] == "application/epub+zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.infolist()[0].filename == "mimetype" and z.infolist()[0].compress_type == zipfile.ZIP_STORED
    im = Image.open(io.BytesIO(z.read("OEBPS/images/pic 1.jpg")))
    assert im.mode == "L" and im.width <= 480 and im.height <= 800
    assert not (im.info.get("progressive") or im.info.get("progression"))
    assert b"\xe0\xb8\xaa\xe0\xb8\xa7\xe0\xb8\xb1\xe0\xb8\xaa\xe0\xb8\x94\xe0\xb8\xb5" in z.read("OEBPS/c.xhtml")
    assert len(list(client.dir.glob(f"{bid}.x4-*.epub"))) == 1
    assert client.get(f"/opds/library/{bid}.epub").content == r.content
    # the original download stays untouched
    assert client.get(f"/api/library/{bid}/download").content == original


def test_rename_changes_title_on_the_x4_copy(client):
    bid = _upload(client, ("hound.epub", _epub())).json()[0]["book"]["id"]
    client.get(f"/opds/library/{bid}.epub")
    r = client.patch(f"/api/library/{bid}", json={"name": "01 หมาปิศาจ", "author": "ดอยล์"})
    assert r.json()["name"] == "01 หมาปิศาจ"
    z = zipfile.ZipFile(io.BytesIO(client.get(f"/opds/library/{bid}.epub").content))
    opf = z.read("OEBPS/content.opf").decode()
    assert "<dc:title>01 หมาปิศาจ</dc:title>" in opf and "<dc:creator>ดอยล์</dc:creator>" in opf
    entry = ET.fromstring(client.get("/opds/library").content).find("a:entry", NS)
    assert entry.find("a:title", NS).text == "01 หมาปิศาจ"


def test_send_library_book_to_x4(client):
    bid = _upload(client, ("hound.epub", _epub())).json()[0]["book"]["id"]
    assert client.post(f"/api/x4/send/library/{bid}").status_code == 200
    ((key, title, files),) = client.sent
    assert key == f"library:{bid}" and title == "หมาปิศาจแห่งบาสเกอร์วิลส์"
    (name, data), = files
    assert name == "หมาปิศาจแห่งบาสเกอร์วิลส์.epub"
    pic = zipfile.ZipFile(io.BytesIO(data)).read("OEBPS/images/pic 1.jpg")
    assert Image.open(io.BytesIO(pic)).mode == "L"


def test_delete_removes_files_and_entry(client):
    bid = _upload(client, ("hound.epub", _epub())).json()[0]["book"]["id"]
    client.get(f"/opds/library/{bid}.epub")
    assert list(client.dir.glob(f"{bid}.*"))
    assert client.delete(f"/api/library/{bid}").json() == {"ok": True}
    assert not list(client.dir.glob(f"{bid}.*"))
    assert client.get("/api/library").json() == []
    assert client.get(f"/opds/library/{bid}.epub").status_code == 404
    assert ET.fromstring(client.get("/opds/library").content).findall("a:entry", NS) == []
    assert client.delete(f"/api/library/{bid}").status_code == 404
