"""Client for Postiz's public API (the self-hosted social media scheduler).

Endpoints used (Postiz v2.25 apps/backend/src/public-api/routes/v1/public.integrations.controller.ts),
all under <POSTIZ_URL>/public/v1 with the API key from Postiz → Settings in the Authorization header:
  GET    /integrations        connected channels
  POST   /upload              multipart "file" -> {id, path}
  POST   /posts               create / schedule -> [{postId, integration}]
  GET    /posts               ?startDate&endDate -> {posts: [{id, state, publishDate, releaseURL, ...}]}
  DELETE /posts/{id}          deletes the post and the others of its group
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

# Channel types clipbot posts clips to, by Postiz provider identifier.
VIDEO_PLATFORMS = {"youtube": "youtube", "tiktok": "tiktok", "instagram": "instagram",
                   "instagram-standalone": "instagram"}
PLATFORM_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


class PostizError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def lasting(self) -> bool:
        """A refusal that retrying won't change (bad settings, missing channel), not an outage."""
        return self.status is not None and 400 <= self.status < 500 and self.status not in (408, 429)


def as_html(text: str) -> str:
    """Postiz stores post text as editor HTML; one paragraph per line keeps the line breaks."""
    lines = [line.strip() for line in (text or "").splitlines()]
    return "".join(f"<p>{html.escape(line)}</p>" if line else "<p></p>" for line in lines)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_iso(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Postiz:
    def __init__(self, base_url: str, api_key: str, client: Optional[httpx.AsyncClient] = None):
        self.base_url = base_url.rstrip("/") + "/public/v1"
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, read=300))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._client.request(method, self.base_url + path,
                                              headers={"Authorization": self.api_key}, **kwargs)
        except httpx.HTTPError as e:
            raise PostizError(f"Postiz isn't reachable ({e or type(e).__name__}).") from e
        if resp.status_code == 401:
            raise PostizError("Postiz refused the API key. Copy it again from Postiz → Settings → "
                              "Public API into POSTIZ_API_KEY.", 401)
        if resp.status_code >= 400:
            raise PostizError(f"Postiz answered {resp.status_code}: {_message(resp)}", resp.status_code)
        return resp.json() if resp.content else None

    async def healthy(self) -> bool:
        try:
            await self._call("GET", "/is-connected")
            return True
        except PostizError:
            return False

    async def channels(self) -> list[dict[str, Any]]:
        """Connected, enabled channels clipbot can post a vertical video to."""
        items = await self._call("GET", "/integrations") or []
        return [{"id": c["id"], "name": c.get("name") or c["identifier"], "identifier": c["identifier"],
                 "platform": VIDEO_PLATFORMS[c["identifier"]], "picture": c.get("picture"),
                 "disabled": bool(c.get("disabled"))}
                for c in items if c.get("identifier") in VIDEO_PLATFORMS]

    async def upload(self, path: Path) -> dict[str, Any]:
        with open(path, "rb") as f:
            media = await self._call("POST", "/upload", files={"file": (path.name, f, "video/mp4")})
        if not media or not media.get("path"):
            raise PostizError("Postiz accepted the upload but returned no media path.")
        return {"id": media["id"], "path": media["path"]}

    async def create_post(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        return await self._call("POST", "/posts", json=body) or []

    async def posts(self, start: float, end: float) -> list[dict[str, Any]]:
        doc = await self._call("GET", "/posts", params={"startDate": iso(start), "endDate": iso(end)})
        return (doc or {}).get("posts") or []

    async def delete_post(self, post_id: str) -> None:
        await self._call("DELETE", f"/posts/{post_id}")


def _message(resp: httpx.Response) -> str:
    try:
        doc = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(doc, dict):
        msg = doc.get("msg") or doc.get("message") or doc.get("error") or doc
        if isinstance(msg, list):
            msg = "; ".join(str(m) for m in msg)
        return str(msg)[:500]
    return str(doc)[:500]
