"""Discord messages for the moments worth a phone buzz: glossary ready, 25/50/75%, done, stuck, error.

DISCORD_WEBHOOK_URL empty = off. A failed post is logged and forgotten: notifications never stop a job.
"""
from __future__ import annotations

import logging
import threading

import httpx

from .config import settings

log = logging.getLogger("notify")

COLORS = {"info": 0x2B4FC7, "ok": 0x1D7A46, "warn": 0xA75A06, "bad": 0xC0392B}


def send(title: str, text: str = "", kind: str = "info", job_id: str = "") -> None:
    if not settings.discord_webhook_url:
        return
    embed = {"title": title[:256], "description": text[:3000], "color": COLORS.get(kind, COLORS["info"])}
    if job_id and settings.public_url:
        embed["url"] = f"{settings.public_url.rstrip('/')}/#{job_id}"
    threading.Thread(target=_post, args=({"username": "EPUB Translate", "embeds": [embed]},), daemon=True).start()


def _post(body: dict) -> None:
    try:
        r = httpx.post(settings.discord_webhook_url, json=body, timeout=15)
        if r.status_code >= 300:
            log.warning("discord: HTTP %s %s", r.status_code, r.text[:200])
    except httpx.HTTPError as e:
        log.warning("discord: %s", e)


def duration(sec: float) -> str:
    sec = int(sec)
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h} ชม. {m} นาที" if h else f"{m} นาที"
