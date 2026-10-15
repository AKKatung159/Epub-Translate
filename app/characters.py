"""Character sheet and book profile, built during the glossary step so every chunk knows who is who.

- Gender: counted from the pronouns (he/him/his vs she/her) in the sentences that name the character, plus
  title words (Mr, Lord, King / Mrs, Lady, Queen). No model needed, and it is right far more often than a
  4B model guessing.
- Speech: the instruct model reads a few lines of the character's dialogue and proposes the Thai ending
  particle (ค่ะ/คะ, ครับ, none), how they say "I" (ฉัน, ผม, ข้า, กระหม่อม ...), their role, and how others
  address them (ราชาศัพท์, ท่าน, คุณ ...).
- Book profile: genre, setting, register and narrator, drafted from the opening chapters.
All of it is shown in the glossary step and can be edited before translating; the translator then gets one
short rule per character that appears in a chunk, and the profile as rules for the whole book.
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from . import llm
from .config import settings

log = logging.getLogger("characters")

HE = re.compile(r"\b(he|him|his|himself)\b", re.I)
SHE = re.compile(r"\b(she|her|hers|herself)\b", re.I)
PRONOUN = re.compile(r"\b(he|him|his|himself|she|her|hers|herself)\b", re.I)
SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+")
MALE_TITLES = {"mr", "lord", "king", "emperor", "prince", "duke", "sir", "father", "brother", "uncle", "baron", "count"}
FEMALE_TITLES = {"mrs", "miss", "ms", "lady", "queen", "empress", "princess", "duchess", "mother", "sister", "aunt",
                 "baroness", "countess", "madam", "dame"}
PARTICLES = ["ค่ะ/คะ", "ครับ", "none"]
# The model may only pick from these (it reached for กู far too often); the user can type anything in the table.
SELF_PRONOUNS = ["ฉัน", "ผม", "ข้า", "เรา", "หนู", "ข้าพเจ้า", "กระหม่อม", "หม่อมฉัน", "ข้าน้อย"]
ADDRESS = ["คุณ", "เธอ", "ท่าน", "เจ้า", "ราชาศัพท์", "นาย", "-"]
FORBIDDEN_RULE = re.compile(r"explan|footnote|illustrat|note|อธิบาย|เชิงอรรถ|หมายเหตุ|ภาพประกอบ", re.I)


def genders(names: list[str], texts: list[str]) -> dict[str, tuple[str, int, int]]:
    """name -> (male|female|'', he_count, she_count), from pronouns near the name."""
    pats = {n: re.compile(r"(?<![A-Za-z])" + re.escape(n) + r"(?![A-Za-z])") for n in names}
    he, she = Counter(), Counter()
    for t in texts:
        sents = SENT_SPLIT.split(t)
        for i, s in enumerate(sents):
            present = [n for n, p in pats.items() if p.search(s)]
            if len(present) != 1:
                continue                                   # two names in one sentence: pronouns are ambiguous
            n = present[0]
            window = s[pats[n].search(s).end():] + " " + (sents[i + 1] if i + 1 < len(sents) else "")
            # only the first gendered pronoun after the name: later ones often point at someone else
            m = PRONOUN.search(window)
            if m:
                if HE.fullmatch(m.group(0)):
                    he[n] += 1
                else:
                    she[n] += 1
    out = {}
    for n in names:
        first = n.split()[0].lower().rstrip(".")
        h, s = he[n], she[n]
        if first in MALE_TITLES:
            g = "male"
        elif first in FEMALE_TITLES:
            g = "female"
        elif h + s >= 3 and h >= 2 * s:
            g = "male"
        elif h + s >= 3 and s >= 2 * h:
            g = "female"
        else:
            g = ""
        out[n] = (g, h, s)
    return out


def dialogue_examples(name: str, texts: list[str], k: int = 3) -> list[str]:
    pat = re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])")
    out = []
    for t in texts:
        if ("“" in t or '"' in t) and pat.search(t):
            out.append(t[:260] + ("…" if len(t) > 260 else ""))
            if len(out) >= k:
                break
    return out


SPEECH_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "en": {"type": "string"},
            "role": {"type": "string"},
            "particle": {"type": "string", "enum": PARTICLES},
            "self_pronoun": {"type": "string", "enum": SELF_PRONOUNS},
            "addressed_as": {"type": "string", "enum": ADDRESS},
        },
        "required": ["en", "role", "particle", "self_pronoun", "addressed_as"],
    },
}

SPEECH_SYSTEM = """คุณเป็นบรรณาธิการแปลนิยาย อังกฤษ→ไทย งานนี้คือทำข้อมูลตัวละครก่อนแปล
สำหรับตัวละครแต่ละตัว อ่านเพศ และตัวอย่างบทพูด แล้วตอบ:
- role: บทบาท/ฐานะสั้นๆ เป็นภาษาไทย (เช่น เด็กหญิงตัวเอก, จักรพรรดิ, คนรับใช้, กระต่ายผู้รีบร้อน)
- particle: คำลงท้ายเมื่อตัวละครนี้พูด: "ค่ะ/คะ" (หญิงสุภาพ), "ครับ" (ชายสุภาพ), "none" (ไม่ใช้คำลงท้าย เช่น เด็กพูดกับตัวเอง ผู้มีอำนาจ คนหยาบ)
- self_pronoun: คำที่ตัวละครใช้แทนตัวเอง คำเดียว (ฉัน, ผม, ข้า, กระหม่อม, หม่อมฉัน, เรา, กู)
- addressed_as: คนอื่นพูดกับตัวละครนี้อย่างไร สั้นๆ (เช่น "ราชาศัพท์", "ท่าน", "เธอ", "คุณ", "เจ้า")
ใช้ให้เข้ากับยุคสมัยและฐานะของเรื่อง ตอบเป็น JSON array เท่านั้น ลำดับเดียวกับที่ให้ไป"""


def speech_profiles(people: list[dict], texts: list[str], book_hint: str, profile: str,
                    on_progress=None, should_stop=None) -> dict[str, dict]:
    out: dict[str, dict] = {}
    step = 6
    for i in range(0, len(people), step):
        if should_stop and should_stop():
            break
        part = people[i:i + step]
        blocks = []
        for j, p in enumerate(part):
            ex = dialogue_examples(p["en"], texts) or [p.get("example", "")]
            g = {"male": "ชาย", "female": "หญิง"}.get(p.get("gender", ""), "ไม่ทราบ")
            blocks.append(f'{j + 1}. "{p["en"]}" ({p.get("th", "")}) เพศ: {g}\n   ' + "\n   ".join(e for e in ex if e))
        user = (f"หนังสือ: {book_hint}\n{profile}\n\nตัวละคร ({len(part)} ตัว):\n" + "\n".join(blocks) +
                "\n\nตอบ JSON array ของ object {en, role, particle, self_pronoun, addressed_as} ครบทุกตัว")
        msgs = [{"role": "system", "content": SPEECH_SYSTEM}, {"role": "user", "content": user}]
        try:
            got = llm.chat_json(settings.glossary_model, msgs, SPEECH_SCHEMA, max_tokens=120 * len(part) + 200)
        except (ValueError, KeyError) as e:
            log.warning("speech batch: bad JSON (%s)", e)
            got = []
        for g in got if isinstance(got, list) else []:
            if isinstance(g, dict) and g.get("en"):
                out[g["en"].strip()] = g
        if on_progress:
            on_progress(min(i + step, len(people)), len(people))
    return out


def default_particle(gender: str) -> str:
    return {"female": "ค่ะ/คะ", "male": "ครับ"}.get(gender, "")


PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {"type": "string"},
        "setting": {"type": "string"},
        "register": {"type": "string"},
        "narrator": {"type": "string"},
        "rules": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["genre", "setting", "register", "narrator", "rules"],
}

PROFILE_SYSTEM = """คุณเป็นบรรณาธิการแปลหนังสือ อังกฤษ→ไทย อ่านตอนต้นของหนังสือแล้วสรุปแนวทางการแปลทั้งเล่ม เป็นภาษาไทยสั้นๆ:
- genre: แนวเรื่อง
- setting: ยุคสมัย สถานที่ บรรยากาศ
- register: ระดับภาษาที่ควรใช้ (เช่น ภาษาวรรณกรรมทางการ, ภาษาพูดร่วมสมัย, ราชาศัพท์กับเชื้อพระวงศ์)
- narrator: มุมมองผู้เล่า (บุรุษที่ 1 หรือ 3) และน้ำเสียง
- rules: กฎการแปลที่เฉพาะกับเล่มนี้ 3-6 ข้อ เกี่ยวกับการเลือกคำภาษาไทยเท่านั้น (เช่น สรรพนามที่ใช้ การเรียกยศ
  คำที่ห้ามแปลตรงตัว ระดับความสุภาพ) ห้ามมีกฎให้เพิ่มคำอธิบาย เชิงอรรถ หรือข้อความที่ไม่มีในต้นฉบับ
