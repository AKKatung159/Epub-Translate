"""Chunk parsing, retries and quality checks, with a fake model instead of the Ally."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import llm, translate  # noqa: E402
from app.glossary import matcher  # noqa: E402
from app.translate import SegIn, qa, translate_chunk  # noqa: E402

LONG = "Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do."
THAI_OK = "อลิซเริ่มรู้สึกเบื่อหน่ายกับการนั่งข้างพี่สาวบนฝั่งแม่น้ำ และไม่มีอะไรให้ทำเลยสักอย่าง"


def fake(replies):
    calls = []

    def chat(model, messages, **kw):
        calls.append(messages[0]["content"])
        text = replies.pop(0) if replies else ""
        return llm.Reply(text=text, prompt_tokens=0, completion_tokens=0, finish_reason="stop", gen_tps=25.0)
    return chat, calls


def run(monkeypatch, segs, replies, glossary=()):
    chat, calls = fake(list(replies))
    monkeypatch.setattr(llm, "chat", chat)
    return translate_chunk(segs, matcher(list(glossary)), "", lambda r: None), calls


def test_chunk_ok(monkeypatch):
    segs = [SegIn(1, LONG, LONG), SegIn(2, "He said [1]no[/1].", "He said no.")]
    out, calls = run(monkeypatch, segs, [f"[[1]]{THAI_OK}[[/1]]\n[[2]]เขาพูดว่า [1]ไม่[/1][[/2]]"])
    assert [o.status for o in out] == ["done", "done"] and len(calls) == 1
    assert out[1].marked == "เขาพูดว่า [1]ไม่[/1]"


def test_dropped_italics_keep_words(monkeypatch):
    seg = SegIn(1, LONG.replace("very", "[1]very[/1]"), LONG)
    # chunk drops the marker, both single retries drop it too -> words kept, noted, not flagged
    out, calls = run(monkeypatch, [seg], [f"[[1]]{THAI_OK}[[/1]]", THAI_OK, THAI_OK])
    assert out[0].status == "done" and out[0].note == translate.SIMPLIFIED
    assert "[[" not in calls[1] and "[[N]]" not in calls[1]      # single retry is not told about [[N]] blocks


def test_junk_and_thai_digits_cleaned(monkeypatch):
    out, _ = run(monkeypatch, [SegIn(1, "CHAPTER 1. " + LONG, "CHAPTER 1. " + LONG)],
                 [f"[[1]]บทที่ ๑ {THAI_OK}\n[[1]]...[[/1]]"])
    assert out[0].status == "done"
    assert "บทที่ 1" in out[0].marked and "[[" not in out[0].marked


def test_english_left_is_flagged(monkeypatch):
    bad = "อลิซเริ่มรู้สึก tired of sitting by her sister on the bank ไม่มีอะไรทำ"
    out, _ = run(monkeypatch, [SegIn(1, LONG, LONG)], [f"[[1]]{bad}[[/1]]", bad, bad])
    assert out[0].status == "flagged" and "English" in out[0].note


def test_glossary_rule_and_fixup(monkeypatch):
    g = [{"en": "Alice", "th": "อลิซ", "type": "person", "keep_english": False}]
    out, calls = run(monkeypatch, [SegIn(1, LONG, LONG)],
                     [f"[[1]]Alice เริ่มรู้สึกเบื่อหน่ายกับการนั่งข้างพี่สาวบนฝั่งแม่น้ำ และไม่มีอะไรให้ทำเลย[[/1]]"], g)
    assert 'Translate "Alice" as "อลิซ".' in calls[0]
    assert out[0].marked.startswith("อลิซ") and out[0].status == "done"


def test_qa_rules():
    assert qa(LONG, THAI_OK) is None
    assert qa(LONG, "") == "empty"
    assert qa(LONG, LONG) == "not translated"
    assert "loop" in qa(LONG, "ซ้ำๆๆ " + "ก็แล้วกันนะ" * 8)
