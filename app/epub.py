"""Read an EPUB, cut it into translatable segments, and write it back with translations.

A *segment* is a leaf block element (a <p>, heading, list item, table cell, ... that holds text and no
other block). Its inline markup (<i>, <b>, <span>, <a>, <br/>, <img/> ...) is turned into short numbered
tags so the model can move them with the words, then turned back into the original elements:

    <p>He said <i>no</i>.<br/>Then left.</p>   ->   He said [1]no[/1].[2/]Then left.

Digits in square brackets, because Typhoon Translate transliterates letters inside tags (<p1> came back as <พ1>).

Everything that is not a segment (images, CSS, fonts, structure, attributes, ids) is copied byte for byte
or kept as-is in the tree, so the layout of the book does not change.
"""
from __future__ import annotations

import copy
import html.entities
import io
import posixpath
import re
import zipfile
from dataclasses import dataclass, field

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

BLOCK = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th", "caption", "figcaption",
    "blockquote", "div", "section", "article", "aside", "header", "footer", "address", "nav", "figure",
    "ul", "ol", "dl", "table", "thead", "tbody", "tfoot", "tr", "body", "hr", "center",
}
SKIP = {"script", "style", "pre", "code", "svg", "math", "head", "noscript"}
VOID = {"br", "img", "hr", "wbr", "input", "image"}

TAG_RE = re.compile(r"\[(/?)(\d+)(/?)\]")
_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")


def _local(el) -> str:
    tag = el.tag
    if not isinstance(tag, str):
        return ""
    return etree.QName(tag).localname.lower()


def _fix_entities(data: bytes) -> bytes:
    """HTML named entities (&nbsp; &mdash; ...) are not valid XML; turn them into numeric references."""
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp else m.group(0)

    text = data.decode("utf-8", errors="replace")
    return _ENTITY_RE.sub(repl, text).encode("utf-8")


def parse_xml(data: bytes):
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True, remove_blank_text=False)
    root = etree.fromstring(_fix_entities(data), parser)
    if root is None:
        raise ValueError("cannot parse document")
    return root


def serialize_xml(root, original: bytes) -> bytes:
    head = original[:300].decode("utf-8", errors="ignore")
    m = re.search(r"<!DOCTYPE[^>]*>", head, re.I | re.S)
    out = etree.tostring(root, encoding="utf-8", xml_declaration=False)
    prefix = b'<?xml version="1.0" encoding="utf-8"?>\n'
    if m:
        prefix += m.group(0).encode("utf-8") + b"\n"
    return prefix + out


# ---------------------------------------------------------------------------------------------- segments

@dataclass
class Segment:
    doc: str            # zip path of the document
    path: str           # xpath of the element inside the document
    marked: str         # source with numbered inline tags
    plain: str          # source text only (for glossary, QA, counting)
    kind: str = "text"  # text | nav (NCX label) | title (<title> in head)


def _is_leaf_block(el) -> bool:
    for d in el.iterdescendants():
        if _local(d) in BLOCK:
            return False
    return True


def _has_text(el) -> bool:
    return bool("".join(el.itertext()).strip())


def _unwrap_target(el):
    """If one inline child wraps all the content (<p><span class="x">...</span></p>), translate inside it."""
    while True:
        kids = [c for c in el if isinstance(c.tag, str)]
        if len(kids) != 1 or (el.text or "").strip() or (kids[0].tail or "").strip():
            return el
        k = kids[0]
        if _local(k) in VOID or _local(k) in SKIP or not _has_text(k):
            return el
        el = k


