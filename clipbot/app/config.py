"""Settings, read once from the environment (see .env.example at the repo root)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _chat_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    openshorts_url: str
    openshorts_output_dir: Path
    notify_url: str
    notify_token: str
    self_url: str
    allowed_chat_ids: frozenset[int]
    target_clips: int
    clip_min_seconds: int
    clip_max_seconds: int
    layouts: str
    auto_hook: bool
    captions: bool
    poll_seconds: int
    job_timeout_minutes: int
    max_range_minutes: int
    preview_max_mb: int
    preview_height: int
    # /scan and /watch
    scan_top: int = 5
    scan_before_seconds: int = 90
    scan_after_seconds: int = 45
    scan_clips_per_moment: int = 1
    scan_audio: bool = True
    scan_hype_words: frozenset[str] = frozenset()
    watch_interval_minutes: int = 15
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_downloader: str = "TwitchDownloaderCLI"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clipbot.sqlite3"

    @property
    def cookies_file(self) -> Path:
        return self.data_dir / "cookies.txt"

    def folder(self, name: str) -> Path:
        path = self.data_dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("CLIPBOT_DATA_DIR", "/data")),
            openshorts_url=os.environ.get("OPENSHORTS_URL", "http://openshorts:8000").rstrip("/"),
            openshorts_output_dir=Path(os.environ.get("OPENSHORTS_OUTPUT_DIR", "/openshorts-output")),
            notify_url=os.environ.get("N8N_EVENTS_URL", "http://n8n:5678/webhook/clipbot-events"),
            notify_token=os.environ.get("CLIPBOT_TOKEN", ""),
            self_url=os.environ.get("CLIPBOT_SELF_URL", "http://clipbot:8080").rstrip("/"),
            allowed_chat_ids=_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", "")),
            target_clips=_int("TARGET_CLIPS", 3),
            clip_min_seconds=_int("CLIP_MIN_SECONDS", 15),
            clip_max_seconds=_int("CLIP_MAX_SECONDS", 60),
            layouts=os.environ.get("OPENSHORTS_LAYOUTS", "auto"),
            auto_hook=_bool("AUTO_HOOK", True),
            captions=_bool("CAPTIONS", True),
            poll_seconds=_int("POLL_SECONDS", 15),
            job_timeout_minutes=_int("JOB_TIMEOUT_MINUTES", 360),
            max_range_minutes=_int("MAX_RANGE_MINUTES", 90),
            preview_max_mb=_int("PREVIEW_MAX_MB", 45),
            preview_height=_int("PREVIEW_HEIGHT", 960),
            scan_top=_int("SCAN_TOP", 5),
            scan_before_seconds=_int("SCAN_BEFORE_SECONDS", 90),
            scan_after_seconds=_int("SCAN_AFTER_SECONDS", 45),
            scan_clips_per_moment=_int("SCAN_CLIPS_PER_MOMENT", 1),
            scan_audio=_bool("SCAN_AUDIO", True),
            scan_hype_words=frozenset(w.strip().lower() for w in
                                      os.environ.get("SCAN_HYPE_WORDS", "").split(",") if w.strip()),
            watch_interval_minutes=_int("WATCH_INTERVAL_MINUTES", 15),
            twitch_client_id=os.environ.get("TWITCH_CLIENT_ID", "").strip(),
            twitch_client_secret=os.environ.get("TWITCH_CLIENT_SECRET", "").strip(),
            twitch_downloader=os.environ.get("TWITCH_DOWNLOADER", "TwitchDownloaderCLI"),
        )
