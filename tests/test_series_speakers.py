"""Who is speaking (dialogue attribution) and carrying a glossary over to the next book of a series."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm  # noqa: E402
from app.characters import speakers  # noqa: E402
from app.glossary import matcher, merge_series  # noqa: E402
from app.translate import SegIn, translate_chunk  # noqa: E402

PEOPLE = [
    {"en": "Alice", "type": "person", "gender": "female", "th": "อลิซ", "particle": "ค่ะ/คะ"},
    {"en": "Rabbit", "type": "person", "gender": "male", "th": "กระต่าย", "particle": "ครับ"},
    {"en": "London", "type": "place", "th": "ลอนดอน"},
]


def test_named_speaker_before_and_after_the_verb():
    paras = ["“I’m late!” said the Rabbit.",
             "Alice asked, “Late for what?”",
             "“For the Queen,” the Rabbit replied, looking at Alice."]
    assert speakers(paras, PEOPLE) == ["Rabbit", "Alice", "Rabbit"]


def test_pronouns_are_not_guessed():
    paras = ["Alice walked to the door and the Rabbit followed.",
             "“Is anybody there?” she called.",
             "“I’m late,” he said, “and so is the Rabbit.”"]       # the name in the next quote is not the speaker
    assert speakers(paras, PEOPLE) == [None, None, None]


def test_quote_only_paragraphs_alternate_in_a_conversation():
    paras = ["“Who are you?” said the Rabbit.",
             "“I’m Alice,” Alice said.",
             "“Where do you live?”",
             "“Far away.”",
             "The Rabbit nodded.",           # narration ends the conversation
             "“Good.”"]
    assert speakers(paras, PEOPLE) == ["Rabbit", "Alice", "Rabbit", "Alice", None, None]


def test_unclear_stays_unknown():
    assert speakers(["“Hello,” said someone.", "No dialogue here, Alice thought."], PEOPLE) == [None, None]
    assert speakers(["“Hi,” said Alice."], []) == [None]


def test_speaker_reaches_the_prompt_and_the_particle_check(monkeypatch):
    seen = []

    def fake_chat(model, messages, **kw):
        seen.append(messages[0]["content"])
        return llm.Reply("[[1]]“ไม่ทราบครับ”[[/1]]", 10, 10, "stop", 10.0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    find = matcher(PEOPLE)
    out = translate_chunk([SegIn(1, "“I don’t know.”", "“I don’t know.”", PEOPLE[0])], find, "", lambda r: None)
    assert "spoken by Alice (อลิซ)" in seen[0]
    assert 'end polite sentences with "ค่ะ/คะ"' in seen[0]          # her character rule, though she is not named
    assert len(seen) > 1                                             # ครับ from Alice: retried
    assert out[0].note.startswith("ตรวจคำลงท้าย")


def test_series_keeps_earlier_names_and_adds_only_what_this_book_mentions():
    prev = [{"en": "Alice", "type": "person", "th": "แอลิซ", "gender": "female", "particle": "ค่ะ/คะ",
             "self_pronoun": "ฉัน", "keep_english": 0, "source": "user"},
            {"en": "Dinah", "type": "person", "th": "ไดนาห์", "gender": "female", "keep_english": 0},
            {"en": "Wonderland", "type": "place", "th": "แดนมหัศจรรย์", "keep_english": 0}]
    entries = [{"en": "Alice", "type": "person", "th": "อลิซ", "count": 9, "example": "", "source": "model",
                "keep_english": False},
               {"en": "Looking-Glass", "type": "place", "th": "กระจกเงา", "count": 3, "example": "", "source": "model",
                "keep_english": False}]
    texts = ["Alice went through the glass.", "She missed Dinah, her cat.", "Dinah would like it."]
    got = {e["en"]: e for e in merge_series(entries, prev, texts)}
    assert got["Alice"]["th"] == "แอลิซ" and got["Alice"]["source"] == "series"
    assert got["Alice"]["self_pronoun"] == "ฉัน" and got["Alice"]["count"] == 9
    assert got["Dinah"]["count"] == 2 and got["Dinah"]["source"] == "series"
    assert "Wonderland" not in got                                   # never mentioned in this book
    assert got["Looking-Glass"]["th"] == "กระจกเงา"


def test_series_never_overwrites_what_the_user_typed_in_this_book():
    prev = [{"en": "Alice", "type": "person", "th": "แอลิซ"}]
    entries = [{"en": "Alice", "type": "person", "th": "อาลิส", "count": 1, "source": "user"}]
    assert merge_series(entries, prev, ["Alice"])[0]["th"] == "อาลิส"


def test_only_the_word_next_to_the_verb_counts():
    people = PEOPLE + [{"en": "Setheris", "type": "person", "gender": "male"}]
    paras = ["The Rabbit looked at Alice.",
             "“But—!” said Telimezh, and subsided. Alice thought it was rude.",      # unknown speaker: no guess
             "“Yes?” he said; Setheris had made very sure of it.",                  # "he": not guessed
             "“Hurry,” The Rabbit said."]
    assert speakers(paras, people) == [None, None, None, "Rabbit"]


def test_discord_off_without_webhook_and_embed_with_link(monkeypatch):
    import dataclasses
    from app import notify
    sent = []
    monkeypatch.setattr(notify.httpx, "post", lambda url, json, timeout: sent.append((url, json)) or
                        type("R", (), {"status_code": 204, "text": ""})())
    monkeypatch.setattr(notify.threading, "Thread", lambda target, args, daemon: type("T", (), {"start": lambda self: target(*args)})())
    notify.send("x")
    assert sent == []
    monkeypatch.setattr(notify, "settings", dataclasses.replace(notify.settings, discord_webhook_url="https://hook",
                                                                public_url="http://epub.lan"))
    notify.send("แปลเสร็จแล้ว: Book", "ใช้เวลา 3 ชม.", "ok", "abc")
    url, body = sent[0]
    assert url == "https://hook" and body["embeds"][0]["url"] == "http://epub.lan/#abc"
    assert body["embeds"][0]["color"] == notify.COLORS["ok"]


def test_named_speaker_gets_no_extra_rule_but_is_still_checked(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "chat", lambda model, messages, **kw: seen.append(messages[0]["content"]) or
                        llm.Reply("[[1]]“ครับ” อลิซกล่าว[[/1]]", 1, 1, "stop", 1.0))
    out = translate_chunk([SegIn(1, "“Yes,” Alice said.", "“Yes,” Alice said.", PEOPLE[0])], matcher(PEOPLE), "",
                          lambda r: None)
    assert "spoken by" not in seen[0]
    assert out[0].note.startswith("ตรวจคำลงท้าย")
