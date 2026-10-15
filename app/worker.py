"""One background thread runs the jobs: glossary extraction, then translation, one job at a time
(the Ally serves one request at a time, so parallel requests would only queue there)."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from . import characters, glossary, llm
from .config import settings
from .store import GLOSSARY_COLS, store
from .translate import make_chunks, translate_chunk

log = logging.getLogger("worker")
wake = threading.Event()

ACTIVE = ("glossary", "queued", "translating", "waiting")


def start() -> None:
    for j in store.jobs():                       # resume after a restart
        if j["status"] in ("translating", "waiting"):
            store.set_status(j["id"], "queued", "resumed after restart")
    threading.Thread(target=_loop, name="worker", daemon=True).start()


def _loop() -> None:
    while True:
        job = next((j for j in reversed(store.jobs()) if j["status"] in ACTIVE), None)
        if not job:
            wake.wait(10)
            wake.clear()
            continue
        try:
            if job["status"] == "glossary":
                run_glossary(job["id"])
            else:
                run_translation(job["id"])
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            store.set_status(job["id"], "error", f"{type(e).__name__}: {e}")


def _status(jid: str) -> str:
    j = store.job(jid)
    return j["status"] if j else "deleted"


def _wait_for_server(jid: str) -> None:
    def on_wait(sec):
        store.set_status(jid, "waiting", f"ต่อเครื่องโมเดลไม่ได้ (Ally ปิดอยู่หรือหลับ?) รอมาแล้ว {sec // 60} นาที {sec % 60} วินาที")
    llm.wait_until_up(lambda: _status(jid) not in ACTIVE, on_wait)


def run_glossary(jid: str) -> None:
    job = store.job(jid)
    texts = [r["plain"] for r in store.q("SELECT plain FROM segments WHERE job_id=? AND kind='text'", (jid,))]
    cands = glossary.candidates(texts, settings.max_glossary_terms)
    store.set_message(jid, f"พบคำเฉพาะ {len(cands)} คำ กำลังเสนอคำแปล…")

    def progress(done, total):
        store.update_stats(jid, glossary_done=done, glossary_total=total)
        store.set_message(jid, f"กำลังทำ glossary {done}/{total} คำ")

    while True:
        try:
            entries = glossary.propose(cands, job["name"], progress, lambda: _status(jid) != "glossary")
            break
        except llm.ServerUnavailable:
            _wait_for_server(jid)
            if _status(jid) not in ACTIVE:
                return
            store.set_status(jid, "glossary", "ต่อเครื่องโมเดลได้แล้ว ทำ glossary ต่อ")
    if _status(jid) != "glossary":
        return

    # character sheet: gender from pronouns, then speech style / role from the model for the main characters
    people = sorted([e for e in entries if e["type"] == "person"], key=lambda e: -e["count"])
    for e in entries:
        for k in ("gender", "role", "particle", "self_pronoun", "addressed_as"):
            e.setdefault(k, "")
    for name, (g, _he, _she) in characters.genders([p["en"] for p in people], texts).items():
        next(p for p in people if p["en"] == name)["gender"] = g
    main_cast = [p for p in people if p["count"] >= 3][:30]
    while True:
        try:
            store.set_message(jid, "กำลังสรุปแนวเรื่องและระดับภาษาของหนังสือ…")
            profile = characters.book_profile(texts, main_cast, job["name"])

            def sp_progress(done, total):
                store.set_message(jid, f"กำลังทำข้อมูลตัวละคร {done}/{total} ตัว")
            speech = characters.speech_profiles(main_cast, texts, job["name"], profile, sp_progress,
                                                lambda: _status(jid) != "glossary")
            break
        except llm.ServerUnavailable:
            _wait_for_server(jid)
            if _status(jid) not in ACTIVE:
                return
            store.set_status(jid, "glossary", "ต่อเครื่องโมเดลได้แล้ว ทำข้อมูลตัวละครต่อ")
    if _status(jid) != "glossary":
        return
    for p in people:
        s = speech.get(p["en"], {})
        p["role"] = (s.get("role") or "").strip()[:60]
        p["particle"] = s.get("particle") if s.get("particle") in characters.PARTICLES else characters.default_particle(p["gender"])
        if (p["gender"], p["particle"]) in (("male", "ค่ะ/คะ"), ("female", "ครับ")):
            p["particle"] = characters.default_particle(p["gender"])   # pronoun count beats the model's guess
        p["self_pronoun"] = (s.get("self_pronoun") or "").strip().split("/")[0][:20]
        p["addressed_as"] = (s.get("addressed_as") or "").strip()[:40]
        if p["addressed_as"] in ("-", "none"):
            p["addressed_as"] = ""
        if p["role"] in (p["th"], p["en"]):
            p["role"] = ""                               # the model often just repeats the name
    if profile and not job["options"].get("book_profile_edited"):
        store.update_options(jid, book_profile=profile)     # an empty draft never replaces an earlier one

    store.x("DELETE FROM glossary WHERE job_id=? AND source != 'user'", (jid,))
    cols = GLOSSARY_COLS
    store.many(f"INSERT OR IGNORE INTO glossary(job_id,{','.join(cols)}) VALUES(?{',?' * len(cols)})",
               [(jid, *[int(e[c]) if c == "keep_english" else e.get(c, "") for c in cols]) for e in entries])
    if job["options"].get("auto_glossary"):
        store.set_status(jid, "queued", "glossary อัตโนมัติ เริ่มแปล")
    else:
        store.set_status(jid, "review", f"glossary พร้อมแล้ว {len(entries)} คำ ตรวจ/แก้ แล้วกดเริ่มแปล")


def glossary_entries(jid: str) -> list[dict]:
    return [dict(r) for r in store.q(f"SELECT {','.join(GLOSSARY_COLS)} FROM glossary WHERE job_id=?", (jid,))]


def style_notes(job: dict) -> str:
    """Book profile (drafted, editable) + the user's own notes: rules for the whole book."""
    return "\n".join(x for x in (job["options"].get("book_profile", ""), job["options"].get("style_notes", "")) if x.strip())


