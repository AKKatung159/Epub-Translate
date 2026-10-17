"""OPDS catalog of the translated books, for e-readers on the LAN (CrossPoint on the Xteink X4, KOReader...).

CrossPoint only follows acquisition links of type application/epub+zip and saves them as "<title>.epub", so the
catalog lists EPUBs only; PDF -> X4 files (.xtc) go over with "send to X4" instead.

/opds is a navigation feed with two sections: translated books (/opds/translated) and the library of ready-made
EPUBs (/opds/library, in pages: CrossPoint keeps at most ~60 entries per feed and follows rel="next").
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .store import store

ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog"'
ACQ = "http://opds-spec.org/acquisition"
NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
LIBRARY_PAGE = 50


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def books() -> list[dict]:
    """Jobs with something translated, newest first: (job, percent)."""
    out = []
    for j in store.jobs():
        c = store.counts(j["id"])
        total = sum(v["chars"] for v in c.values())
        done = sum(v["chars"] for k, v in c.items() if k != "pending")
        if total and done:
            out.append({**j, "percent": round(100 * done / total)})
    return out


def _feed(fid: str, title: str, self_href: str, entries: list[str], kind: str = ACQ_TYPE, extra: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed {ATOM_NS}>
  <id>urn:epub-translate:{fid}</id>
  <title>{escape(title)}</title>
  <updated>{_iso(time.time())}</updated>
  <link rel="self" type="{kind}" href="{self_href}"/>
  <link rel="start" type="{NAV_TYPE}" href="/opds"/>
{extra}{chr(10).join(entries)}
</feed>
"""


def root() -> str:
    from . import library
    n_books, n_lib = len(books()), len(library.rows())
    sections = [("translated", "หนังสือแปล", f"แปลจากภาษาอังกฤษ {n_books} เล่ม"),
                ("library", "คลังหนังสือ", f"หนังสือที่อัปโหลดเอง {n_lib} เล่ม")]
    entries = [f"""  <entry>
    <title>{escape(title)}</title>
    <id>urn:epub-translate:{key}</id>
    <updated>{_iso(time.time())}</updated>
    <content type="text">{escape(summary)}</content>
    <link rel="subsection" type="{ACQ_TYPE}" href="/opds/{key}"/>
  </entry>""" for key, title, summary in sections]
    return _feed("catalog", "แปลหนังสือ (epub.lan)", "/opds", entries, NAV_TYPE)


def feed() -> str:
    """Translated books."""
    entries = []
    for b in books():
        whole = b["status"] == "done"
        title = f"{b['name']} (ไทย)" if whole else f"{b['name']} (ไทย {b['percent']}%)"
        updated = b["stats"].get("finished") or b["created"]
        summary = "แปลครบแล้ว" if whole else f"แปลแล้ว {b['percent']}% ส่วนที่ยังไม่แปลเป็นภาษาอังกฤษ"
        entries.append(f"""  <entry>
    <title>{escape(title)}</title>
    <id>urn:epub-translate:{b['id']}</id>
    <updated>{_iso(updated)}</updated>
    <content type="text">{escape(summary)}</content>
    <link rel="{ACQ}" type="application/epub+zip" href="/opds/books/{b['id']}.epub"/>
  </entry>""")
    return _feed("translated", "หนังสือแปล", "/opds/translated", entries)


def library_feed(page: int = 1) -> str:
    """Ready-made EPUBs from the library, newest first, LIBRARY_PAGE per page."""
    from . import library
    items = library.rows()
    pages = max(1, -(-len(items) // LIBRARY_PAGE))
    page = min(max(1, page), pages)
    entries = []
    for d in items[(page - 1) * LIBRARY_PAGE:page * LIBRARY_PAGE]:
        author = f"\n    <author><name>{escape(d['author'])}</name></author>" if d["author"] else ""
        cover = (f'\n    <link rel="http://opds-spec.org/image/thumbnail" type="image/jpeg" '
                 f'href="/api/library/{d["id"]}/cover"/>') if d["cover"] else ""
        entries.append(f"""  <entry>
    <title>{escape(d['name'])}</title>{author}
    <id>urn:epub-translate:library:{d['id']}</id>
    <updated>{_iso(d['created'])}</updated>
    <content type="text">{d['size'] / 1048576:.1f} MB</content>{cover}
    <link rel="{ACQ}" type="application/epub+zip" href="/opds/library/{d['id']}.epub"/>
  </entry>""")
    extra = ""
    if page > 1:
        extra += f'  <link rel="previous" type="{ACQ_TYPE}" href="/opds/library?page={page - 1}"/>\n'
    if page < pages:
        extra += f'  <link rel="next" type="{ACQ_TYPE}" href="/opds/library?page={page + 1}"/>\n'
    title = "คลังหนังสือ" + (f" ({page}/{pages})" if pages > 1 else "")
    return _feed("library", title, f"/opds/library?page={page}" if page > 1 else "/opds/library", entries, extra=extra)
