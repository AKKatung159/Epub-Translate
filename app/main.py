"""HTTP API + static web UI."""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm, worker
from .config import settings
from .epub import TAG_RE, Book, check_tags
from .store import GLOSSARY_COLS, job_dir, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app):
    worker.start()
    yield


app = FastAPI(title="EPUB Translate", lifespan=lifespan)


def _job_or_404(jid: str) -> dict:
    j = store.job(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return j


def _summary(j: dict) -> dict:
    c = store.counts(j["id"])
    total_n = sum(v["n"] for v in c.values())
    total_chars = sum(v["chars"] for v in c.values())
    done_chars = sum(v["chars"] for k, v in c.items() if k != "pending")
    st = j["stats"]
    eta = None
    if j["status"] in ("translating", "waiting", "queued") and st.get("chars_per_sec"):
        eta = int((total_chars - done_chars) / st["chars_per_sec"])
    gl = store.q("SELECT COUNT(*) n FROM glossary WHERE job_id=?", (j["id"],))[0]["n"]
    readable, ready, docs_total = store.readable(j["id"])
    return {
        "id": j["id"], "name": j["name"], "created": j["created"], "status": j["status"], "message": j["message"],
        "options": j["options"], "segments": total_n,
        "done": c.get("done", {}).get("n", 0), "flagged": c.get("flagged", {}).get("n", 0),
        "pending": c.get("pending", {}).get("n", 0),
        "percent": round(100 * done_chars / total_chars, 1) if total_chars else 0,
        "eta_seconds": eta, "chars_per_sec": st.get("chars_per_sec"), "gen_tps": st.get("gen_tps"),
        "current_doc": st.get("current_doc"), "glossary_terms": gl,
        "glossary_progress": [st.get("glossary_done"), st.get("glossary_total")],
        "started": st.get("started"), "finished": st.get("finished"),
        "readable_until": readable, "docs_ready": ready, "docs_total": docs_total,
        "series_from": j["options"].get("series_from", ""),
    }


@app.get("/api/health")
def health():
    try:
        models = llm.models()
        return {"ok": True, "models": models, "translate_model": settings.translate_model,
                "glossary_model": settings.glossary_model}
    except llm.ServerUnavailable as e:
        return {"ok": False, "error": str(e)[:200]}


@app.get("/api/jobs")
def list_jobs():
    return [_summary(j) for j in store.jobs()]


@app.post("/api/jobs")
async def create_job(file: UploadFile = File(...), bilingual: bool = Form(False), auto_glossary: bool = Form(False),
                     style_notes: str = Form(""), series_from: str = Form("")):
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"ไฟล์ใหญ่เกิน {settings.max_upload_mb} MB")
    try:
        book = Book.load(data)
        segs = book.segments()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"อ่านไฟล์ EPUB ไม่ได้: {e}") from e
    if not segs:
        raise HTTPException(400, "ไม่พบข้อความภาษาอังกฤษในไฟล์นี้")
    name = re.sub(r"\.epub$", "", file.filename or "book", flags=re.I)
    prev = store.job(series_from) if series_from else None
    if prev and not style_notes.strip():
        style_notes = prev["options"].get("style_notes", "")     # the series keeps its style rules
    jid = store.new_job(name, {"bilingual": bilingual, "auto_glossary": auto_glossary, "style_notes": style_notes,
                               "series_from": prev["id"] if prev else ""})
    try:
        (job_dir(jid) / "source.epub").write_bytes(data)
        store.many("INSERT INTO segments(job_id,id,doc,path,kind,marked,plain) VALUES(?,?,?,?,?,?,?)",
                   [(jid, i, s.doc, s.path, s.kind, s.marked, s.plain) for i, s in enumerate(segs)])
        store.many("INSERT OR IGNORE INTO docs(job_id,doc,ord,title) VALUES(?,?,?,?)",
                   [(jid, d, i, book.titles.get(d, d)) for i, d in enumerate(book.docs)])
    except Exception as e:  # noqa: BLE001
        log.exception("saving job %s failed", jid)
        store.delete_job(jid)                       # no half-created job left in the list
        shutil.rmtree(job_dir(jid), ignore_errors=True)
        raise HTTPException(500, f"บันทึกงานไม่สำเร็จ: {e}") from e
    store.set_status(jid, "glossary", f"อ่านไฟล์แล้ว {len(book.docs)} ไฟล์ {len(segs)} ย่อหน้า กำลังหาคำเฉพาะ…")
    worker.wake.set()
    return _summary(store.job(jid))


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    return _summary(_job_or_404(jid))


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    _job_or_404(jid)
    store.set_status(jid, "cancelled")
    time.sleep(0.2)
    store.delete_job(jid)
    shutil.rmtree(job_dir(jid), ignore_errors=True)
    return {"ok": True}