def run_translation(jid: str) -> None:
    job = store.job(jid)
    store.set_status(jid, "translating", "")
    find = glossary.matcher(glossary_entries(jid))
    notes = style_notes(job)
    rows = store.q("SELECT id, doc, marked, plain FROM segments WHERE job_id=? AND status='pending' ORDER BY id", (jid,))
    chunks = make_chunks([(r["id"], r["doc"], r["marked"], r["plain"]) for r in rows], settings.chunk_chars)
    titles = {r["doc"]: r["title"] for r in store.q("SELECT doc, title FROM docs WHERE job_id=?", (jid,))}
    doc_of = {r["id"]: r["doc"] for r in rows}

    # Speed = what actually finished in the last few minutes, over that wall time. With 4 chunks in flight
    # they often finish together, so per-chunk timings (or "time since the previous chunk") jump around.
    window = 300.0
    finished: deque = deque()                 # (time, English chars) per finished chunk
    tokens: deque = deque()                   # (time, generated tokens) per model reply, all threads
    st = job["stats"]
    st.setdefault("started", time.time())
    store.update_stats(jid, started=st["started"])
    t_start = time.time()

    def on_reply(reply):
        tokens.append((time.time(), reply.completion_tokens))

    def rates() -> tuple[float, float]:
        now = time.time()
        span = min(window, now - t_start)
        for q in (finished, tokens):
            while q and q[0][0] < now - window:
                q.popleft()
        if span < 30:
            return 0.0, 0.0                   # too early to say anything useful
        return sum(c for _, c in finished) / span, sum(t for _, t in list(tokens)) / span

    # Several chunks at once: llama-server batches them on the GPU (measured: 4 at once = 2.6x the
    # throughput of 1, same model and prompts, so the same quality). Chunks are independent, so order
    # of completion does not matter; each is saved as soon as it is done.
    n = max(1, settings.translate_parallel)
    pending = deque(chunks)
    running: dict = {}
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="chunk") as pool:
        while pending or running:
            go_on = _status(jid) in ("translating", "waiting")
            while go_on and pending and len(running) < n:
                chunk = pending.popleft()
                doc = doc_of[chunk[0].id]
                store.set_message(jid, f"กำลังแปล: {titles.get(doc, doc)}")
                running[pool.submit(translate_chunk, chunk, find, notes, on_reply)] = chunk
            if not running:
                break                                         # paused / cancelled with nothing in flight
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            server_down = False
            for fut in done:
                chunk = running.pop(fut)
                try:
                    outs = fut.result()
                except llm.ServerUnavailable:
                    pending.appendleft(chunk)                 # try this chunk again later
                    server_down = True
                    continue
                store.many("UPDATE segments SET out=?, status=?, note=? WHERE job_id=? AND id=? AND edited=0",
                           [(o.marked, o.status, o.note, jid, o.id) for o in outs])
                finished.append((time.time(), sum(len(x.plain) for x in chunk)))
                cps, tps = rates()
                store.update_stats(jid, chars_per_sec=round(cps, 1), gen_tps=round(tps, 1),
                                   current_doc=titles.get(doc_of[chunk[0].id], ""))
            if server_down and not running:
                _wait_for_server(jid)
                if _status(jid) not in ACTIVE:
                    return
                store.set_status(jid, "translating", "ต่อเครื่องโมเดลได้แล้ว แปลต่อ")
            if _status(jid) not in ("translating", "waiting") and not running:
                return                                        # paused / cancelled / deleted

    if _status(jid) == "translating" and not pending:
        store.update_stats(jid, finished=time.time())
        store.set_status(jid, "done", "แปลเสร็จแล้ว")
