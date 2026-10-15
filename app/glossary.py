"""Find the book's proper nouns and special terms, and propose one Thai rendering for each.

1. Heuristics pick capitalised words/phrases that are not just sentence starts (names, places, titles,
   invented terms), with a short example sentence for each.
2. The instruct model (Typhoon 2.1) classifies each candidate; common words it marks "not a term" are dropped.
3. The translation model (Typhoon Translate) gives one Thai form per term, in lists (a list gives context;
   one term alone often comes back unchanged). Tested: Gryphon -> กริฟฟอน, Mock Turtle -> เต่าจำแลง.
4. Multi-word names are rebuilt from their parts, so "Maia" and "Maia Drazhar" always agree.
The user reviews/edits the table before the translation starts; the translator then gets, for every
chunk, the entries that occur in it as hard rules.
"""
from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass

from . import llm
from .config import settings

log = logging.getLogger("glossary")

TYPES = ["person", "place", "organization", "title", "term"]

STOP = set("""
a an and are as at be but by for from had has have he her his i if in into is it its me my no not of on or our
she so than that the their them then there these they this those to us was we were what when where which who
why will with would you your yes oh ah well now here how all any each every some one two three first last
mr mrs ms miss sir madam lord lady dr st chapter part book prologue epilogue contents index notes note
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august
september october november december god o after before again also because both do does did down even ever
just like many more most much must never nothing only other out over perhaps same should since still such
through too under until up very what whatever while yet indeed shall let thank thanks please whom whose
""".split())

WORD = r"[A-Z][a-zA-ZÀ-ɏ'’\-]*[a-zA-ZÀ-ɏ]"
CAND_RE = re.compile(rf"\b{WORD}(?:\s+(?:of|the|de|du|la|le|van|von|al|el)?\s*{WORD})*")
SENT_START = re.compile(r"(^|[.!?…:;\"“”'‘’—–\-\(]\s*)$")
THAI = re.compile(r"[฀-๿]")


@dataclass
class Candidate:
    en: str
    count: int
    example: str


def candidates(texts: list[str], limit: int) -> list[Candidate]:
    caps: Counter = Counter()
    mid: Counter = Counter()          # occurrences not at a sentence start
    example: dict[str, str] = {}
    lower_words: Counter = Counter()
    for t in texts:
        for w in re.findall(r"\b[a-z][a-z'’\-]+\b", t):
            lower_words[w] += 1
        for m in CAND_RE.finditer(t):
            phrase = re.sub(r"[’']s$", "", m.group(0)).strip()
            if not phrase:
                continue
            words = phrase.split()
            stripped = False
            while words and words[0].lower() in STOP:     # "The Emperor" -> "Emperor"
                words = words[1:]
                stripped = True
            if not words:
                continue
            phrase = " ".join(words)
            caps[phrase] += 1
            # "Then Maia ..." at a sentence start: "Maia" itself is not the first word, so it counts as mid-sentence
            if stripped or not SENT_START.search(t[: m.start()][-3:] if m.start() else ""):
                mid[phrase] += 1
            if phrase not in example and 20 < len(t) < 400:
                example[phrase] = t
    out = []
    for phrase, n in caps.items():
        low = phrase.lower()
        if low in STOP or len(phrase) < 2 or mid[phrase] == 0:
            continue
        if re.search(r"[’'](?:d|ll|ve|m|re|s|t)$", phrase, re.I):
            continue                  # I'd, Let's, Don't ... are contractions, not names
        words = phrase.split()
        if len(words) > 5 or (len(words) >= 3 and phrase.isupper()):
            continue                  # shouted sentences / licence boilerplate, not names
        if " " not in phrase and lower_words[low] > 3 * mid[phrase]:
            continue                  # a normal word that is sometimes capitalised
        if n < 2:
            continue                  # once only: often two capitalised words meeting by chance ("Nile On" in a poem)
        if re.search(r"[’']s\b", phrase):
            continue                  # "Alice's Adventures": a possessive phrase, not one name
        out.append(Candidate(phrase, n, _trim(example.get(phrase, ""), phrase)))
    out.sort(key=lambda c: -c.count)
    return out[:limit]


def _trim(sentence: str, phrase: str, width: int = 160) -> str:
    i = sentence.find(phrase)
    if i < 0 or len(sentence) <= width:
        return sentence[:width]
    a = max(0, i - width // 2)
    return ("…" if a else "") + sentence[a:a + width] + "…"


# ---------------------------------------------------------------------------------------------- step 2: classify

SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "en": {"type": "string"},
            "type": {"type": "string", "enum": TYPES + ["not_a_term"]},
        },
        "required": ["en", "type"],
    },
}

