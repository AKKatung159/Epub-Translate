"""Send files to the Xteink X4 while it is in File Transfer mode (CrossPoint web server: /upload, /mkdir, /api/files).

A queue: press "send" on as many books as you like; one upload runs at a time (the X4 serves one), the rest wait.
Files already on the device with the same name and size are skipped, so "send everything" can be pressed again.
A book in several parts goes to its own folder, /Books/<name>/.

The container cannot resolve crosspoint.local (mDNS), so the device address is a setting (X4_ADDRESS, or saved
from the UI); reserve the X4's IP in the router so it stays put. Progress lives in memory only.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path

import httpx

from .config import settings
from .store import store

log = logging.getLogger("x4")
ROOT = "/Books"
sends: dict[str, dict] = {}          # key ("pdf:<id>" / "book:<id>") -> state shown in the UI
_queue: deque = deque()
_wake = threading.Event()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def address() -> str:
    r = store.q("SELECT value FROM kv WHERE key='x4_address'")
    return (r[0]["value"] if r else "") or settings.x4_address


def set_address(addr: str) -> None:
    addr = addr.strip().removeprefix("http://").rstrip("/")
    store.x("INSERT INTO kv(key, value) VALUES('x4_address', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (addr,))


def status() -> dict:
    """Is the X4 reachable right now (File Transfer screen open)?"""
    addr = address()
    if not addr:
        return {"ok": False, "address": "", "error": "ยังไม่ได้ตั้งที่อยู่ X4"}
    try:
        r = httpx.get(f"http://{addr}/api/status", timeout=4)
        return {"ok": r.status_code == 200, "address": addr, **(r.json() if r.status_code == 200 else {})}
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "address": addr, "error": f"ต่อ X4 ไม่ได้ (เปิดหน้า File Transfer บนเครื่องหรือยัง?): {type(e).__name__}"}


def queue_view() -> dict:
    """sends plus each waiting item's place in the queue (1 = next)."""
    with _lock:
        order = {job["key"]: i + 1 for i, job in enumerate(_queue)}
        return {k: {**v, "place": order.get(k)} for k, v in sends.items()}


def enqueue(key: str, title: str, files: list[tuple[str, Path | bytes]], folder: str = "") -> dict:
    """files: (name on the device, path or data); folder: sub-folder of /Books ("" = /Books itself)."""
    global _worker
    with _lock:
        cur = sends.get(key)
        if cur and cur["state"] in ("queued", "sending"):
            return cur
        total = sum(len(f) if isinstance(f, bytes) else f.stat().st_size for _, f in files)
        sends[key] = {"state": "queued", "title": title, "sent": 0, "total": total, "files": len(files),
                      "done_files": 0, "skipped": 0, "message": "รอคิว", "t": time.time(),
                      "folder": f"{ROOT}/{folder}" if folder else ROOT}
        _queue.append({"key": key, "files": files, "folder": folder})
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_loop, name="x4", daemon=True)
            _worker.start()
    _wake.set()
    return sends[key]


def cancel(key: str) -> None:
    with _lock:
        for job in list(_queue):
            if job["key"] == key:
                _queue.remove(job)
                sends[key].update(state="cancelled", message="ยกเลิกแล้ว")


def _loop() -> None:
    while True:
        with _lock:
            job = _queue.popleft() if _queue else None
        if job is None:
            _wake.wait(30)
            _wake.clear()
            continue
        _run(job)


class _Counting:
    """File wrapper that reports how much httpx has read (= sent)."""

    def __init__(self, f, on_read):
        self.f, self.on_read = f, on_read

    def read(self, n=-1):
        b = self.f.read(n)
        self.on_read(len(b))
        return b

    def __getattr__(self, k):
        return getattr(self.f, k)


def _listing(c: httpx.Client, base: str, path: str) -> dict[str, dict]:
    r = c.get(f"{base}/api/files", params={"path": path})
    return {f["name"]: f for f in r.json()} if r.status_code == 200 else {}


def _run(job: dict) -> None:
    key, st = job["key"], sends[job["key"]]
    base = f"http://{address()}"
    target = f"{ROOT}/{job['folder']}" if job["folder"] else ROOT
    st.update(state="sending", message="กำลังเชื่อมต่อ X4…")
    try:
        s = status()
        if not s["ok"]:
            raise RuntimeError(s.get("error") or "X4 ไม่ตอบ")
        with httpx.Client(timeout=httpx.Timeout(120, connect=6)) as c:
            parent = ROOT
            for part in [p for p in job["folder"].split("/") if p]:      # "Sherlock Holmes/Book" -> both levels
                if part not in _listing(c, base, parent):
                    r = c.post(f"{base}/mkdir", files={"name": (None, part), "path": (None, parent)})
                    if r.status_code != 200:
                        raise RuntimeError(f"สร้างโฟลเดอร์ไม่ได้: {r.text[:120]}")
                parent = f"{parent}/{part}"
            there = _listing(c, base, target)
            n = len(job["files"])
            for k, (name, src) in enumerate(job["files"]):
                size = len(src) if isinstance(src, bytes) else src.stat().st_size
                if there.get(name, {}).get("size") == size:          # already on the device
                    st["sent"] += size
                    st["skipped"] += 1
                    st["done_files"] += 1
                    continue
                st["message"] = f"กำลังส่ง {name}" + (f" ({k + 1}/{n})" if n > 1 else "")

                def bump(b):
                    st["sent"] += b
                fh = open(src, "rb") if isinstance(src, Path) else None
                try:
                    body = _Counting(fh, bump) if fh else src
                    r = c.post(f"{base}/upload", params={"path": target}, files={"file": (name, body)})
                finally:
                    if fh:
                        fh.close()
                if isinstance(src, bytes):
                    bump(size)
                if r.status_code != 200:
                    raise RuntimeError(f"X4 ตอบ {r.status_code}: {r.text[:120]}")
                st["done_files"] += 1
        skipped = f" (มีอยู่แล้ว {st['skipped']} ไฟล์ ข้ามไป)" if st["skipped"] else ""
        st.update(state="done", sent=st["total"], message=f"ส่งเข้า X4 แล้ว: {target}{skipped}")
    except Exception as e:  # noqa: BLE001
        log.warning("send %s failed: %s", key, e)
        st.update(state="error", message=str(e)[:200])
