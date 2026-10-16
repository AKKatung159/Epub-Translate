"""Readable names from download file names; the OPDS feed the X4 (CrossPoint) can follow."""
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.names import clean_name  # noqa: E402


def test_clean_name():
    assert clean_name("_OceanofPDF.com_The_Goblin_Emperor_-_Katherine_Addison.epub") == \
        "The Goblin Emperor - Katherine Addison"
    assert clean_name("_OceanofPDF.com_หมาปิศาจแห่งบาสเกอร์วิลส์_-_ดวงใจ_กวียะ.pdf") == \
        "หมาปิศาจแห่งบาสเกอร์วิลส์ - ดวงใจ กวียะ"
    assert clean_name("Project Hail Mary (z-lib.org).epub") == "Project Hail Mary"
    assert clean_name("Atomic Habits - James Clear") == "Atomic Habits - James Clear"     # already fine
    assert clean_name("") == "book"


def test_opds_feed_lists_translated_books_as_epub(monkeypatch):
    from app import opds
    monkeypatch.setattr(opds, "books", lambda: [
        {"id": "abc", "name": "Dungeon Crawler Carl", "status": "done", "percent": 100, "created": 1e9, "stats": {}},
        {"id": "def", "name": "A & B", "status": "translating", "percent": 40, "created": 1e9, "stats": {}}])
    root = ET.fromstring(opds.feed())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    entries = root.findall("a:entry", ns)
    assert [e.find("a:title", ns).text for e in entries] == ["Dungeon Crawler Carl (ไทย)", "A & B (ไทย 40%)"]
    link = entries[0].find("a:link", ns).attrib
    # CrossPoint follows only acquisition links of type application/epub+zip
    assert link["rel"] == "http://opds-spec.org/acquisition" and link["type"] == "application/epub+zip"
    assert link["href"] == "/opds/books/abc.epub"
