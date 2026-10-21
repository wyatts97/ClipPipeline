"""Recognise VOD and channel links, list a channel's newest VODs, build timestamped links."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx

from .clips_api import BROWSER_HEADERS, _parse_time
from .media import run

_TWITCH_VOD = re.compile(r"twitch\.tv/(?:videos/|[A-Za-z0-9_]+/v/)(\d+)", re.I)
_KICK_VOD = re.compile(r"kick\.com/(?:[A-Za-z0-9_-]+/videos|video)/([0-9a-f]{8}-[0-9a-f-]{27})", re.I)
_YOUTUBE_VOD = re.compile(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|shorts/)|youtu\.be/)([\w-]{11})", re.I)

_TWITCH_CHANNEL = re.compile(r"^(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{2,25})/?(?:\?.*)?$", re.I)
_KICK_CHANNEL = re.compile(r"^(?:https?://)?(?:www\.)?kick\.com/([A-Za-z0-9_-]{2,25})/?(?:\?.*)?$", re.I)
_YOUTUBE_CHANNEL = re.compile(
    r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/(@[\w.\-]+|channel/UC[\w-]{22})(?:/\w*)?/?(?:\?.*)?$", re.I)
_SHORT_CHANNEL = re.compile(r"^(twitch|kick|youtube|yt)[:/ ]\s*(@?[\w.\-]+)$", re.I)
_RESERVED = {"videos", "video", "directory", "search", "settings", "categories", "browse", "clips"}


@dataclass(frozen=True)
class Vod:
    platform: str           # twitch | youtube | kick | other
    vod_id: Optional[str]
    url: str


@dataclass(frozen=True)
class Channel:
    platform: str           # twitch | youtube | kick
    name: str               # login, @handle / channel/UC..., or slug

    @property
    def label(self) -> str:
        return f"{self.platform}/{self.name}"


def detect_vod(url: str) -> Vod:
    for platform, pattern in (("twitch", _TWITCH_VOD), ("kick", _KICK_VOD), ("youtube", _YOUTUBE_VOD)):
        match = pattern.search(url)
        if match:
            return Vod(platform, match.group(1), url)
    return Vod("other", None, url)


def detect_channel(text: str) -> Optional[Channel]:
    text = text.strip()
    short = _SHORT_CHANNEL.match(text)
    if short:
        platform = "youtube" if short.group(1).lower() in ("youtube", "yt") else short.group(1).lower()
        name = short.group(2)
        if platform == "youtube" and not name.startswith("@"):
            name = "@" + name
        return Channel(platform, name if platform == "youtube" else name.lower())
    for platform, pattern in (("twitch", _TWITCH_CHANNEL), ("kick", _KICK_CHANNEL)):
        match = pattern.match(text)
        if match and match.group(1).lower() not in _RESERVED:
            return Channel(platform, match.group(1).lower())
    match = _YOUTUBE_CHANNEL.match(text)
    if match:
        return Channel("youtube", match.group(1))
    return None


def timestamp_link(vod: Vod, seconds: float) -> str:
    s = max(0, int(seconds))
    if vod.platform == "twitch":
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"https://www.twitch.tv/videos/{vod.vod_id}?t={h}h{m}m{sec}s"
    if vod.platform == "youtube":
        return f"https://youtu.be/{vod.vod_id}?t={s}"
    if vod.platform == "kick":
        return f"{vod.url.split('?')[0]}?t={s}"
    return vod.url


@dataclass(frozen=True)
class LatestVod:
    vod_id: str
    url: str
    duration: Optional[float]
    live: bool
    title: Optional[str] = None
    published: Optional[float] = None    # unix time, when the platform says


def channel_url(channel: Channel) -> str:
    if channel.platform == "twitch":
        return f"https://www.twitch.tv/{channel.name}"
    if channel.platform == "kick":
        return f"https://kick.com/{channel.name}"
    return f"https://www.youtube.com/{channel.name}"


def _published(entry: dict) -> Optional[float]:
    for key in ("timestamp", "release_timestamp"):
        if isinstance(entry.get(key), (int, float)):
            return float(entry[key])
    day = str(entry.get("upload_date") or "")
    if re.fullmatch(r"\d{8}", day):
        return datetime(int(day[:4]), int(day[4:6]), int(day[6:]), tzinfo=timezone.utc).timestamp()
    return None


async def list_vods(channel: Channel, client: Optional[httpx.AsyncClient] = None,
                    limit: int = 8) -> list[LatestVod]:
    """The channel's newest past broadcasts, newest first (the first may still be live)."""
    if channel.platform == "kick":
        own = client is None
        client = client or httpx.AsyncClient(timeout=30)
        try:
            resp = await client.get(f"https://kick.com/api/v2/channels/{channel.name}/videos",
                                    headers=BROWSER_HEADERS)
            resp.raise_for_status()
            items = resp.json()
        finally:
            if own:
                await client.aclose()
        out = []
        for item in items or []:
            uuid = (item.get("video") or {}).get("uuid")
            if not uuid:
                continue
            try:
                published = _parse_time(str(item.get("created_at"))).timestamp() if item.get("created_at") else None
            except ValueError:
                published = None
            out.append(LatestVod(uuid, f"https://kick.com/{channel.name}/videos/{uuid}",
                                 (item.get("duration") or 0) / 1000 or None, bool(item.get("is_live")),
                                 item.get("session_title") or None, published))
            if len(out) >= limit:
                break
        return out

    listing = (f"https://www.twitch.tv/{channel.name}/videos?filter=archives&sort=time"
               if channel.platform == "twitch" else f"https://www.youtube.com/{channel.name}/streams")
    out = await run(["yt-dlp", "--flat-playlist", "-J", "-I", f"1:{limit + 2}", "--no-warnings", listing],
                    timeout=180)
    vods = []
    for entry in json.loads(out).get("entries") or []:
        status = entry.get("live_status")
        if status == "is_upcoming":  # a scheduled stream, not a VOD yet
            continue
        vod_id = str(entry.get("id") or "").lstrip("v") if channel.platform == "twitch" else str(entry.get("id"))
        url = entry.get("url") or (f"https://www.twitch.tv/videos/{vod_id}" if channel.platform == "twitch"
                                   else f"https://www.youtube.com/watch?v={vod_id}")
        # post_live: ended but YouTube is still processing it; treat it as not finished yet.
        vods.append(LatestVod(vod_id, url, entry.get("duration"), status in ("is_live", "post_live"),
                              entry.get("title") or None, _published(entry)))
        if len(vods) >= limit:
            break
    return vods


async def latest_vod(channel: Channel, client: Optional[httpx.AsyncClient] = None) -> Optional[LatestVod]:
    """The channel's newest past broadcast (it may still be live and growing)."""
    vods = await list_vods(channel, client, limit=1)
    return vods[0] if vods else None
