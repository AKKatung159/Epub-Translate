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
    """Where convert() writes: "book.xtc", or "book 01.xtc", "book 02.xtc"... for a long book."""
    return pdf_dir(d["id"]) / ("book" + EXT[int(d["options"].get("depth", 1))])


def parts(d: dict) -> list[Path]:
    o = output(d)
    single = o.parent / o.name
    if single.exists():
        return [single]
    return sorted(o.parent.glob(f"book [0-9][0-9]{o.suffix}"))


def device_files(d: dict, base: str) -> tuple[str, list[tuple[str, Path]]]:
    """(folder under /Books or "", [(file name, path)]) as the files should appear on the X4.

    One file: "/Books/<name>.xtc". Several: their own folder "/Books/<name>/" with "01 บทที่ 1-93.xtc",
    "02 บทที่ 94-187.xtc"...: the number first, because the X4's file list cuts long names."""
    files = parts(d)
    if len(files) == 1:
        return "", [(f"{base}{files[0].suffix}", files[0])]
    labels = {}
    index = pdf_dir(d["id"]) / "book.parts.json"
    if index.exists():
        labels = {x["file"]: x["label"] for x in json.loads(index.read_text(encoding="utf-8"))}
    return base, [(f"{k + 1:02d} {labels.get(f.name) or 'ส่วนที่ ' + str(k + 1)}{f.suffix}".strip(), f)
                  for k, f in enumerate(files)]


PART_PAGES = settings.pdf_part_pages


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
    opts = Options(mode=o.get("mode", "auto"), rotate=o.get("rotate", "cw"), depth=int(o.get("depth", 1)),
                   trim=bool(o.get("trim", True)), per_part=PART_PAGES)
    for old in [*pdf_dir(pid).glob("book*.xtc*"), pdf_dir(pid) / "all.zip", pdf_dir(pid) / "book.parts.json"]:
        old.unlink(missing_ok=True)
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
        return
    except Exception as e:  # noqa: BLE001
        log.exception("pdf %s failed", pid)
        set_status(pid, "error", f"แปลงไม่สำเร็จ: {e}")
        return
    took = time.time() - t0
    files = parts(d)
    size = sum(f.stat().st_size for f in files)
    note = f" · {len(files)} ไฟล์" if len(files) > 1 else ""
    set_status(pid, "done", f"เสร็จใน {took:.0f} วินาที{note}", out_pages=pages, out_size=size)
    if took > 120:                                   # only long ones are worth a message
        notify.send(f"PDF พร้อมอ่านบน X4: {d['name']}", f"{pages} หน้าจอ · {size / 1048576:.1f} MB{note}", "ok")