class Options(BaseModel):
    bilingual: bool | None = None
    style_notes: str | None = None
    book_profile: str | None = None


@app.patch("/api/jobs/{jid}")
def patch_job(jid: str, o: Options):
    _job_or_404(jid)
    changes = {k: v for k, v in o.model_dump().items() if v is not None}
    if "book_profile" in changes:
        changes["book_profile_edited"] = True        # "make the glossary again" keeps the user's profile
    store.update_options(jid, **changes)
    return _summary(store.job(jid))


@app.post("/api/jobs/{jid}/{action}")
def job_action(jid: str, action: str):
    j = _job_or_404(jid)
    s = j["status"]
    if action == "start" and s in ("review", "paused", "error", "done", "cancelled"):
        store.set_status(jid, "queued", "รอคิวแปล")
    elif action == "pause" and s in ("queued", "translating", "waiting", "glossary"):
        store.set_status(jid, "paused", "หยุดชั่วคราว")
    elif action == "resume" and s == "paused":
        has_gloss = store.q("SELECT COUNT(*) n FROM glossary WHERE job_id=?", (jid,))[0]["n"]
        store.set_status(jid, "queued" if has_gloss or j["options"].get("auto_glossary") else "glossary", "ทำต่อ")
    elif action == "regenerate-glossary" and s in ("review", "paused", "error", "done"):
        store.set_status(jid, "glossary", "ทำ glossary ใหม่")
    elif action == "retry-flagged" and s in ("review", "paused", "error", "done"):
        store.x("UPDATE segments SET status='pending' WHERE job_id=? AND status='flagged' AND edited=0", (jid,))
        store.set_status(jid, "queued", "แปลย่อหน้าที่มีปัญหาใหม่")
    elif action == "cancel":
        store.set_status(jid, "cancelled", "ยกเลิกแล้ว")
    else:
        raise HTTPException(409, f"ทำ {action} ตอนสถานะ {s} ไม่ได้")
    worker.wake.set()
    return _summary(store.job(jid))


# glossary -------------------------------------------------------------------------------------------------

class Term(BaseModel):
    en: str
    type: str = "term"
    th: str = ""
    keep_english: bool = False
    gender: str = ""
    role: str = ""
    particle: str = ""
    self_pronoun: str = ""
    addressed_as: str = ""


EDITABLE = ("type", "th", "keep_english", "gender", "role", "particle", "self_pronoun", "addressed_as")


@app.get("/api/jobs/{jid}/glossary")
def get_glossary(jid: str):
    _job_or_404(jid)
    return [dict(r) for r in store.q(
        f"SELECT {','.join(GLOSSARY_COLS)} FROM glossary WHERE job_id=? ORDER BY count DESC, en", (jid,))]


@app.put("/api/jobs/{jid}/glossary")
def put_glossary(jid: str, terms: list[Term]):
    _job_or_404(jid)
    old = {r["en"]: dict(r) for r in store.q("SELECT * FROM glossary WHERE job_id=?", (jid,))}
    rows = []
    for t in terms:
        en = t.en.strip()
        if not en:
            continue
        new = t.model_dump()
        new["th"] = t.th.strip() or en
        new["keep_english"] = int(t.keep_english)
        o = old.get(en, {})
        changed = not o or any(str(o.get(k, "")) != str(new[k]) for k in EDITABLE)
        row = {**new, "en": en, "count": o.get("count", 0), "example": o.get("example", ""),
               "source": "user" if changed else o.get("source", "")}
        rows.append((jid, *[row[c] for c in GLOSSARY_COLS]))
    store.x("DELETE FROM glossary WHERE job_id=?", (jid,))
    store.many(f"INSERT INTO glossary(job_id,{','.join(GLOSSARY_COLS)}) VALUES(?{',?' * len(GLOSSARY_COLS)})", rows)
    return {"ok": True, "terms": len(rows)}


@app.post("/api/jobs/{jid}/glossary/import")
def import_glossary(jid: str, source: str):
    """Book 2 of a series: take names, Thai forms and the character sheet from an earlier book."""
    j = _job_or_404(jid)
    _job_or_404(source)
    if source == jid:
        raise HTTPException(400, "เลือกเล่มอื่น")
    if j["status"] in ("glossary", "translating"):
        raise HTTPException(409, "รอให้ขั้นนี้เสร็จก่อน หรือหยุดชั่วคราว")
    return {"ok": True, "terms": worker.import_series(jid, source)}


