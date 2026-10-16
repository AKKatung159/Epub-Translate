"""PDF -> Xteink X4 pages: each PDF page is rendered, its white margins cropped, and cut into landscape strips
that are read with the device turned sideways (800 px of width instead of 480: text 1.67x larger).

- Strips are cut in the white gap between two lines of text (largest gap near the limit), never through a
  line when a gap exists; with no gap at all (a picture), the cut overlaps the next strip a little.
- mode "auto": as wide as the screen allows (largest text), as many strips as that needs (~3 for a book page);
  "3" / "4": at most that many strips per page (smaller text on big pages, fewer page turns);
  "page": the whole page upright on 480x800 (for pictures, forms, slides).
- The PDF outline (bookmarks) becomes the chapter list of the XTC file.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from .xtc import H, W, Chapter, Writer, picture_tiles
from .xtccover import cover_page

log = logging.getLogger("pdfslice")

LONG, SHORT = H, W          # landscape strip: 800 wide, 480 high
INK = 200                   # a pixel darker than this is ink
MODES = ("auto", "3", "4", "page")


@dataclass
class Options:
    mode: str = "auto"
    rotate: str = "cw"          # landscape strips: "cw" = top of the text on the right edge of the screen
    # 1 = black/white XTC (default), 2 = 4 greys XTCH. The X4 firmware mallocs one whole page per turn: 48 KB for
    # XTC but 96 KB for XTCH, which a fragmented heap often cannot give ("Memory error" on a 70 MB XTCH book).
    depth: int = 1
    margin: int = 6             # px of white kept around the cropped content
    trim: bool = True           # drop running headers / page numbers (a lone short line at the very top/bottom)
    per_part: int = 600         # device pages per file; the X4 keeps a 16-byte entry per page in RAM
    # every file starts with a cover page (the X4 shows a file's first page as its cover): a picture on PDF page 1,
    # else a title page
    cover: bool = True


def ink_box(gray: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box (left, top, right, bottom) of the ink, ignoring specks."""
    ink = gray < INK
    rows = np.flatnonzero(ink.sum(axis=1) > 1)
    cols = np.flatnonzero(ink.sum(axis=0) > 1)
    if not len(rows) or not len(cols):
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1


def blocks(gray: np.ndarray) -> list[tuple[int, int]]:
    """Runs of rows that contain ink: (top, bottom)."""
    ink = (gray < INK).sum(axis=1) > 1
    edges = np.flatnonzero(np.diff(np.concatenate(([0], ink.astype(np.int8), [0]))))
    return list(zip(edges[::2].tolist(), edges[1::2].tolist()))


