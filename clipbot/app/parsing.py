"""Turn a Telegram message into a command.

Accepted forms (anything else gets the help text):
    /start, /help
    /status
    /scan <url>                   find the best moments of a VOD
    /watch <channel>, /unwatch <channel>, /watching
    /cleanup                      offer to delete the files of finished jobs
    /templates                    list the clip templates
    <url>                         clip the whole video
    <url> 1:02:00-1:12:30         clip only that part (also 62:00-72:30, 1h2m-1h12m)
    <url> clips=5                 how many clips to make (1-15)
    <url> t=meme                  which template (captions, music, sound effects); /templates lists them
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Optional

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TIME = r"(?:\d{1,2}:)?\d{1,2}:\d{2}|\d+h(?:\d{1,2}m)?(?:\d{1,2}s)?|\d+m(?:\d{1,2}s)?|\d+s"
RANGE_RE = re.compile(
    rf"(?<![\w:])({_TIME})\s*(?:-|–|—|to)\s*({_TIME})(?![\w:])", re.IGNORECASE)
CLIPS_RE = re.compile(r"\b(?:clips?|n)\s*[=:]\s*(\d{1,2})\b", re.IGNORECASE)
TEMPLATE_RE = re.compile(r"\b(?:t|template|style)\s*[=:]\s*([\w-]{1,40})", re.IGNORECASE)
_HMS_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", re.IGNORECASE)

Kind = Literal["help", "status", "clip", "scan", "watch", "unwatch", "watching", "cleanup", "templates",
               "invalid"]


@dataclass(frozen=True)
class Command:
    kind: Kind
    url: Optional[str] = None
    start: Optional[float] = None
    end: Optional[float] = None
    target_clips: Optional[int] = None
    error: Optional[str] = None
    arg: Optional[str] = None
    template: Optional[str] = None


def parse_timestamp(value: str) -> float:
    """'1:02:03' -> 3723, '62:03' -> 3723, '1h2m3s' -> 3723, '90m' -> 5400."""
    value = value.strip().lower()
    if ":" in value:
        seconds = 0
        for part in value.split(":"):
            seconds = seconds * 60 + int(part)
        return float(seconds)
    match = _HMS_RE.match(value)
    if not match or not any(match.groups()):
        raise ValueError(f"not a timestamp: {value!r}")
    h, m, s = (int(g) if g else 0 for g in match.groups())
    return float(h * 3600 + m * 60 + s)


def format_timestamp(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _clean_url(url: str) -> str:
    # Telegram users paste links followed by punctuation; strip what can't end a URL.
    return url.rstrip(").,;!?]>")


def parse_message(text: str, max_range_minutes: int = 90, min_range_seconds: int = 60) -> Command:
    text = (text or "").strip()
    if not text:
        return Command("help")

    first = text.split()[0].lower()
    if first.startswith("/"):
        command = first.split("@", 1)[0]  # /status@MyBot in group chats
        arg = text[len(first):].strip()
        if command == "/status":
            return Command("status")
        if command in ("/start", "/help"):
            return Command("help")
        if command == "/watching":
            return Command("watching")
        if command in ("/cleanup", "/storage"):
            return Command("cleanup")
        if command == "/templates":
            return Command("templates")
        if command in ("/watch", "/unwatch"):
            if not arg:
                return Command("invalid", error=f"Usage: {command} https://twitch.tv/<channel> "
                                                "(or kick.com/<channel>, youtube.com/@<handle>)")
            return Command(command[1:], arg=arg)
        if command == "/scan":
            match = URL_RE.search(arg)
            if not match:
                return Command("invalid", error="Usage: /scan <VOD link>")
            return Command("scan", url=_clean_url(match.group(0)))

    url_match = URL_RE.search(text)
    if not url_match:
        return Command("help")
    url = _clean_url(url_match.group(0))
    rest = (text[:url_match.start()] + " " + text[url_match.end():]).strip()

    template = None
    template_match = TEMPLATE_RE.search(rest)
    if template_match:
        template = template_match.group(1).lower()
        rest = rest[:template_match.start()] + rest[template_match.end():]

    target = None
    clips_match = CLIPS_RE.search(rest)
    if clips_match:
        target = int(clips_match.group(1))
        if not 1 <= target <= 15:
            return Command("invalid", url=url, error="clips= must be between 1 and 15.")
        rest = rest[:clips_match.start()] + rest[clips_match.end():]

    start = end = None
    range_match = RANGE_RE.search(rest)
    if range_match:
        start = parse_timestamp(range_match.group(1))
        end = parse_timestamp(range_match.group(2))
        if end <= start:
            return Command("invalid", url=url,
                           error="The range ends before it starts. Use start-end, e.g. 1:02:00-1:12:00.")
        length = end - start
        if length < min_range_seconds:
            return Command("invalid", url=url,
                           error=f"The range is {format_timestamp(length)} long; make it at least "
                                 f"{format_timestamp(min_range_seconds)} so there is something to clip.")
        if length > max_range_minutes * 60:
            return Command("invalid", url=url,
                           error=f"The range is {format_timestamp(length)} long; the limit is "
                                 f"{max_range_minutes} minutes (MAX_RANGE_MINUTES).")

    return Command("clip", url=url, start=start, end=end, target_clips=target, template=template)
