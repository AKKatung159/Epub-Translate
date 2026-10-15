"""Translate segments in chunks with Typhoon Translate, check every result, and fall back when needed.

Per chunk (consecutive paragraphs of one chapter, ~CHUNK_CHARS English characters):
  1. one request with every paragraph wrapped in [[N]]...[[/N]] and the glossary entries that occur in it
     given as rules ("Translate "X" as "Y"");
  2. each paragraph is checked: really Thai, no English left behind, sane length, no repetition loop,
     no leaked markers, glossary names fixed up when the English name slipped through, same [n]...[/n] marks;
  3. paragraphs that fail are retried one by one. When only italics/links break but the words are right,
     the words are kept (noted, not flagged); only wrong content is flagged for review.
A book never stops on one bad paragraph.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import llm
from .config import settings
from .epub import TAG_RE, check_tags

log = logging.getLogger("translate")

THAI = re.compile(r"[฀-๿]")
LATIN = re.compile(r"[A-Za-z]")
P_RE = re.compile(r"\[\[(\d+)\]\](.*?)\[\[/\1\]\]", re.S)
LOOP_RE = re.compile(r"(.{6,40}?)\1{4,}", re.S)
ENGLISH_RUN = re.compile(r"(?:\b[A-Za-z]{2,}\b[\s,;:'’\-]+){3,}\b[A-Za-z]{2,}\b")   # 4+ English words in a row
JUNK = re.compile(r"\s*\[\[/?\d+\]\](?:\.{3}|…)?\s*")                                  # stray [[N]] / "[[1]]..."
THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
SIMPLIFIED = "รูปแบบตัวเอียง/ลิงก์บางส่วนหายไป"

BASE_RULES = [
    "This is a published book (fiction or non-fiction). Write natural, fluent Thai prose of publishing quality, "
    "not word-for-word; keep the author's tone, register and paragraphing.",
    "Each [[N]]...[[/N]] block is one paragraph: keep every block, in the same order, with the same number, "
    "and translate only the text inside.",
    "Markers like [3]...[/3] and [4/] mark italics, links and line breaks: keep each of them exactly once, "
    "around the Thai words that correspond to the English inside them.",
    "Use the Thai names given in these rules every time the name appears; do not change their spelling.",
    "Dialogue stays in quotation marks “ ”. Keep numbers, dates and units correct, written with Arabic digits "
    "(1, 2, 3), not Thai digits.",
    "Transliterate other proper nouns following the Royal Institute (ราชบัณฑิตยสถาน) transliteration rules; "
    "use Royal Institute coined terms for technical words, or transliterate when there is none.",
    "Do not add explanations, notes or anything that is not in the source.",
]
PARA_RULE, MARK_RULE = 1, 2          # indexes into BASE_RULES


@dataclass
class SegIn:
    id: int
    marked: str
    plain: str


@dataclass
class SegOut:
    id: int
    marked: str
    status: str        # done | flagged
    note: str = ""


def _rules(glossary_hits: list[dict], style_notes: str, *, chunk: bool = True, marks: bool = True) -> list[str]:
    rules = []
    for e in glossary_hits:
        if e.get("keep_english"):
            rules.append(f'Keep "{e["en"]}" in English.')
        elif e.get("th"):
            rules.append(f'Translate "{e["en"]}" as "{e["th"]}".')
    for i, r in enumerate(BASE_RULES):
        if (i == PARA_RULE and not chunk) or (i == MARK_RULE and not marks):
            continue          # a single paragraph must not hear about [[N]] blocks, or it invents them
        rules.append(r)
    if style_notes.strip():
        rules += [s.strip() for s in style_notes.strip().splitlines() if s.strip()]
    return rules


def _prompt(source: str, rules: list[str]) -> list[dict]:
    body = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(rules))
    return [{"role": "user", "content":
             "Translate the following English text into Thai, strictly following the rules below and return only "
             f"the translated text.\nRules:\n{body}\nSource Text:\n{source}"}]


def _max_tokens(chars: int) -> int:
    return min(12000, int(chars * 1.6) + 300)


def _clean(text: str) -> str:
    """Remove stray [[N]] paragraph markers / '[[1]]...' placeholders, and use Arabic digits."""
    return JUNK.sub(" ", text).strip().translate(THAI_DIGITS)


def _simplified(marked: str, out: str) -> str:
    """Keep the words, drop the broken [n]...[/n] pairs; anchors/images/line breaks are put back by epub.restore."""
    paired_nums = {m.group(2) for m in TAG_RE.finditer(marked) if m.group(1)}
    return TAG_RE.sub(lambda m: "" if m.group(2) in paired_nums else m.group(0), out)


def qa(src_plain: str, out_marked: str, keep_english: tuple[str, ...] = ()) -> str | None:
    if "[[" in out_marked or "]]" in out_marked or re.search(r"</?[a-z]\d+/?>", out_marked):
        return "paragraph marker leaked into the text"
    text = TAG_RE.sub("", out_marked).strip()
    letters = len(LATIN.findall(src_plain))
    if letters == 0:
        return None
    if not text:
        return "empty"
    th, la = len(THAI.findall(text)), len(LATIN.findall(text))
    if letters >= 20 and th < 0.3 * (th + la):
        return "not translated"
    rest = text
    for term in keep_english:
        rest = rest.replace(term, " ")
    m = ENGLISH_RUN.search(rest) if letters >= 40 else None
    if m:
        return "part left in English: " + m.group(0)[:60]
    ratio = len(text) / max(1, len(src_plain))
    if len(src_plain) > 40 and (ratio < 0.25 or ratio > 4.0):
        return f"length ratio {ratio:.2f}"
    if LOOP_RE.search(text):
        return "repetition loop"
    return None


def _keep(hits: list[dict]) -> tuple[str, ...]:
    return tuple(e["en"] for e in hits if e.get("keep_english"))


def fix_glossary(out: str, hits: list[dict]) -> str:
    """If the model left a glossary name in English, swap in the agreed Thai."""
    for e in hits:
        if e.get("keep_english") or not e.get("th") or e["th"] in out:
            continue
        out = re.sub(r"(?<![A-Za-z])" + re.escape(e["en"]) + r"(?:[’']s)?(?![A-Za-z])", e["th"], out)
    return out


def translate_chunk(segs: list[SegIn], glossary_find, style_notes: str, stats) -> list[SegOut]:
    """Translate a chunk; never raises for content problems (only ServerUnavailable)."""
    source = "\n".join(f"[[{i + 1}]]{s.marked}[[/{i + 1}]]" for i, s in enumerate(segs))
    hits = glossary_find(" ".join(s.plain for s in segs))
    results: dict[int, SegOut] = {}
    retry: list[SegIn] = []
    try:
        reply = llm.chat(settings.translate_model, _prompt(source, _rules(hits, style_notes)),
                         max_tokens=_max_tokens(len(source)))
        stats(reply)
        parts = {int(n): t.strip() for n, t in P_RE.findall(reply.text)}
        if reply.finish_reason == "length":
            parts = {}       # cut off: trust none of it
        for i, s in enumerate(segs):
            out = parts.get(i + 1)
            if out is None:
                retry.append(s)
                continue
            out = fix_glossary(_clean(out), hits)
            if qa(s.plain, out, _keep(hits)) or check_tags(s.marked, out):
                retry.append(s)
            else:
                results[s.id] = SegOut(s.id, out, "done")
    except llm.ContextTooLong:
        retry = list(segs)

    for s in retry:
        results[s.id] = translate_one(s, glossary_find, style_notes, stats)
    return [results[s.id] for s in segs]


def translate_one(s: SegIn, glossary_find, style_notes: str, stats) -> SegOut:
    hits = glossary_find(s.plain)
    has_marks = bool(TAG_RE.search(s.marked))
    rules = _rules(hits, style_notes, chunk=False, marks=has_marks)
    simplified, last_out, last_problem = None, "", "no output"
    for temperature in (0.2, 0.5):
        reply = llm.chat(settings.translate_model, _prompt(s.marked, rules),
                         max_tokens=_max_tokens(len(s.marked)), temperature=temperature)
        stats(reply)
        out = fix_glossary(_clean(reply.text), hits)
        problem = qa(s.plain, out, _keep(hits))
        if problem:
            last_out, last_problem = out, problem
            continue
        if not check_tags(s.marked, out):
            return SegOut(s.id, out, "done")
        simplified = simplified or _simplified(s.marked, out)
    if simplified is not None:
        return SegOut(s.id, simplified, "done", SIMPLIFIED)   # right words, some italics/links lost

    if has_marks:
        # the markers themselves may confuse it: translate the plain text
        reply = llm.chat(settings.translate_model, _prompt(s.plain, _rules(hits, style_notes, chunk=False, marks=False)),
                         max_tokens=_max_tokens(len(s.plain)))
        stats(reply)
        out = fix_glossary(_clean(TAG_RE.sub("", reply.text)), hits)
        if not qa(s.plain, out, _keep(hits)):
            voids = "".join(m.group(0) for m in TAG_RE.finditer(s.marked) if m.group(3))
            return SegOut(s.id, voids + out, "done", SIMPLIFIED)
    return SegOut(s.id, _simplified(s.marked, last_out), "flagged", last_problem)


def make_chunks(segs: list[tuple[int, str, str, str]], limit: int) -> list[list[SegIn]]:
    """segs: (id, doc, marked, plain) in reading order -> chunks inside one document."""
    chunks, cur, cur_doc, size = [], [], None, 0
    for sid, doc, marked, plain in segs:
        n = len(marked)
        if cur and (doc != cur_doc or size + n > limit):
            chunks.append(cur)
            cur, size = [], 0
        cur.append(SegIn(sid, marked, plain))
        cur_doc = doc
        size += n
    if cur:
        chunks.append(cur)
    return chunks