SYSTEM = """คุณเป็นบรรณาธิการแปลหนังสือ อังกฤษ→ไทย งานนี้คือคัดคำสำหรับอภิธานศัพท์ (glossary) ก่อนแปลทั้งเล่ม
อ่านประโยคตัวอย่างของแต่ละคำ แล้วระบุ type:
- person: ชื่อคน ตัวละคร (รวมสัตว์หรือสิ่งของที่ใช้เป็นชื่อตัวละคร เช่น the White Rabbit)
- place: สถานที่ เมือง ประเทศ อาคาร
- organization: องค์กร ตระกูล กลุ่ม
- title: ยศ ตำแหน่ง คำเรียกขาน (King, Emperor, Lord Chancellor, Your Grace)
- term: ศัพท์เฉพาะ ศัพท์ที่ผู้เขียนคิดขึ้น สิ่งมีชีวิตในจินตนาการ ศัพท์วิชาการ
- not_a_term: คำธรรมดาที่แค่ขึ้นต้นประโยคหรือเน้น คำย่อแบบ I'd / Let's ตัวเลข วัน เดือน หัวข้อเอกสาร
ตอบเป็น JSON array เท่านั้น ลำดับเดียวกับที่ให้ไป"""


def _classify(user: str, n: int) -> list:
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    for attempt in range(3):
        try:
            data = llm.chat_json(settings.glossary_model, msgs, SCHEMA, max_tokens=60 * n + 200)
            if isinstance(data, list):
                return data
        except (ValueError, KeyError) as e:
            log.warning("glossary batch: bad JSON (%s), retry %d", e, attempt + 1)
    return []


# ---------------------------------------------------------------------------------------------- step 3: render

RENDER_RULES = [
    "These are names and terms from one book, for its glossary. Give the single Thai form a Thai publisher would print: "
    "one form only, no alternatives, no brackets, no explanations.",
    "Invented names of people, places and creatures: transliterate by sound following the Royal Institute "
    "(ราชบัณฑิตยสถาน) transliteration rules. Names with an established Thai form (London, Christmas) use that form.",
    "Ordinary words used as names, jobs, ranks and titles (animals, Footman, Duchess, Emperor): translate the meaning.",
    "Technical terms: use the Royal Institute coined term; transliterate when there is none.",
    "Keep every [[N]]...[[/N]] block, one term per block, in the same order.",
]

P_RE = re.compile(r"\[\[(\d+)\]\](.*?)\[\[/\1\]\]", re.S)
LINE_JUNK = re.compile(r"^\s*(?:\[\[/?\d+\]\]|\d+[.)])\s*|\s*\[\[/\d+\]\]\s*$")


def _render(terms: list[str]) -> dict[str, str]:
    """Thai forms for a list of terms; if the answer cannot be lined up with the input, split and retry."""
    if not terms:
        return {}
    source = "\n".join(f"[[{i + 1}]]{t}[[/{i + 1}]]" for i, t in enumerate(terms))
    rules = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(RENDER_RULES))
    msgs = [{"role": "user", "content": "Translate the following English text into Thai, strictly following the rules "
             f"below and return only the translated text.\nRules:\n{rules}\nSource Text:\n{source}"}]
    reply = llm.chat(settings.translate_model, msgs, max_tokens=40 * len(terms) + 200, temperature=0.1)
    got = {int(n): t.strip() for n, t in P_RE.findall(reply.text)}
    if len(got) < len(terms):                               # markers dropped: try one term per line
        lines = [LINE_JUNK.sub("", ln).strip() for ln in reply.text.splitlines()]
        lines = [ln for ln in lines if ln]
        if len(lines) == len(terms):
            got = {i + 1: ln for i, ln in enumerate(lines)}
    out = {t: got[i + 1] for i, t in enumerate(terms) if got.get(i + 1) and THAI.search(got[i + 1])}
    missing = [t for t in terms if t not in out]
    if not out and len(terms) > 1:                          # nothing lined up: halve the list
        half = len(terms) // 2
        return {**_render(terms[:half]), **_render(terms[half:])}
    if 1 < len(missing) < len(terms):                       # some came back untranslated: once more, together
        out.update(_render(missing))
    return out


def clean_th(th: str, en: str) -> tuple[str, bool]:
    """One Thai form: first option only, no brackets/notes. keep_english when there is no Thai left."""
    th = re.split(r"\s*(?:/|,|;|\bหรือ\b)\s*", th.strip())[0]
    th = re.sub(r"\s*[\(\[（].*?[\)\]）]", "", th).strip(" .\"'“”")
    if not th:
        return en, True
    return th, not THAI.search(th)


