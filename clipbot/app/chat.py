"""Chat replays: Twitch (TwitchDownloaderCLI) and YouTube live-stream replays (yt-dlp)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .media import MediaError, run

log = logging.getLogger("clipbot.chat")


@dataclass(frozen=True)
class ChatMessage:
    t: float      # seconds from the start of the VOD
    author: str
    text: str


def parse_twitch_downloader(path: Path) -> list[ChatMessage]:
    """TwitchDownloaderCLI `chatdownload` JSON -> messages."""
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = []
    for c in data.get("comments") or []:
        body = (c.get("message") or {}).get("body") or ""
        author = (c.get("commenter") or {}).get("_id") or (c.get("commenter") or {}).get("name") or "?"
        messages.append(ChatMessage(float(c.get("content_offset_seconds") or 0), str(author), body))
    return messages


def _youtube_text(renderer: dict) -> str:
    parts = []
    for run_ in (renderer.get("message") or {}).get("runs") or []:
        if "text" in run_:
            parts.append(run_["text"])
        elif "emoji" in run_:
            # Unicode emoji carry the character as emojiId; channel emotes only have a :shortcut:.
            emoji = run_["emoji"]
            shortcut = (emoji.get("shortcuts") or [""])[0]
            parts.append(shortcut if emoji.get("isCustomEmoji") else (emoji.get("emojiId") or shortcut))
    return "".join(parts)


def parse_youtube_live_chat(path: Path) -> list[ChatMessage]:
    """yt-dlp `live_chat` JSON lines -> messages (text messages and Super Chats)."""
    messages = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            replay = obj.get("replayChatItemAction") or {}
            try:
                t = int(replay.get("videoOffsetTimeMsec")) / 1000
            except (TypeError, ValueError):
                continue
            for action in replay.get("actions") or []:
                item = (action.get("addChatItemAction") or {}).get("item") or {}
                renderer = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not renderer:
                    continue
                author = renderer.get("authorExternalChannelId") or \
                    (renderer.get("authorName") or {}).get("simpleText") or "?"
                messages.append(ChatMessage(t, author, _youtube_text(renderer)))
    return messages


async def fetch_twitch_chat(vod_id: str, work_dir: Path, binary: str = "TwitchDownloaderCLI") -> list[ChatMessage]:
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / "chat.json"
    await run([binary, "chatdownload", "--id", vod_id, "-o", str(out), "--collision", "Overwrite",
               "--banner", "false", "--log-level", "Error", "--temp-path", str(work_dir)], timeout=3600)
    if not out.is_file():
        raise MediaError("TwitchDownloaderCLI wrote no chat file")
    return parse_twitch_downloader(out)


async def fetch_youtube_chat(url: str, work_dir: Path, cookies: Optional[Path] = None) -> list[ChatMessage]:
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["yt-dlp", "--no-playlist", "--no-progress", "--quiet", "--skip-download", "--write-subs",
           "--sub-langs", "live_chat", "-o", str(work_dir / "chat.%(ext)s")]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    await run(cmd + [url], timeout=3600)
    files = list(work_dir.glob("chat*.live_chat.json"))
    if not files:
        raise MediaError("yt-dlp found no chat replay for this video")
    return parse_youtube_live_chat(files[0])