# segments / review ----------------------------------------------------------------------------------------

@app.get("/api/jobs/{jid}/docs")
def docs(jid: str):
    _job_or_404(jid)
    rows = store.q("""SELECT d.doc, d.title, d.ord, COUNT(s.id) n,
                      SUM(s.status='done') done, SUM(s.status='flagged') flagged
                      FROM docs d LEFT JOIN segments s ON s.job_id=d.job_id AND s.doc=d.doc AND s.kind!='nav'
                      WHERE d.job_id=? GROUP BY d.doc ORDER BY d.ord""", (jid,))
    return [dict(r) for r in rows if r["n"]]


@app.get("/api/jobs/{jid}/segments")
def segments(jid: str, status: str | None = None, doc: str | None = None, offset: int = 0, limit: int = 50):
    _job_or_404(jid)
    where, args = ["job_id=?"], [jid]
    if status:
        where.append("status=?")
        args.append(status)
    if doc:
        where.append("doc=?")
        args.append(doc)
    total = store.q(f"SELECT COUNT(*) n FROM segments WHERE {' AND '.join(where)}", args)[0]["n"]
    rows = store.q(f"SELECT id,doc,kind,marked,plain,out,status,note,edited FROM segments WHERE {' AND '.join(where)} "
                   "ORDER BY id LIMIT ? OFFSET ?", args + [limit, offset])
    return {"total": total, "items": [dict(r) for r in rows]}


class Edit(BaseModel):
    out: str


@app.put("/api/jobs/{jid}/segments/{sid}")
def edit_segment(jid: str, sid: int, e: Edit):
    _job_or_404(jid)
    r = store.q("SELECT marked FROM segments WHERE job_id=? AND id=?", (jid, sid))
    if not r:
        raise HTTPException(404)
    out = e.out.strip()
    if TAG_RE.search(r[0]["marked"]) and TAG_RE.search(out):
        problem = check_tags(r[0]["marked"], out)
        if problem:
            raise HTTPException(422, f"แท็กไม่ครบ: {problem}")
    store.x("UPDATE segments SET out=?, status='done', note='แก้ด้วยมือ', edited=1 WHERE job_id=? AND id=?", (out, jid, sid))
    return {"ok": True}


@app.post("/api/jobs/{jid}/segments/{sid}/retranslate")
def retranslate(jid: str, sid: int):
    from .glossary import matcher
    from .translate import SegIn, translate_one
    _job_or_404(jid)
    r = store.q("SELECT marked, plain FROM segments WHERE job_id=? AND id=?", (jid, sid))
    if not r:
        raise HTTPException(404)
    entries = worker.glossary_entries(jid)
    find = matcher(entries)
    speaker = worker.speaker_map(jid, entries).get(sid)
    try:
        o = translate_one(SegIn(sid, r[0]["marked"], r[0]["plain"], speaker), find,
                          worker.style_notes(store.job(jid)), lambda _: None)
    except llm.ServerUnavailable as e:
        raise HTTPException(503, f"ต่อเครื่องโมเดลไม่ได้: {e}") from e
    store.x("UPDATE segments SET out=?, status=?, note=?, edited=0 WHERE job_id=? AND id=?",
            (o.marked, o.status, o.note, jid, sid))
    return {"out": o.marked, "status": o.status, "note": o.note}


# download -------------------------------------------------------------------------------------------------

@app.get("/api/jobs/{jid}/download")
def download(jid: str, bilingual: bool | None = None):
    j = _job_or_404(jid)
    book = Book.load((job_dir(jid) / "source.epub").read_bytes())
    tr = {(r["doc"], r["path"]): r["out"] for r in store.q(
        "SELECT doc, path, out FROM segments WHERE job_id=? AND status IN ('done','flagged') AND out IS NOT NULL", (jid,))}
    bi = j["options"].get("bilingual", False) if bilingual is None else bilingual
    data = book.build(tr, bilingual=bi)
    suffix = "_th_bilingual" if bi else "_th"
    if j["status"] != "done":
        suffix += "_partial"
    fname = re.sub(r"[\\/:*?\"<>|]+", "_", j["name"])[:120] + suffix + ".epub"
    ascii_name = re.sub(r"[^A-Za-z0-9\-_. ]+", "_", fname)
    return Response(data, media_type="application/epub+zip", headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(fname)}"})


def _versioned_index() -> str:
    """index.html with ?v=<content hash> on the CSS/JS links, so a new image is never served with stale assets."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("style.css", "app.js"):
        v = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
    return html


INDEX_HTML = _versioned_index()


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
