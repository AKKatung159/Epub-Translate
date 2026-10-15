"""OpenAI-compatible chat client (llama-server router on the ROG Ally) with waiting and retries."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from .config import settings

log = logging.getLogger("llm")


class ServerUnavailable(Exception):
    """The model server cannot be reached (Ally asleep, Wi-Fi down, model loading)."""


class ContextTooLong(Exception):
    """The request does not fit the model's context window: split it."""


@dataclass
class Reply:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str
    gen_tps: float


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {}
    return httpx.Client(base_url=settings.llm_base_url.rstrip("/"), headers=headers,
                        timeout=httpx.Timeout(settings.llm_timeout, connect=10))


def chat(model: str, messages: list[dict], *, max_tokens: int, temperature: float = 0.2,
         json_schema: dict | None = None) -> Reply:
    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "top_p": 0.9, "repeat_penalty": 1.05, "cache_prompt": True}
    if json_schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": json_schema}}
    try:
        with _client() as c:
            r = c.post("/chat/completions", json=body)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
        raise ServerUnavailable(str(e)) from e
    if r.status_code in (502, 503, 504):
        raise ServerUnavailable(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code == 400 and "context" in r.text.lower():
        raise ContextTooLong(r.text[:300])
    if r.status_code == 401:
        raise ServerUnavailable("HTTP 401: wrong LLM_API_KEY")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    timings = data.get("timings", {})
    return Reply(
        text=(choice.get("message", {}).get("content") or "").strip(),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        finish_reason=choice.get("finish_reason", ""),
        gen_tps=float(timings.get("predicted_per_second") or 0),
    )


def chat_json(model: str, messages: list[dict], schema: dict, *, max_tokens: int) -> object:
    reply = chat(model, messages, max_tokens=max_tokens, temperature=0.1, json_schema=schema)
    text = reply.text
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
    return json.loads(text[start:])


def models() -> list[str]:
    try:
        with _client() as c:
            r = c.get("/models", timeout=5)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", [])]
    except Exception as e:  # noqa: BLE001
        raise ServerUnavailable(str(e)) from e


def wait_until_up(should_stop, on_wait) -> None:
    """Block until the model server answers, calling on_wait(seconds_waited) while it is down."""
    waited, delay = 0, 5
    while True:
        try:
            models()
            return
        except ServerUnavailable:
            if should_stop():
                return
            on_wait(waited)
            time.sleep(delay)
            waited += delay
            delay = min(delay * 2, 60)