def body_rows(gray: np.ndarray) -> tuple[int, int]:
    """Rows of the page without a running header / footer (page number, book title).

    A header is the first line of ink when it is thin, sits in the top 12 % of the page and is followed by a
    gap clearly wider than the gaps between lines of text; the footer is the same at the bottom."""
    h = gray.shape[0]
    b = blocks(gray)
    if len(b) < 4:
        return (b[0][0], b[-1][1]) if b else (0, h)
    gaps = sorted(b[i + 1][0] - b[i][1] for i in range(len(b) - 1))
    line_gap = gaps[len(gaps) // 2]
    tall = sorted(e - a for a, e in b)[len(b) // 2] * 1.6         # a header line is not taller than text
    top, bottom = 0, len(b)
    if b[0][0] < h * 0.12 and b[0][1] - b[0][0] <= tall and b[1][0] - b[0][1] > max(2.2 * line_gap, h * 0.012):
        top = 1
    if b[-1][1] > h * 0.88 and b[-1][1] - b[-1][0] <= tall and b[-1][0] - b[-2][1] > max(2.2 * line_gap, h * 0.012):
        bottom -= 1
    return b[top][0], b[bottom - 1][1]


def cut_points(gray: np.ndarray, limit: int) -> list[tuple[int, int]]:
    """Split rows into (top, bottom) strips of at most `limit` rows, cutting in white gaps between lines."""
    h = gray.shape[0]
    blank = (gray < INK).sum(axis=1) == 0
    strips, y = [], 0
    while y < h:
        if h - y <= limit:
            strips.append((y, h))
            break
        lo = y + int(limit * 0.55)
        window = blank[lo:y + limit + 1]
        if window.any():
            # the white run that ends closest to the limit; cut in its middle
            idx = np.flatnonzero(window) + lo
            end = idx[-1]
            start = end
            while start - 1 >= lo and blank[start - 1]:
                start -= 1
            cut = (start + end + 1) // 2
            strips.append((y, cut))
            y = cut
        else:
            cut = y + limit                                   # no gap (a picture): overlap a little
            strips.append((y, cut))
            y = cut - limit // 12
    # drop strips with nothing on them
    return [(a, b) for a, b in strips if (gray[a:b] < INK).any()]


def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    s = min(w / img.width, h / img.height)
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def page_content(page: pdfium.PdfPage, o: Options) -> Image.Image | None:
    """Render one PDF page, drop header/footer, crop the white margins."""
    pw, _ = page.get_size()
    # render so that the content is ~2x the target width, then downscale (sharper text)
    target = SHORT if o.mode == "page" else LONG
    gray = page.render(scale=max(1.0, 2 * target / pw), grayscale=True).to_pil().convert("L")
    arr = np.asarray(gray)
    if o.trim:
        y0, y1 = body_rows(arr)
        arr = arr.copy()
        arr[:y0] = 255
        arr[y1:] = 255
        gray = Image.fromarray(arr)
    box = ink_box(arr)
    if box is None:
        return None
    m = o.margin * 2
    l, t, r, b = box
    return gray.crop((max(0, l - m), max(0, t - m), min(gray.width, r + m), min(gray.height, b + m)))


def is_cover(content: Image.Image) -> bool:
    """A page that is mostly picture (a cover, a full-page illustration), not a page of text."""
    return float(picture_tiles(np.asarray(whole_page(content))).mean()) >= 0.25


def whole_page(content: Image.Image) -> Image.Image:
    canvas = Image.new("L", (W, H), 255)
    img = _fit(content, W, H)
    canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    return canvas


def strip_source(content: Image.Image, o: Options) -> Image.Image:
    """Content scaled for landscape strips: 800 px wide, unless "at most N strips per page" asks for smaller."""
    s = LONG / content.width
    if o.mode in ("3", "4"):
        s = min(s, int(o.mode) * SHORT * 0.92 / content.height)    # 0.92: room for cutting in gaps
    return content.resize((max(1, round(content.width * s)), max(1, round(content.height * s))), Image.LANCZOS)


class Flow:
    """Page bodies one under the other, like a scroll; full strips are taken off the top as soon as the next
    cut is certain. So a page's last few lines share a strip with the next page instead of leaving it half
    empty (an A5 novel: ~10 % fewer pages to turn)."""

    GAP = 14                                                     # px between two PDF pages, like a paragraph

    def __init__(self, o: Options, emit: Callable[[Image.Image], None]):
        self.o, self.emit, self.buf, self.count = o, emit, None, 0

    def _out(self, strip: Image.Image) -> None:
        canvas = Image.new("L", (LONG, SHORT), 255)
        canvas.paste(strip, ((LONG - strip.width) // 2, 0))      # top-aligned: the eye starts in the same place
        self.emit(canvas.transpose(Image.ROTATE_270 if self.o.rotate == "cw" else Image.ROTATE_90))
        self.count += 1

    def add(self, img: Image.Image) -> int:
        """Append a page body; returns the index of the device page where it starts."""
        if self.buf is None:
            y0, self.buf = 0, img
        else:
            y0 = self.buf.height + self.GAP
            joined = Image.new("L", (max(self.buf.width, img.width), y0 + img.height), 255)
            joined.paste(self.buf, ((joined.width - self.buf.width) // 2, 0))
            joined.paste(img, ((joined.width - img.width) // 2, y0))
            self.buf = joined
        cuts = cut_points(np.asarray(self.buf), SHORT)
        start = self.count + next((k for k, (_, bottom) in enumerate(cuts) if bottom > y0), len(cuts) - 1)
        for top, bottom in cuts[:-1]:                            # the last strip may still grow
            self._out(self.buf.crop((0, top, self.buf.width, bottom)))
        if cuts:
            self.buf = self.buf.crop((0, cuts[-1][0], self.buf.width, self.buf.height))
        return start

    def flush(self) -> None:
        if self.buf is not None:
            for top, bottom in cut_points(np.asarray(self.buf), SHORT):
                self._out(self.buf.crop((0, top, self.buf.width, bottom)))
            self.buf = None


MAX_CHAPTERS = 64      # entries when there is nothing better than "every N pages"
HEADING = re.compile(r"^\s*((?:บทที่|ตอนที่|ตอน|บท|chapter|CHAPTER|Chapter)\s*[0-9๐-๙]+[^\n]{0,60})", re.M)


def outline(pdf: pdfium.PdfDocument) -> list[tuple[str, int]]:
    """(title, 0-based page) for the PDF bookmarks: top level, plus the second level only if all of it fits."""
    out = []
    try:
        items = list(pdf.get_toc(max_depth=2))
        if len(items) > 3000:                                  # a huge outline: top level only
            items = [i for i in items if i.level == 0]
        for item in items:
            dest = item.get_dest()
            idx = dest.get_index() if dest is not None else None
            if idx is not None and item.get_title():
                out.append((("  " * item.level) + item.get_title().strip()[:60], idx))
    except Exception as e:  # noqa: BLE001 - a broken outline must not stop the conversion
        log.warning("outline: %s", e)
    return out


def text_marks(pdf: pdfium.PdfDocument) -> list[tuple[str, int]]:
    """Chapters from the page text when the PDF has no bookmarks: a line near the top that starts with
    "บทที่ 12", "ตอนที่ 3", "Chapter 7"... (scanned PDFs have no text: nothing is found)."""
    out = []
    for i in range(len(pdf)):
        page = pdf[i]
        try:
            tp = page.get_textpage()
            head = tp.get_text_range()[:400]
            tp.close()
        except Exception:  # noqa: BLE001
            head = ""
        finally:
            page.close()
        m = HEADING.search(head)
        if m:
            out.append((re.sub(r"\s+", " ", m.group(1)).strip(), i))
    return out


def convert(src: Path, dst: Path, o: Options, title: str = "",
            progress: Callable[[int, int, int], None] | None = None,
            should_stop: Callable[[], bool] | None = None) -> int:
    """Write dst (.xtch / .xtc), or "<stem> 01.xtc", "<stem> 02.xtc"... next to it when the book is longer than
    o.per_part device pages (plus a cover page each). Returns the number of device pages in all files."""
    pdf = pdfium.PdfDocument(str(src))
    try:
        n = len(pdf)
        meta = pdf.get_metadata_dict(skip_empty=True)
        marks = [(t, p) for t, p in outline(pdf) if 0 <= p < n] or text_marks(pdf)
        chapter_pages = {p for _, p in marks}
        title = title or meta.get("Title", "") or src.stem
        author = meta.get("Author", "")
        art = None                                             # the cover picture, when PDF page 1 is one
        writer = Writer(o.depth, (lambda label: cover_page(art, title, author, o.depth, label)) if o.cover else None)
        flow = Flow(o, lambda img: writer.add(np.asarray(img, np.uint8)))
        first_of: list[int] = []                               # PDF page -> first device page
        for i in range(n):
            if should_stop and should_stop():
                raise InterruptedError
            page = pdf[i]
            try:
                content = page_content(page, o)
            finally:
                page.close()
            if i == 0 and o.cover and content is not None and is_cover(content):
                art = content                                  # the cover page of every file, not a page of text
                first_of.append(0)
                if progress:
                    progress(i + 1, n, len(writer))
                continue
            if o.mode == "page":
                first_of.append(len(writer))
                if content is not None:
                    writer.add(np.asarray(whole_page(content), np.uint8))
            else:
                if i in chapter_pages:
                    flow.flush()                               # a chapter starts on a fresh device page
                first_of.append(flow.add(strip_source(content, o)) if content is not None else len(writer))
            if progress:
                progress(i + 1, n, len(writer))
        flow.flush()
        if not len(writer) and art is not None:               # a one-page PDF that is only a picture
            writer.add(np.asarray(whole_page(art), np.uint8))
        total = len(writer)
        if not total:
            raise ValueError("ไม่พบเนื้อหาใน PDF นี้ (หน้าว่างทั้งหมด?)")
        first_of = [min(f, total - 1) for f in first_of]
        chapters = []
        for k, (t, p) in enumerate(marks):
            start = first_of[p]
            end = first_of[marks[k + 1][1]] - 1 if k + 1 < len(marks) else total - 1
            chapters.append(Chapter(t, start, max(start, end)))
        if not chapters and n > 1:
            # no bookmarks: one entry every 10 PDF pages, so the chapter menu still works as "go to page"
            every = max(10, -(-n // MAX_CHAPTERS))
            for p in range(0, n, every):
                end = first_of[p + every] - 1 if p + every < n else total - 1
                chapters.append(Chapter(f"หน้า {p + 1}", first_of[p], max(first_of[p], end)))
        def page_label(s: int, e: int) -> str:
            """PDF pages shown on device pages s..e-1."""
            pages = [p for p, f in enumerate(first_of) if s <= f < e] or [0]
            return f"หน้า {pages[0] + 1}-{pages[-1] + 1}"
        files = writer.save_parts(dst.parent, dst.stem, title, author, chapters, o.per_part, page_label)
        return total + (len(files) if o.cover else 0)
    finally:
        pdf.close()
