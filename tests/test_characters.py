"""Character sheet: gender from pronouns, rules for the translator, particle check, lower-case terms."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm  # noqa: E402
from app.characters import character_rule, gender_mismatch, genders  # noqa: E402
from app.glossary import matcher  # noqa: E402
from app.translate import SegIn, translate_chunk  # noqa: E402

TEXTS = [
    "Alice was beginning to get very tired of sitting by her sister. She looked at her book.",
    "Then Alice said to herself that she would go. Her feet were far away.",
    "The Rabbit took out his watch and looked at it. He hurried on.",
    "The Rabbit dropped his gloves.",
    "Bill climbed the chimney and he fell. His friends caught him.",
    "Bill sneezed and he laughed.", "Bill said he was fine.", "The Rabbit said he was late.",
    "Alice smiled; she was happy.",
    "Alice and the Rabbit ran. They were late.",           # two names: ignored
    "Queen Margaret spoke.",
]


def test_genders_from_pronouns_and_titles():
    g = genders(["Alice", "Rabbit", "Bill", "Queen Margaret", "Nobody"], TEXTS)
    assert g["Alice"][0] == "female"
    assert g["Rabbit"][0] == "male" and g["Bill"][0] == "male"
    assert g["Queen Margaret"][0] == "female"      # title word
    assert g["Nobody"][0] == ""


def test_character_rule_text():
    e = {"type": "person", "en": "Alice", "th": "อลิซ", "gender": "female", "role": "เด็กหญิงตัวเอก",
         "particle": "ค่ะ/คะ", "self_pronoun": "ฉัน", "addressed_as": "เธอ"}
    r = character_rule(e)
    assert "Alice (อลิซ) is female, เด็กหญิงตัวเอก." in r
    assert 'end polite sentences with "ค่ะ/คะ"' in r and 'say "ฉัน" for I/me' in r and "Others address Alice with: เธอ." in r
    assert character_rule({"type": "place", "en": "London", "th": "ลอนดอน"}) is None


def test_gender_mismatch():
    alice = [{"type": "person", "en": "Alice", "particle": "ค่ะ/คะ"}]
    assert gender_mismatch("“ขออนุญาตนะครับ” อลิซพูด", alice)
    assert gender_mismatch("“ขออนุญาตนะคะ” อลิซพูด", alice) is None
    assert gender_mismatch("อลิซเดินไปครับ", alice) is None                      # no dialogue: no check
    both = alice + [{"type": "person", "en": "Rabbit", "particle": "ครับ"}]
    assert gender_mismatch("“ครับ” “ค่ะ”", both) is None                         # two speakers: can't tell


def test_lowercase_terms_match_any_case_and_names_stay_case_sensitive():
    find = matcher([{"en": "sister", "th": "พี่สาว"}, {"en": "Bill", "th": "บิล"}])
    assert [e["en"] for e in find("Her Sister smiled.")] == ["sister"]
    assert find("He paid the bill.") == []
    assert [e["en"] for e in find("Bill came.")] == ["Bill"]


def test_unnamed_speaker_uses_the_chunk_cast(monkeypatch):
    calls, replies = [], ["[[1]]อลิซตกลงไปในหลุมกระต่ายที่ลึกมากและมืดมิดจนมองไม่เห็นอะไรเลยสักอย่าง[[/1]]\n"
                          "[[2]]“คุณแม่ครับ ที่นี่คือนิวซีแลนด์หรือออสเตรเลีย” เธอถามขณะร่วงลงไปเรื่อยๆ[[/2]]",
                          "“คุณผู้หญิงคะ ที่นี่คือนิวซีแลนด์หรือออสเตรเลีย” เธอถามขณะร่วงลงไปเรื่อยๆ"]

    def chat(model, messages, **kw):
        calls.append(messages[0]["content"])
        return llm.Reply(replies.pop(0), 0, 0, "stop", 25.0)
    monkeypatch.setattr(llm, "chat", chat)
    g = [{"type": "person", "en": "Alice", "th": "อลิซ", "gender": "female", "particle": "ค่ะ/คะ", "self_pronoun": "ฉัน",
          "role": "", "addressed_as": "", "keep_english": 0}]
    a = "Alice fell down the rabbit-hole, which was very deep and so dark that she could see nothing at all."
    b = "“Please, Ma’am, is this New Zealand or Australia?” she asked as she kept falling down and down."
    out = translate_chunk([SegIn(1, a, a), SegIn(2, b, b)], matcher(g), "", lambda r: None)
    assert out[1].status == "done" and "คะ" in out[1].marked and "ครับ" not in out[1].marked
    assert "Alice (อลิซ) is female." in calls[1]          # the single retry still knows who is speaking


def test_character_rules_reach_the_prompt_and_wrong_particle_is_retried(monkeypatch):
    calls, replies = [], ["[[1]]“ขออนุญาตนะครับ” อลิซพูดด้วยเสียงเบาๆ และขี้อายกับกระต่ายขาวที่วิ่งผ่านไป[[/1]]",
                          "“ขออนุญาตนะคะ” อลิซพูดด้วยเสียงเบาๆ และขี้อายกับกระต่ายขาวที่วิ่งผ่านไป"]

    def chat(model, messages, **kw):
        calls.append(messages[0]["content"])
        return llm.Reply(replies.pop(0), 0, 0, "stop", 25.0)
    monkeypatch.setattr(llm, "chat", chat)
    g = [{"type": "person", "en": "Alice", "th": "อลิซ", "gender": "female", "particle": "ค่ะ/คะ", "self_pronoun": "ฉัน",
          "role": "", "addressed_as": "", "keep_english": 0}]
    src = "“If you please, sir,” said Alice in a low, timid voice to the White Rabbit running by."
    out = translate_chunk([SegIn(1, src, src)], matcher(g), "", lambda r: None)
    assert "Alice (อลิซ) is female." in calls[0]
    assert out[0].status == "done" and "คะ" in out[0].marked and len(calls) == 2