เขียนทุกช่องเป็นภาษาไทย ตอบเป็น JSON object เท่านั้น"""


def book_profile(texts: list[str], people: list[dict], book_hint: str) -> str:
    sample, size = [], 0
    for t in texts:
        if len(t) < 40:
            continue                                   # skip headings, licence lines, ornaments
        sample.append(t)
        size += len(t)
        if size > 6000:
            break
    cast = ", ".join(f'{p["en"]} ({ {"male": "ชาย", "female": "หญิง"}.get(p.get("gender", ""), "?") })' for p in people[:12])
    user = f"หนังสือ: {book_hint}\nตัวละครหลัก: {cast}\n\nตอนต้นของหนังสือ:\n" + "\n".join(sample)
    msgs = [{"role": "system", "content": PROFILE_SYSTEM}, {"role": "user", "content": user}]
    try:
        p = llm.chat_json(settings.glossary_model, msgs, PROFILE_SCHEMA, max_tokens=1800)
    except (ValueError, KeyError) as e:
        log.warning("book profile: bad JSON (%s)", e)
        return ""
    if not isinstance(p, dict):
        return ""
    lines = [f"แนวเรื่อง: {p.get('genre', '')}", f"ฉาก: {p.get('setting', '')}",
             f"ระดับภาษา: {p.get('register', '')}", f"ผู้เล่า: {p.get('narrator', '')}"]
    # a rule asking for explanations/notes would make the translator add text that is not in the book
    lines += [f"- {r}" for r in p.get("rules", []) if isinstance(r, str) and r.strip() and not FORBIDDEN_RULE.search(r)]
    return "\n".join(ln for ln in lines if ln.split(":", 1)[-1].strip())


def character_rule(e: dict) -> str | None:
    """One translation rule for a character who appears in the chunk."""
    if e.get("type") != "person":
        return None
    name = f'{e["en"]} ({e["th"]})' if e.get("th") and e["th"] != e["en"] else e["en"]
    parts = []
    g = {"male": "male", "female": "female"}.get(e.get("gender", ""))
    if g or e.get("role"):
        parts.append(f"{name} is " + ", ".join(x for x in (g, e.get("role", "")) if x) + ".")
    speech = []
    if e.get("particle") and e["particle"] != "none":
        speech.append(f'end polite sentences with "{e["particle"]}"')
    elif e.get("particle") == "none":
        speech.append("use no polite ending particle")
    if e.get("self_pronoun"):
        speech.append(f'say "{e["self_pronoun"]}" for I/me')
    if speech:
        parts.append(f"When {e['en']} speaks, " + " and ".join(speech) + ".")
    if e.get("addressed_as"):
        parts.append(f"Others address {e['en']} with: {e['addressed_as']}.")
    return " ".join(parts) or None


def gender_mismatch(text_th: str, hits: list[dict]) -> str | None:
    """Only one character with a known particle appears and the Thai uses the other gender's particle."""
    people = [e for e in hits if e.get("type") == "person" and e.get("particle") in ("ค่ะ/คะ", "ครับ")]
    if len(people) != 1 or "“" not in text_th and '"' not in text_th:
        return None
    p = people[0]
    if p["particle"] == "ค่ะ/คะ" and re.search(r"ครับ", text_th):
        return f'{p["en"]} is female but the dialogue uses ครับ'
    if p["particle"] == "ครับ" and re.search(r"(?<![ก-๙])(ค่ะ|คะ)(?![ก-๙])", text_th):
        return f'{p["en"]} is male but the dialogue uses ค่ะ/คะ'
    return None
