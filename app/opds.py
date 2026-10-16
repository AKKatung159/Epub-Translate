"""OPDS catalog of the translated books, for e-readers on the LAN (CrossPoint on the Xteink X4, KOReader...).

CrossPoint only follows acquisition links of type application/epub+zip and saves them as "<title>.epub", so the
catalog lists EPUBs only; PDF -> X4 files (.xtc) go over with "send to X4" instead.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from .store import store

ATOM_NS = 'xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog"'
ACQ = "http://opds-spec.org/acquisition"
NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"


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


def feed() -> str:
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
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed {ATOM_NS}>
  <id>urn:epub-translate:catalog</id>
  <title>แปลหนังสือ (epub.lan)</title>
  <updated>{_iso(time.time())}</updated>
  <link rel="self" type="{NAV_TYPE}" href="/opds"/>
  <link rel="start" type="{NAV_TYPE}" href="/opds"/>
{chr(10).join(entries)}
</feed>
"""
