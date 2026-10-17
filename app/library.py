"""Library: ready-made EPUBs (Thai books you already have) kept as they are, listed in the OPDS catalog next to the
translated ones and sendable to the X4. No translation, no parsing into segments.

Files: DATA_DIR/library/<id>.epub (the upload, untouched), <id>.jpg (cover thumbnail for the web UI) and
<id>.x4-<version>.epub (the copy served to the X4: pictures through x4img.for_x4, title set to the library name;
built on first request, rebuilt after a rename or when x4img changes).
"""
from __future__ import annotations

import io
import posixpath
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote

from .config import settings
from .epub import DC_NS, parse_xml, serialize_xml
from .store import store

THUMB = (240, 360)


class BadEpub(ValueError):
    pass


def lib_dir() -> Path:
    d = settings.data_dir / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(bid: str) -> Path:
    return lib_dir() / f"{bid}.epub"


def row(bid: str) -> dict | None:
    r = store.q("SELECT * FROM library WHERE id=?", (bid,))
    return dict(r[0]) if r else None


def rows() -> list[dict]:
    return [dict(r) for r in store.q("SELECT * FROM library ORDER BY created DESC")]


# reading the OPF --------------------------------------------------------------------------------------------

def _opf(zf: zipfile.ZipFile) -> tuple[str, object]:
    try:
        container = parse_xml(zf.read("META-INF/container.xml"))
        rootfile = container.find(".//{*}rootfile")
        opf_path = rootfile.get("full-path")
        return opf_path, parse_xml(zf.read(opf_path))
    except (KeyError, AttributeError, TypeError) as e:
        raise BadEpub("ไม่ใช่ไฟล์ EPUB (ไม่พบ META-INF/container.xml หรือ OPF)") from e


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split())


def _cover_href(opf, base: str, names: set[str]) -> str | None:
    """Cover picture inside the zip: EPUB 3 cover-image, EPUB 2 <meta name="cover">, else an image named cover."""
    items = [(i.get("id") or "", i.get("href") or "", i.get("media-type") or "", i.get("properties") or "")
             for i in opf.iterfind(".//{*}manifest/{*}item")]
    images = [(iid, href) for iid, href, mt, _ in items if mt.startswith("image/")]
    pick = next((href for iid, href, mt, props in items if "cover-image" in props.split()), None)
    if not pick:
        meta = next((m.get("content") for m in opf.iterfind(".//{*}metadata/{*}meta") if m.get("name") == "cover"), None)
        pick = next((href for iid, href in images if meta and iid == meta), None)
    if not pick:
        pick = next((href for iid, href in images if "cover" in (iid + href).lower()), None)
    if not pick and images:
        pick = images[0][1]
    if not pick:
        return None
    full = posixpath.normpath(posixpath.join(base, unquote(pick)))
    return full if full in names else None


def read_meta(data: bytes) -> dict:
    """title, author and cover picture bytes (or None) of an EPUB; BadEpub when it is not one."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise BadEpub("ไม่ใช่ไฟล์ EPUB (เปิดเป็น zip ไม่ได้)") from e
    opf_path, opf = _opf(zf)
    titles = [_text(el) for el in opf.iterfind(".//{%s}title" % DC_NS)]
    authors = [_text(el) for el in opf.iterfind(".//{%s}creator" % DC_NS)]
    href = _cover_href(opf, posixpath.dirname(opf_path), set(zf.namelist()))
    return {"title": next((t for t in titles if t), ""), "author": ", ".join(a for a in authors if a),
            "cover": zf.read(href) if href else None}


def _thumbnail(data: bytes) -> bytes | None:
    from PIL import Image, ImageOps
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail(THUMB)
            if im.mode not in ("RGB", "L"):
                bg = Image.new("RGB", im.size, "white")
                im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            out = io.BytesIO()
            im.save(out, "JPEG", quality=82)
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


# changes ----------------------------------------------------------------------------------------------------

def add(src: Path, file_name: str) -> dict:
    """Take the EPUB at `src` (moved into the library) and return its row."""
    from .names import clean_name
    data = src.read_bytes()
    meta = read_meta(data)
    bid = uuid.uuid4().hex[:12]
    thumb = _thumbnail(meta["cover"]) if meta["cover"] else None
    if thumb:
        (lib_dir() / f"{bid}.jpg").write_bytes(thumb)
    src.replace(path(bid))
    name = meta["title"] or clean_name(file_name)
    store.x("INSERT INTO library(id,name,author,created,size,file_name,cover) VALUES(?,?,?,?,?,?,?)",
            (bid, name[:200], meta["author"][:200], time.time(), len(data), file_name[:200], int(bool(thumb))))
    return row(bid)


def _drop_x4_copies(bid: str) -> None:
    for f in lib_dir().glob(f"{bid}.x4-*.epub"):
        f.unlink(missing_ok=True)


def update(bid: str, name: str | None = None, author: str | None = None) -> dict:
    if name is not None and name.strip():
        store.x("UPDATE library SET name=? WHERE id=?", (name.strip()[:200], bid))
    if author is not None:
        store.x("UPDATE library SET author=? WHERE id=?", (author.strip()[:200], bid))
    _drop_x4_copies(bid)
    return row(bid)


def delete(bid: str) -> None:
    store.x("DELETE FROM library WHERE id=?", (bid,))
    for f in lib_dir().glob(f"{bid}.*"):
        f.unlink(missing_ok=True)


# the copy for the X4 ----------------------------------------------------------------------------------------

def _retitle(opf_bytes: bytes, name: str, author: str) -> bytes:
    opf = parse_xml(opf_bytes)
    meta = opf.find(".//{*}metadata")
    if meta is None:
        return opf_bytes
    title = opf.find(".//{%s}title" % DC_NS)
    if title is None:
        from lxml import etree
        title = etree.SubElement(meta, "{%s}title" % DC_NS)
    title.text = name
    creators = list(opf.iterfind(".//{%s}creator" % DC_NS))
    current = ", ".join(_text(c) for c in creators)
    if author and author != current:
        if creators:
            creators[0].text = author
            for c in creators[1:]:
                c.getparent().remove(c)
        else:
            from lxml import etree
            etree.SubElement(meta, "{%s}creator" % DC_NS).text = author
    return serialize_xml(opf, opf_bytes)


def x4_epub(d: dict) -> bytes:
    """The book as served to the X4: pictures baseline grey within 480x800, title/author as named here."""
    from .x4img import _VERSION, for_x4
    cached = lib_dir() / f"{d['id']}.x4-{_VERSION.decode()}.epub"
    if cached.exists():
        return cached.read_bytes()
    zin = zipfile.ZipFile(path(d["id"]))
    opf_path, _ = _opf(zin)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zout:
        if "mimetype" in zin.namelist():
            zout.writestr(zipfile.ZipInfo("mimetype"), zin.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        else:
            zout.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename == "mimetype" or info.is_dir():
                continue
            data = zin.read(info)
            if info.filename == opf_path:
                data = _retitle(data, d["name"], d["author"])
            else:
                data = for_x4(info.filename, data)
            zout.writestr(info.filename, data, compress_type=zipfile.ZIP_DEFLATED)
    out = buf.getvalue()
    _drop_x4_copies(d["id"])
    tmp = cached.with_suffix(".tmp")
    tmp.write_bytes(out)
    tmp.replace(cached)
    return out
