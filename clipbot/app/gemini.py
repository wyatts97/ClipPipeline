"""Gemini over REST: JSON answers (with a response schema) and videos through the File API.

Made for the free tier: requests wait their turn under a tokens-a-minute budget shared by everything
in clipbot, a 429 is retried after the delay Gemini names, and a quota that's used up for the day
raises QuotaExhausted so callers fall back instead of hammering it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import Settings

log = logging.getLogger("clipbot.gemini")

UPLOAD_TIMEOUT = 600
ACTIVE_TIMEOUT = 300          # Gemini processes an uploaded video before it can be used
MAX_RETRIES = 3


class GeminiError(RuntimeError):
    pass


class QuotaExhausted(GeminiError):
    """The free tier's requests for today (or this model) are used up."""


class _Budget:
    """Tokens sent in the last minute, per API key and model."""

    def __init__(self) -> None:
        self.sent: deque[tuple[float, int]] = deque()
        self.lock = asyncio.Lock()

    async def take(self, tokens: int, per_minute: int) -> None:
        tokens = min(tokens, per_minute)
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.sent and now - self.sent[0][0] >= 60:
                    self.sent.popleft()
                if sum(n for _, n in self.sent) + tokens <= per_minute:
                    self.sent.append((now, tokens))
                    return
                await asyncio.sleep(max(0.5, 60 - (now - self.sent[0][0])))


_budgets: dict[tuple[str, str], _Budget] = {}


def _budget(key: str, model: str) -> _Budget:
    return _budgets.setdefault((key[-8:], model), _Budget())


def _retry_delay(body: dict[str, Any]) -> tuple[Optional[float], bool]:
    """(seconds to wait, used up for the day) from a 429's error details."""
    delay, daily = None, False
    for detail in (body.get("error") or {}).get("details") or []:
        if str(detail.get("retryDelay") or "").endswith("s"):
            try:
                delay = float(str(detail["retryDelay"])[:-1])
            except ValueError:
                pass
        for violation in detail.get("violations") or []:
            if "PerDay" in str(violation.get("quotaId") or ""):
                daily = True
    return delay, daily


class Gemini:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None,
                 model: Optional[str] = None) -> None:
        if not settings.gemini_api_key:
            raise GeminiError("clipbot needs GEMINI_API_KEY")
        self.settings = settings
        self.model = model or settings.gemini_model
        self.base = settings.gemini_api_url.rstrip("/")
        self.headers = {"x-goog-api-key": settings.gemini_api_key}
        self._own = client is None
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30))

    async def aclose(self) -> None:
        if self._own:
            await self.client.aclose()

    # ---- videos ---------------------------------------------------------------------------
    async def upload(self, path: Path, mime: str = "video/mp4") -> dict[str, Any]:
        """Upload a file and wait until Gemini can use it: {"name", "uri", "mimeType"}."""
        size = path.stat().st_size
        start = await self.client.post(
            f"{self.base}/upload/v1beta/files", json={"file": {"display_name": path.name}},
            headers={**self.headers, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
                     "X-Goog-Upload-Header-Content-Length": str(size),
                     "X-Goog-Upload-Header-Content-Type": mime},
            timeout=60)
        self._check(start, "upload")
        url = start.headers.get("x-goog-upload-url")
        if not url:
            raise GeminiError("Gemini gave no upload URL")
        done = await self.client.post(url, content=path.read_bytes(), timeout=UPLOAD_TIMEOUT, headers={
            "Content-Length": str(size), "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize"})
        self._check(done, "upload")
        info = done.json().get("file") or {}
        deadline = time.monotonic() + ACTIVE_TIMEOUT
        while str(info.get("state") or "ACTIVE").upper() == "PROCESSING":
            if time.monotonic() > deadline:
                raise GeminiError(f"Gemini took too long to process {path.name}")
            await asyncio.sleep(2)
            resp = await self.client.get(f"{self.base}/v1beta/{info['name']}", headers=self.headers, timeout=60)
            self._check(resp, "upload")
            info = resp.json()
        if str(info.get("state") or "").upper() == "FAILED":
            raise GeminiError(f"Gemini couldn't read {path.name}")
        return {"name": info["name"], "uri": info["uri"], "mimeType": info.get("mimeType") or mime}

    async def delete(self, name: str) -> None:
        try:
            await self.client.delete(f"{self.base}/v1beta/{name}", headers=self.headers, timeout=30)
        except httpx.HTTPError as e:      # files expire on their own after 48 h
            log.debug("couldn't delete %s: %s", name, e)

    # ---- answers --------------------------------------------------------------------------
    async def generate_json(self, parts: list[dict[str, Any]], schema: dict[str, Any], *,
                            est_tokens: int = 2000, temperature: float = 0.4,
                            low_res: bool = False, retries: int = MAX_RETRIES) -> dict[str, Any]:
        """Ask for JSON in `schema`. Waits under the tokens-a-minute budget and retries a 429 or a
        5xx `retries` times (0 for someone waiting on a button); QuotaExhausted when the daily
        quota is gone."""
        config: dict[str, Any] = {"responseMimeType": "application/json", "responseSchema": schema,
                                  "temperature": temperature}
        if low_res:
            config["mediaResolution"] = "MEDIA_RESOLUTION_LOW"
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": config}
        url = f"{self.base}/v1beta/models/{self.model}:generateContent"
        budget = _budget(self.settings.gemini_api_key, self.model)
        for attempt in range(retries + 1):
            await budget.take(est_tokens, self.settings.director_tpm)
            try:
                resp = await self.client.post(url, json=body, headers=self.headers)
            except httpx.HTTPError as e:
                if attempt == retries:
                    raise GeminiError(f"Gemini didn't answer: {e}") from e
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                delay, daily = _retry_delay(_json(resp))
                if daily or attempt == retries:
                    raise QuotaExhausted(f"Gemini's free quota for {self.model} is used up"
                                         + (" for today" if daily else ""))
                log.info("gemini: rate limited, waiting %ss", delay or 30)
                await asyncio.sleep(min(delay or 30, 90))
                continue
            if resp.status_code >= 500 and attempt < retries:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            self._check(resp, "request")
            data = resp.json()
            try:
                candidate = data["candidates"][0]
                text = "".join(p.get("text", "") for p in candidate["content"]["parts"])
                return json.loads(_strip_fences(text))
            except (KeyError, IndexError, ValueError) as e:
                reason = (data.get("promptFeedback") or {}).get("blockReason") or \
                    ((data.get("candidates") or [{}])[0].get("finishReason"))
                raise GeminiError(f"Gemini's answer wasn't usable ({reason or e})") from e
        raise GeminiError("Gemini didn't answer")

    @staticmethod
    def _check(resp: httpx.Response, what: str) -> None:
        if resp.status_code < 400:
            return
        message = ((_json(resp).get("error") or {}).get("message") or resp.text or "")[:300]
        raise GeminiError(f"Gemini {what} failed ({resp.status_code}): {message}")


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _strip_fences(text: str) -> str:
    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())


def video_part(file: dict[str, Any]) -> dict[str, Any]:
    return {"fileData": {"mimeType": file["mimeType"], "fileUri": file["uri"]}}


def text_part(text: str) -> dict[str, Any]:
    return {"text": text}