class Marker:
    """Builds the numbered-tag form of an element and puts a translation back into it."""

    def __init__(self, start: int = 1):
        self.n = start
        self.map: dict[int, tuple[str, object]] = {}

    def mark(self, el) -> str:
        parts = [_esc(el.text or "")]
        for child in el:
            if not isinstance(child.tag, str):          # comment / PI: keep its tail text only
                parts.append(_esc(child.tail or ""))
                continue
            name = _local(child)
            n = self.n
            self.n += 1
            shell = copy.copy(child)
            for c in list(shell):
                shell.remove(c)
            shell.text = None
            shell.tail = None
            if name == "br":
                self.map[n] = ("b", shell)
                parts.append(f"[{n}/]")
            elif name in VOID or name in SKIP or not _has_text(child):
                self.map[n] = ("v", copy.deepcopy(child))
                self.map[n][1].tail = None
                parts.append(f"[{n}/]")
            else:
                self.map[n] = ("t", shell)
                parts.append(f"[{n}]" + self.mark(child) + f"[/{n}]")
            parts.append(_esc(child.tail or ""))
        return "".join(parts)


def _esc(s: str) -> str:
    return s.replace("[", "⟦").replace("]", "⟧")   # keep literal [ ] out of the tag syntax


def _unesc(s: str) -> str:
    return s.replace("⟦", "[").replace("⟧", "]")


def paired(marked: str) -> list[int]:
    """Numbers of the [n]...[/n] tags in a marked string."""
    return sorted(int(m.group(2)) for m in TAG_RE.finditer(marked) if m.group(1) and not m.group(3))


def check_tags(src_marked: str, out: str) -> str | None:
    """Return an error message if `out` does not carry the same [n]...[/n] pairs, properly nested."""
    want = paired(src_marked)
    stack, got = [], []
    for m in TAG_RE.finditer(out):
        closing, num, selfclose = m.group(1), int(m.group(2)), m.group(3)
        if num not in want:
            continue                                  # line breaks / anchors may move or drop
        if closing:
            if not stack or stack[-1] != num:
                return f"tag [/{num}] out of order"
            stack.pop()
        elif selfclose:
            return f"tag [{num}/] must have a closing [/{num}]"
        else:
            stack.append(num)
            got.append(num)
    if stack:
        return "unclosed tag"
    if sorted(got) != want:
        return f"tags differ: want {want}, got {sorted(got)}"
    return None


def restore(el, translated: str, marker_map: dict[int, tuple[str, object]]) -> None:
    """Replace el's content with `translated` (numbered-tag form), rebuilding the original inline elements."""
    attrib, tail = dict(el.attrib), el.tail
    for c in list(el):
        el.remove(c)
    el.text = None
    el.attrib.clear()
    el.attrib.update(attrib)
    el.tail = tail

    used: set[int] = set()
    stack = [el]
    last = {id(el): None}   # last child appended to each open element, for tail text

    def add_text(s: str):
        if not s:
            return
        s = _unesc(s)
        cur = stack[-1]
        lc = last[id(cur)]
        if lc is None:
            cur.text = (cur.text or "") + s
        else:
            lc.tail = (lc.tail or "") + s

    def add_child(node):
        cur = stack[-1]
        cur.append(node)
        last[id(cur)] = node

    pos = 0
    for m in TAG_RE.finditer(translated):
        add_text(translated[pos:m.start()])
        pos = m.end()
        closing, n = m.group(1), int(m.group(2))
        if n not in marker_map:
            continue
        kind, shell = marker_map[n]
        if closing:
            if kind == "t" and len(stack) > 1:
                stack.pop()
            continue
        if n in used:
            continue
        node = copy.deepcopy(shell)
        node.tail = None
        add_child(node)
        used.add(n)
        if kind == "t":
            stack.append(node)
            last[id(node)] = None
    add_text(translated[pos:])

    # anchors / images / line breaks the model dropped: put them back at the start so ids and pictures survive
    missing = [n for n, (k, _) in marker_map.items() if k == "v" and n not in used]
    for n in reversed(missing):
        node = copy.deepcopy(marker_map[n][1])
        node.tail = el.text
        el.text = None
        el.insert(0, node)


# ---------------------------------------------------------------------------------------------- the book

