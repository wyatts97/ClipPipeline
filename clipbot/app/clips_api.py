"""Clips viewers already made, placed on the VOD timeline.

Kick: its public site API (no key). Twitch: the Helix API, only when TWITCH_CLIENT_ID and
TWITCH_CLIENT_SECRET are set (a free app at dev.twitch.tv).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
    "Accept": "application/json",
}


@dataclass(frozen=True)
class ClipMarker:
    t: float          # where the clip starts, seconds into the VOD
    duration: float
    views: int
    title: str = ""   # what the viewer called it: often says what happened ("bro smashed the monitor")


def _parse_time(value: str) -> datetime:
    value = value.strip().replace(" ", "T")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if re.match(r".*T\d\d:\d\d:\d\d$", value):
        value += "+00:00"
    return datetime.fromisoformat(value)


# ---- Kick ---------------------------------------------------------------------
async def kick_vod(client: httpx.AsyncClient, uuid: str) -> dict[str, Any]:
    resp = await client.get(f"https://kick.com/api/v1/video/{uuid}", headers=BROWSER_HEADERS)
    resp.raise_for_status()
    stream = resp.json().get("livestream") or {}
    return {
        "livestream_id": str(stream.get("id")),
        "start": _parse_time(stream["start_time"]),
        "duration": (stream.get("duration") or 0) / 1000,
        "slug": (stream.get("channel") or {}).get("slug"),
        "is_live": bool(stream.get("is_live")),
    }


async def kick_clip_markers(client: httpx.AsyncClient, slug: str, livestream_id: str,
                            start: datetime, max_pages: int = 60) -> list[ClipMarker]:
    """Kick clips record when they were made, not where; a clip covers the `duration`
    seconds before its creation time."""
    markers, cursor = [], None
    for _ in range(max_pages):
        url = f"https://kick.com/api/v2/channels/{slug}/clips?sort=date&time=all"
        params = {"cursor": cursor} if cursor else None
        resp = await client.get(url, params=params, headers=BROWSER_HEADERS)
        resp.raise_for_status()
        data = resp.json()
        clips = data.get("clips") or []
        for clip in clips:
            if str(clip.get("livestream_id")) != livestream_id:
                continue
            made = (_parse_time(clip["created_at"]) - start).total_seconds()
            duration = float(clip.get("duration") or 30)
            markers.append(ClipMarker(max(0.0, made - duration), duration,
                                      int(clip.get("view_count") or clip.get("views") or 0),
                                      str(clip.get("title") or "").strip()))
        cursor = data.get("nextCursor")
        if not clips or not cursor or _parse_time(clips[-1]["created_at"]) < start:
            break
        if isinstance(cursor, dict):
            cursor = json.dumps(cursor, separators=(",", ":"))
    return markers


# ---- Twitch -------------------------------------------------------------------
def _twitch_duration(value: str) -> float:
    total = 0
    for amount, unit in re.findall(r"(\d+)([hms])", value or ""):
        total += int(amount) * {"h": 3600, "m": 60, "s": 1}[unit]
    return float(total)


async def twitch_clip_markers(client: httpx.AsyncClient, vod_id: str, client_id: str,
                              client_secret: str, max_pages: int = 5) -> list[ClipMarker]:
    token = await client.post("https://id.twitch.tv/oauth2/token", params={
        "client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"})
    token.raise_for_status()
    headers = {"Client-Id": client_id, "Authorization": f"Bearer {token.json()['access_token']}"}

    video = await client.get("https://api.twitch.tv/helix/videos", params={"id": vod_id}, headers=headers)
    video.raise_for_status()
    items = video.json().get("data") or []
    if not items:
        return []
    info = items[0]
    created = _parse_time(info["created_at"])
    ended = created + timedelta(seconds=_twitch_duration(info.get("duration", "")) + 3600)

    markers, cursor = [], None
    for _ in range(max_pages):
        params = {"broadcaster_id": info["user_id"], "first": 100,
                  "started_at": created.isoformat().replace("+00:00", "Z"),
                  "ended_at": ended.isoformat().replace("+00:00", "Z")}
        if cursor:
            params["after"] = cursor
        resp = await client.get("https://api.twitch.tv/helix/clips", params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()
        for clip in body.get("data") or []:
            if str(clip.get("video_id")) == str(vod_id) and clip.get("vod_offset") is not None:
                markers.append(ClipMarker(float(clip["vod_offset"]), float(clip.get("duration") or 30),
                                          int(clip.get("view_count") or 0), str(clip.get("title") or "").strip()))
        cursor = (body.get("pagination") or {}).get("cursor")
        if not cursor:
            break
    return markers
