"""PDF -> Xteink conversions: a queue of their own (no model needed), so they never wait for a translation."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from . import notify
from .config import settings
from .pdfslice import Options, convert
from .store import store

log = logging.getLogger("pdfworker")
wake = threading.Event()
EXT = {1: ".xtc", 2: ".xtch"}


def pdf_dir(pid: str) -> Path:
    d = settings.data_dir / "pdf" / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def row(pid: str) -> dict | None:
    r = store.q("SELECT * FROM pdfs WHERE id=?", (pid,))
    if not r:
        return None
    d = dict(r[0])
    d["options"] = json.loads(d["options"] or "{}")
    return d


def rows() -> list[dict]:
    return [row(r["id"]) for r in store.q("SELECT id FROM pdfs ORDER BY created DESC")]


def new(name: str, pages: int, options: dict) -> str:
    pid = uuid.uuid4().hex[:12]
    store.x("INSERT INTO pdfs(id,name,created,status,options,pdf_pages) VALUES(?,?,?,?,?,?)",
            (pid, name, time.time(), "queued", json.dumps(options), pages))
    return pid


def output(d: dict) -> Path:
    return pdf_dir(d["id"]) / ("book" + EXT[int(d["options"].get("depth", 2))])


def set_status(pid: str, status: str, message: str = "", **cols) -> None:
    sets = ", ".join(f"{k}=?" for k in cols)
    store.x(f"UPDATE pdfs SET status=?, message=?{', ' + sets if sets else ''} WHERE id=?",
            (status, message, *cols.values(), pid))


def start() -> None:
    store.x("UPDATE pdfs SET status='queued' WHERE status='converting'")    # restart: do it again
    threading.Thread(target=_loop, name="pdf", daemon=True).start()


def _loop() -> None:
    while True:
        r = store.q("SELECT id FROM pdfs WHERE status='queued' ORDER BY created LIMIT 1")
        if not r:
            wake.wait(10)
            wake.clear()
            continue
        _run(r[0]["id"])


def _run(pid: str) -> None:
    d = row(pid)
    o = d["options"]
    opts = Options(mode=o.get("mode", "auto"), rotate=o.get("rotate", "cw"), depth=int(o.get("depth", 2)),
                   trim=bool(o.get("trim", True)))
    for old in pdf_dir(pid).glob("book.*"):
        old.unlink()
    set_status(pid, "converting", "กำลังแปลงหน้า…", done_pages=0, out_pages=0)
    t0 = time.time()

    def progress(i, n, dev):
        if i == n or i % 5 == 0:
            store.x("UPDATE pdfs SET done_pages=?, out_pages=?, message=? WHERE id=?",
                    (i, dev, f"หน้า {i}/{n}", pid))

    def stop():
        cur = row(pid)
        return not cur or cur["status"] != "converting"

    dst = output(d)
    try:
        pages = convert(pdf_dir(pid) / "source.pdf", dst, opts, d["name"], progress, stop)
    except InterruptedError:
        dst.unlink(missing_ok=True)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("pdf %s failed", pid)
        dst.unlink(missing_ok=True)
        set_status(pid, "error", f"แปลงไม่สำเร็จ: {e}")
        return
    took = time.time() - t0
    set_status(pid, "done", f"เสร็จใน {took:.0f} วินาที", out_pages=pages, out_size=dst.stat().st_size)
    if took > 120:                                   # only long ones are worth a message
        notify.send(f"PDF พร้อมอ่านบน X4: {d['name']}", f"{pages} หน้าจอ · {dst.stat().st_size / 1048576:.1f} MB",
                    "ok")