def propose(cands: list[Candidate], book_hint: str, on_progress=None, should_stop=None) -> list[dict]:
    total = len(cands) * 2                                  # classify + render, for the progress bar
    kept: list[tuple[Candidate, dict]] = []
    step = 12
    for i in range(0, len(cands), step):
        if should_stop and should_stop():
            return []
        part = cands[i:i + step]
        lines = "\n".join(f'{j + 1}. "{c.en}" — ตัวอย่าง: {c.example}' for j, c in enumerate(part))
        user = (f"หนังสือ: {book_hint}\n\nคำ ({len(part)} คำ):\n{lines}\n\n"
                "ตอบ JSON array ของ object {en, type} ครบทุกคำ")
        by_en = {g.get("en", "").strip(): g for g in _classify(user, len(part)) if isinstance(g, dict)}
        for c in part:
            g = by_en.get(c.en) or {"type": "term"}
            if g.get("type") != "not_a_term":
                kept.append((c, g))
        if on_progress:
            on_progress(min(i + step, len(cands)), total)

    # the 4B instruct model ticks "keep English" far too often, so every term goes to the translation model;
    # a term stays English only when that model leaves it in English (brands, code names)
    todo = [c.en for c, _ in kept]
    thai: dict[str, str] = {}
    step = 15
    for i in range(0, len(todo), step):
        if should_stop and should_stop():
            return []
        thai.update(_render(todo[i:i + step]))
        if on_progress:
            on_progress(len(cands) + min(i + step, len(todo)) * len(cands) // max(1, len(todo)), total)

    results: list[dict] = []
    for c, g in kept:
        t = g.get("type") if g.get("type") in TYPES else "term"
        th, keep = clean_th(thai.get(c.en, ""), c.en)
        src = "model" if c.en in thai else "fallback"
        results.append({"en": c.en, "type": t, "count": c.count, "example": c.example,
                        "th": th, "keep_english": keep, "source": src})
    return harmonize(results)


def harmonize(entries: list[dict]) -> list[dict]:
    """Make multi-word person/place names use the same Thai as their single-word parts."""
    single = {e["en"]: e for e in entries if " " not in e["en"]}
    for e in entries:
        words = e["en"].split()
        if e.get("source") in ("user", "series"):
            continue                  # a form someone already chose stays as it is
        if len(words) > 1 and e["type"] in ("person", "place") and all(w in single for w in words):
            parts = [single[w] for w in words]
            if all(p["type"] in ("person", "place") and not p["keep_english"] for p in parts):
                e["th"] = " ".join(p["th"] for p in parts)
                e["source"] = "joined"
    return entries


# ---------------------------------------------------------------------------------------------- use in chunks

def matcher(entries: list[dict]):
    """Return f(text) -> entries that occur in text (longest first, whole words, with possessive).

    Names match case-sensitively ("Bill" the lizard, not "the bill"); entries the user writes in lower case
    ("sister", "fan", "ma'am") are ordinary words and match in any case.
    """
    entries = sorted([e for e in entries if e.get("en")], key=lambda e: -len(e["en"]))
    if not entries:
        return lambda text: []
    alts = [(re.escape(e["en"]) if e["en"] != e["en"].lower() else "(?i:" + re.escape(e["en"]) + ")") for e in entries]
    pattern = re.compile(r"(?<![A-Za-z])(" + "|".join(alts) + r")(?![A-Za-z])")
    by_key = {(e["en"] if e["en"] != e["en"].lower() else e["en"].lower()): e for e in entries}

    def find(text: str) -> list[dict]:
        seen, out = set(), []
        for m in pattern.finditer(text):
            e = by_key.get(m.group(1)) or by_key.get(m.group(1).lower())
            if e and e["en"] not in seen:
                seen.add(e["en"])
                out.append(e)
        return out
    return find


def group_by_type(entries: list[dict]) -> dict[str, list[dict]]:
    g = defaultdict(list)
    for e in entries:
        g[e["type"]].append(e)
    return g


# ---------------------------------------------------------------------------------------------- series

SERIES_KEEP = ("type", "th", "keep_english", "gender", "role", "particle", "self_pronoun", "addressed_as")


def merge_series(entries: list[dict], prev: list[dict], texts: list[str]) -> list[dict]:
    """Book 2 of a series: names the earlier book already has keep its Thai and character sheet; its other
    entries are added when they occur in this book. Nothing is added that this book never mentions."""
    by_en = {e["en"]: e for e in entries}
    for p in prev:
        e = by_en.get(p["en"])
        if e and e.get("source") != "user":
            e.update({k: p.get(k, "") for k in SERIES_KEEP}, source="series")
    missing = [p for p in prev if p.get("en") and p["en"] not in by_en]
    if missing:
        find = matcher(missing)
        count: Counter = Counter()
        example: dict[str, str] = {}
        for t in texts:
            for e in find(t):
                count[e["en"]] += 1
                example.setdefault(e["en"], _trim(t, e["en"]))
        for p in missing:
            if count[p["en"]]:
                entries.append({**{k: p.get(k, "") for k in SERIES_KEEP}, "en": p["en"], "count": count[p["en"]],
                                "example": example[p["en"]], "source": "series"})
    return harmonize(entries)
