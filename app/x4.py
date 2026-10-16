"""Send files straight to the Xteink X4 while it is in File Transfer mode (CrossPoint web server, POST /upload).

The container cannot resolve crosspoint.local (mDNS), so the device address is a setting (X4_ADDRESS, or saved
from the UI); reserve the X4's IP in the router so it stays put. One send at a time; progress is kept in memory.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import httpx

from .config import settings
from .store import store

log = logging.getLogger("x4")
FOLDER = "/Books"
sends: dict[str, dict] = {}          # key ("pdf:<id>" / "book:<id>") -> {state, sent, total, message, t}
_lock = threading.Lock()


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


def start(key: str, files: list[tuple[str, Path | bytes]]) -> dict:
    """files: (name on the device, path or data). Runs in the background; returns the initial state."""
    with _lock:
        cur = sends.get(key)
        if cur and cur["state"] == "sending":
            return cur
        total = sum(len(f) if isinstance(f, bytes) else f.stat().st_size for _, f in files)
        sends[key] = {"state": "sending", "sent": 0, "total": total, "message": "กำลังส่ง…", "t": time.time()}
    threading.Thread(target=_run, args=(key, files), daemon=True).start()
    return sends[key]


def _run(key: str, files) -> None:
    st = sends[key]
    addr = address()
    try:
        s = status()
        if not s["ok"]:
            raise RuntimeError(s.get("error") or "X4 ไม่ตอบ")
        with httpx.Client(timeout=httpx.Timeout(60, connect=6)) as c:
            for k, (name, src) in enumerate(files):
                st["message"] = f"กำลังส่ง {name}" + (f" ({k + 1}/{len(files)})" if len(files) > 1 else "")

                def bump(n):
                    st["sent"] += n
                fh = open(src, "rb") if isinstance(src, Path) else None
                body = _Counting(fh, bump) if fh else src
                try:
                    r = c.post(f"http://{addr}/upload", params={"path": FOLDER}, files={"file": (name, body)})
                finally:
                    if fh:
                        fh.close()
                if isinstance(src, bytes):
                    bump(len(src))
                if r.status_code != 200:
                    raise RuntimeError(f"X4 ตอบ {r.status_code}: {r.text[:120]}")
        st.update(state="done", sent=st["total"], message=f"ส่งเข้า X4 แล้ว ({FOLDER})" +
                  (f" {len(files)} ไฟล์" if len(files) > 1 else ""))
    except Exception as e:  # noqa: BLE001
        log.warning("send %s failed: %s", key, e)
        st.update(state="error", message=str(e)[:200])