@dataclass
class Book:
    files: dict[str, bytes]
    order: list[str]                     # zip entry order
    opf_path: str
    docs: list[str]                      # spine XHTML docs, in reading order
    ncx_path: str | None
    titles: dict[str, str] = field(default_factory=dict)   # doc -> chapter title (for progress display)

    @classmethod
    def load(cls, data: bytes) -> "Book":
        zf = zipfile.ZipFile(io.BytesIO(data))
        files = {i.filename: zf.read(i.filename) for i in zf.infolist() if not i.is_dir()}
        order = [i.filename for i in zf.infolist() if not i.is_dir()]
        container = parse_xml(files["META-INF/container.xml"])
        rootfile = container.find(".//{*}rootfile")
        opf_path = rootfile.get("full-path")
        opf = parse_xml(files[opf_path])
        base = posixpath.dirname(opf_path)
        manifest = {}
        for item in opf.iterfind(".//{*}manifest/{*}item"):
            href = posixpath.normpath(posixpath.join(base, item.get("href", "")))
            manifest[item.get("id")] = (href, item.get("media-type", ""), item.get("properties", ""))
        docs = []
        for ref in opf.iterfind(".//{*}spine/{*}itemref"):
            href, mt, _ = manifest.get(ref.get("idref"), (None, "", ""))
            if href and href in files and href not in docs and ("html" in mt or href.endswith((".html", ".xhtml", ".htm"))):
                docs.append(href)          # some books list the same file twice in the spine: translate it once
        for href, mt, props in manifest.values():       # EPUB 3 nav document, if it is not in the spine
            if "nav" in props.split() and href in files and href not in docs:
                docs.append(href)
        ncx = next((h for h, mt, _ in manifest.values() if mt == "application/x-dtbncx+xml" and h in files), None)
        return cls(files=files, order=order, opf_path=opf_path, docs=docs, ncx_path=ncx)

    def segments(self) -> list[Segment]:
        out: list[Segment] = []
        for doc in self.docs:
            root = parse_xml(self.files[doc])
            tree = root.getroottree()
            title_el = root.find(".//{*}head/{*}title")
            if title_el is not None and (title_el.text or "").strip():
                out.append(Segment(doc, tree.getpath(title_el), _esc(title_el.text), title_el.text.strip(), "title"))
            body = root.find(".//{*}body")
            if body is None:
                continue
            first_heading = None
            for el in body.iter():
                name = _local(el)
                if name not in BLOCK or name in ("body", "hr"):
                    continue
                if any(_local(a) in SKIP for a in el.iterancestors()):
                    continue
                if not _is_leaf_block(el) or not _has_text(el):
                    continue
                target = _unwrap_target(el)
                marked = Marker().mark(target)
                plain = " ".join("".join(target.itertext()).split())
                if not re.search(r"[A-Za-z]", plain):
                    continue          # numbers, ornaments, already-Thai text: nothing to translate
                out.append(Segment(doc, tree.getpath(target), marked, plain))
                if first_heading is None and name in ("h1", "h2", "h3"):
                    first_heading = plain
            self.titles[doc] = (first_heading or (title_el.text.strip() if title_el is not None and title_el.text else "")
                                or posixpath.basename(doc))[:80]
        if self.ncx_path:
            root = parse_xml(self.files[self.ncx_path])
            tree = root.getroottree()
            for t in root.iterfind(".//{*}navLabel/{*}text"):
                if (t.text or "").strip() and re.search(r"[A-Za-z]", t.text):
                    out.append(Segment(self.ncx_path, tree.getpath(t), _esc(t.text), t.text.strip(), "nav"))
        return out

    def build(self, translations: dict[tuple[str, str], str], *, bilingual: bool = False,
              title_suffix: str = " (ฉบับแปลไทย)", for_x4: bool = False) -> bytes:
        """translations: (doc, path) -> translated marked text. Segments without a translation stay English.
        for_x4: pictures become baseline greyscale JPEG/PNG that fit the X4's screen (see app/x4img.py)."""
        by_doc: dict[str, dict[str, str]] = {}
        for (doc, path), text in translations.items():
            by_doc.setdefault(doc, {})[path] = text
        files = dict(self.files)
        if for_x4:
            from .x4img import for_x4 as _picture_for_x4, is_picture
            for name in list(files):
                if is_picture(name):
                    files[name] = _picture_for_x4(name, files[name])

        for doc in self.docs:
            root = parse_xml(self.files[doc])
            items = by_doc.get(doc, {})
            # resolve every element first: inserting bilingual copies would shift later xpaths
            targets = []
            for path, text in items.items():
                found = root.xpath(path)
                if found:
                    targets.append((found[0], text))
            for el, text in targets:
                if _local(el) == "title":
                    el.text = _unesc(TAG_RE.sub("", text))
                    continue
                if bilingual:
                    block = el
                    while _local(block) not in BLOCK and block.getparent() is not None:
                        block = block.getparent()
                    original = copy.deepcopy(block)
                    original.tail = None
                    for e in original.iter():
                        e.attrib.pop("id", None)      # ids must stay unique (links point at the Thai copy)
                    block.addprevious(original)
                    _add_class(original, "epubtr-src")
                    _add_class(block, "epubtr-th")
                marker = Marker()
                marker.mark(el)          # same numbering as when the segment was cut
                restore(el, text, marker.map)
            _set_lang(root)
            _add_thai_style(root, bilingual)
            files[doc] = serialize_xml(root, self.files[doc])

        if self.ncx_path and self.ncx_path in by_doc:
            root = parse_xml(self.files[self.ncx_path])
            for path, text in by_doc[self.ncx_path].items():
                found = root.xpath(path)
                if found:
                    found[0].text = _unesc(TAG_RE.sub("", text))
            files[self.ncx_path] = serialize_xml(root, self.files[self.ncx_path])

        opf = parse_xml(self.files[self.opf_path])
        for lang in opf.iterfind(".//{%s}language" % DC_NS):
            lang.text = "th"
        title = opf.find(".//{%s}title" % DC_NS)
        if title is not None and title.text and title_suffix and not title.text.endswith(title_suffix):
            title.text = title.text + title_suffix
        files[self.opf_path] = serialize_xml(opf, self.files[self.opf_path])

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            names = ["mimetype"] + [n for n in self.order if n != "mimetype"]
            for name in names:
                if name not in files:
                    continue
                if name == "mimetype":
                    zf.writestr(zipfile.ZipInfo("mimetype"), files[name], compress_type=zipfile.ZIP_STORED)
                else:
                    zf.writestr(name, files[name], compress_type=zipfile.ZIP_DEFLATED)
        return buf.getvalue()


def _add_class(el, cls: str):
    cur = el.get("class", "")
    if cls not in cur.split():
        el.set("class", (cur + " " + cls).strip())


def _set_lang(root):
    root.set("lang", "th")
    root.set(XML_LANG, "th")


THAI_CSS = """
/* added by epub-translate: Thai fonts first, a bit more line height for Thai vowels/tones */
body, p, li, dd, dt, td, th, blockquote, h1, h2, h3, h4, h5, h6 {
  font-family: "Sarabun", "Noto Serif Thai", "Noto Sans Thai", "Leelawadee UI", "Thonburi", serif;
}
p, li, dd, blockquote { line-height: 1.65; }
"""
BILINGUAL_CSS = """
.epubtr-src { color: #666; font-size: 0.92em; margin-bottom: 0.2em; }
"""


def _add_thai_style(root, bilingual: bool):
    head = root.find(".//{*}head")
    if head is None or head.find("{*}style[@id='epubtr-style']") is not None:
        return
    ns = etree.QName(root).namespace
    style = etree.SubElement(head, f"{{{ns}}}style" if ns else "style")
    style.set("type", "text/css")
    style.set("id", "epubtr-style")
    style.text = THAI_CSS + (BILINGUAL_CSS if bilingual else "")
