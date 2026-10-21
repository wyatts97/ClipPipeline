"""SQLite state: jobs, clips, scans, watches, scheduled posts, the dashboard's log and an outbox of
events for n8n.

Everything survives a restart: unfinished jobs are picked up again and
undelivered events are retried.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    range_start REAL,
    range_end REAL,
    target_clips INTEGER NOT NULL,
    status TEXT NOT NULL,
    openshorts_job TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    retry_of TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    idx INTEGER NOT NULL,
    title TEXT,
    hook TEXT,
    meta TEXT,
    duration REAL,
    status TEXT NOT NULL,
    path TEXT NOT NULL,
    preview_path TEXT,
    decided REAL
);
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT,
    duration REAL,
    signals TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS moments (
    id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL REFERENCES scans(id),
    rank INTEGER NOT NULL,
    peak REAL NOT NULL,
    start REAL NOT NULL,
    end REAL NOT NULL,
    score REAL NOT NULL,
    job_id TEXT,
    info TEXT
);
CREATE TABLE IF NOT EXISTS watches (
    id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    channel TEXT NOT NULL,
    last_vod_id TEXT,
    pending_vod_id TEXT,
    pending_duration REAL,
    created REAL NOT NULL,
    UNIQUE (chat_id, platform, channel)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL,
    dead INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL,
    job_id TEXT,
    scan_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE TABLE IF NOT EXISTS scan_timelines (
    scan_id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS publish_queue (
    clip_id TEXT PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    publish_at REAL,
    now INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    clip_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    channel_name TEXT,
    status TEXT NOT NULL,
    publish_at REAL NOT NULL,
    media_id TEXT,
    media_path TEXT,
    url TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_clip ON posts(clip_id);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS vods (
    watch_id TEXT NOT NULL,
    vod_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    duration REAL,
    published REAL,
    live INTEGER NOT NULL DEFAULT 0,
    seen REAL NOT NULL,
    PRIMARY KEY (watch_id, vod_id)
);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# Columns added after the first release; created on older databases at startup.
MIGRATIONS = (("jobs", "retry_of", "TEXT"), ("moments", "info", "TEXT"), ("jobs", "template", "TEXT"),
              ("clips", "template", "TEXT"), ("clips", "sfx", "TEXT"), ("clips", "words", "TEXT"),
              ("clips", "splits", "TEXT"), ("clips", "render_state", "TEXT"), ("clips", "render_error", "TEXT"),
              ("clips", "edit", "TEXT"), ("scans", "watch_id", "TEXT"),
              ("watches", "auto_scan", "INTEGER NOT NULL DEFAULT 1"),
              ("watches", "auto_clip", "INTEGER NOT NULL DEFAULT 0"),
              ("watches", "template", "TEXT"), ("watches", "checked", "REAL"), ("watches", "check_error", "TEXT"))
LOG_KEEP_DAYS = 14
LOG_KEEP_ROWS = 20000

# queued -> downloading -> submitting -> processing -> done | failed
ACTIVE_STATUSES = ("queued", "downloading", "submitting", "processing")


@dataclass
class Job:
    id: str
    chat_id: int
    source_url: str
    range_start: Optional[float]
    range_end: Optional[float]
    target_clips: int
    status: str
    openshorts_job: Optional[str]
    error: Optional[str]
    created: float
    updated: float
    retry_of: Optional[str] = None
    template: Optional[str] = None     # template id picked for the job; None = the default


@dataclass
class Clip:
    id: str
    job_id: str
    idx: int
    title: Optional[str]
    hook: Optional[str]
    meta: dict[str, Any]
    duration: Optional[float]
    status: str
    path: str
    preview_path: Optional[str]
    decided: Optional[float]
    template: Optional[str] = None       # template the current render used
    sfx: Optional[str] = None            # JSON [{sound, t, volume}]: effects placed by hand
    words: Optional[str] = None          # JSON [{w, s, e}]: the clip's words, for captions
    splits: Optional[str] = None         # JSON [[start, end]]: two-speaker stretches (captions on the seam)
    render_state: Optional[str] = None   # rendering | failed | None
    render_error: Optional[str] = None
    edit: Optional[str] = None           # JSON: the editor's cuts and zooms (see editing.py)

    def manual_sfx(self) -> list[dict[str, Any]]:
        return json.loads(self.sfx) if self.sfx else []

    def word_list(self) -> list[dict[str, Any]]:
        return json.loads(self.words) if self.words else []

    def split_list(self) -> list[tuple[float, float]]:
        return [tuple(r) for r in json.loads(self.splits)] if self.splits else []


@dataclass
class Scan:
    id: str
    chat_id: int
    source_url: str
    status: str            # queued -> running -> done | failed
    title: Optional[str]
    duration: Optional[float]
    signals: Optional[str]
    error: Optional[str]
    created: float
    updated: float
    watch_id: Optional[str] = None   # the streamer whose VOD this is (auto-clip settings)


@dataclass
class MomentRow:
    id: str
    scan_id: str
    rank: int
    peak: float
    start: float
    end: float
    score: float
    job_id: Optional[str]
    info: Optional[str] = None   # JSON: chat_ratio, loud, clips, top_tokens, ...

    @property
    def details(self) -> dict[str, Any]:
        return json.loads(self.info) if self.info else {}


@dataclass
class PublishRequest:
    clip_id: str
    chat_id: int
    status: str            # waiting -> scheduled | failed | cancelled
    publish_at: Optional[float]   # a time picked in the dashboard; None = the next free slot
    now: int               # 1 = post right away
    error: Optional[str]
    attempts: int
    next_try: float
    created: float


@dataclass
class Post:
    id: str                # Postiz post id; one per channel
    clip_id: str
    channel_id: str
    platform: str          # youtube | tiktok | instagram
    channel_name: Optional[str]
    status: str            # scheduled -> published | failed | cancelled
    publish_at: float
    media_id: Optional[str]
    media_path: Optional[str]
    url: Optional[str]
    error: Optional[str]
    created: float
    updated: float


@dataclass
class Watch:
    id: str
    chat_id: int
    platform: str
    channel: str
    last_vod_id: Optional[str]
    pending_vod_id: Optional[str]
    pending_duration: Optional[float]
    created: float
    auto_scan: int = 1                 # scan each new VOD when the stream ends
    auto_clip: int = 0                 # then clip the top N moments by itself (0 = just send them)
    template: Optional[str] = None     # template for those clips (None = the default)
    checked: Optional[float] = None    # last time its VOD list was read
    check_error: Optional[str] = None


@dataclass
class VodRow:
    watch_id: str
    vod_id: str
    url: str
    title: Optional[str]
    duration: Optional[float]
    published: Optional[float]
    live: int
    seen: float


def new_id() -> str:
    # Short enough for Telegram's 64-byte callback_data, long enough not to collide.
    return secrets.token_hex(5)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            for table, column, decl in MIGRATIONS:
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._log_inserts = 0

    def close(self) -> None:
        self._db.close()

    # ---- jobs ------------------------------------------------------------
    def create_job(self, chat_id: int, source_url: str, range_start: Optional[float],
                   range_end: Optional[float], target_clips: int,
                   retry_of: Optional[str] = None, template: Optional[str] = None) -> Job:
        now = time.time()
        job = Job(new_id(), chat_id, source_url, range_start, range_end, target_clips,
                  "queued", None, None, now, now, retry_of, template)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, chat_id, source_url, range_start, range_end, target_clips, "
                "status, openshorts_job, error, created, updated, retry_of, template) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.chat_id, job.source_url, job.range_start, job.range_end,
                 job.target_clips, job.status, None, None, now, now, retry_of, template))
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    def update_job(self, job_id: str, **fields: Any) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))

    def jobs_with_status(self, *statuses: str) -> list[Job]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY created", statuses).fetchall()
        return [Job(**dict(r)) for r in rows]

    def list_jobs(self, status: Optional[str] = None, limit: int = 100) -> list[Job]:
        sql, args = "SELECT * FROM jobs", []
        if status:
            sql, args = sql + " WHERE status=?", [status]
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created DESC LIMIT ?", (*args, limit)).fetchall()
        return [Job(**dict(r)) for r in rows]

    def count_by_status(self, table: str, since: float = 0) -> dict[str, int]:
        assert table in ("jobs", "scans", "clips")
        ts = "decided" if table == "clips" else "created"
        with self._lock:
            rows = self._db.execute(
                f"SELECT status, COUNT(*) AS n FROM {table} WHERE COALESCE({ts}, 0) >= ? GROUP BY status",
                (since,)).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def retried(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE retry_of=? ORDER BY created DESC LIMIT 1",
                                   (job_id,)).fetchone()
        return Job(**dict(row)) if row else None

    def recent_jobs(self, chat_id: int, limit: int = 5) -> list[Job]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE chat_id=? ORDER BY created DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        return [Job(**dict(r)) for r in rows]

    def queue_position(self, job_id: str) -> int:
        """1-based position among jobs not yet handed to OpenShorts."""
        waiting = [j.id for j in self.jobs_with_status("queued", "downloading", "submitting")]
        return waiting.index(job_id) + 1 if job_id in waiting else 0

    # ---- clips -----------------------------------------------------------
    def add_clip(self, job_id: str, idx: int, title: Optional[str], hook: Optional[str],
                 meta: dict[str, Any], duration: Optional[float], path: str,
                 preview_path: Optional[str]) -> Clip:
        clip = Clip(new_id(), job_id, idx, title, hook, meta, duration, "pending", path,
                    preview_path, None)
        with self._lock:
            self._db.execute(
                "INSERT INTO clips (id, job_id, idx, title, hook, meta, duration, status, path, preview_path, "
                "decided) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (clip.id, job_id, idx, title, hook, json.dumps(meta), duration, "pending",
                 path, preview_path, None))
        return clip

    def update_clip(self, clip_id: str, **fields: Any) -> None:
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE clips SET {cols} WHERE id=?", (*fields.values(), clip_id))

    def get_clip(self, clip_id: str) -> Optional[Clip]:
        with self._lock:
            row = self._db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["meta"] = json.loads(data["meta"] or "{}")
        return Clip(**data)

    def list_clips(self, status: Optional[str] = None, limit: int = 200) -> list[Clip]:
        sql, args = "SELECT id FROM clips", []
        if status:
            sql, args = sql + " WHERE status=?", [status]
        order = (" ORDER BY rowid ASC" if status == "pending"
                 else " ORDER BY COALESCE(decided, 0) DESC, rowid DESC")
        with self._lock:
            ids = [r["id"] for r in self._db.execute(sql + order + " LIMIT ?", (*args, limit))]
        return [c for c in (self.get_clip(i) for i in ids) if c]

    def update_clip_text(self, clip_id: str, title: Optional[str], hook: Optional[str],
                         meta: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("UPDATE clips SET title=?, hook=?, meta=? WHERE id=?",
                             (title, hook, json.dumps(meta), clip_id))

    def clips_for_job(self, job_id: str) -> list[Clip]:
        with self._lock:
            ids = [r["id"] for r in self._db.execute(
                "SELECT id FROM clips WHERE job_id=? ORDER BY idx", (job_id,))]
        return [c for c in (self.get_clip(i) for i in ids) if c]

    def decide_clip(self, clip_id: str, status: str, path: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE clips SET status=?, path=?, preview_path=NULL, decided=? WHERE id=?",
                (status, path, time.time(), clip_id))

    # ---- scans and their moments ----------------------------------------
    def create_scan(self, chat_id: int, source_url: str, watch_id: Optional[str] = None) -> Scan:
        now = time.time()
        scan = Scan(new_id(), chat_id, source_url, "queued", None, None, None, None, now, now, watch_id)
        with self._lock:
            self._db.execute(
                "INSERT INTO scans (id, chat_id, source_url, status, title, duration, signals, error, created, "
                "updated, watch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (scan.id, chat_id, source_url, "queued", None, None, None, None, now, now, watch_id))
        return scan

    def scans_for_url(self, source_url: str) -> list[Scan]:
        """Scans of this VOD, newest first."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM scans WHERE source_url=? ORDER BY created DESC",
                                    (source_url,)).fetchall()
        return [Scan(**dict(r)) for r in rows]

    def get_scan(self, scan_id: str) -> Optional[Scan]:
        with self._lock:
            row = self._db.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        return Scan(**dict(row)) if row else None

    def update_scan(self, scan_id: str, **fields: Any) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), scan_id))

    def scans_with_status(self, *statuses: str) -> list[Scan]:
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM scans WHERE status IN ({marks}) ORDER BY created", statuses).fetchall()
        return [Scan(**dict(r)) for r in rows]

    def recent_scans(self, chat_id: int, limit: int = 3) -> list[Scan]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM scans WHERE chat_id=? ORDER BY created DESC LIMIT ?",
                (chat_id, limit)).fetchall()
        return [Scan(**dict(r)) for r in rows]

    def list_scans(self, limit: int = 100) -> list[Scan]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM scans ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [Scan(**dict(r)) for r in rows]

    def add_moment(self, scan_id: str, rank: int, peak: float, start: float, end: float,
                   score: float, info: Optional[dict[str, Any]] = None) -> MomentRow:
        info_json = json.dumps(info) if info else None
        row = MomentRow(new_id(), scan_id, rank, peak, start, end, score, None, info_json)
        with self._lock:
            self._db.execute(
                "INSERT INTO moments (id, scan_id, rank, peak, start, end, score, job_id, info) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (row.id, scan_id, rank, peak, start, end, score, None, info_json))
        return row

    def save_timeline(self, scan_id: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO scan_timelines VALUES (?, ?)",
                             (scan_id, json.dumps(data)))

    def get_timeline(self, scan_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._db.execute("SELECT data FROM scan_timelines WHERE scan_id=?", (scan_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def get_moment(self, moment_id: str) -> Optional[MomentRow]:
        with self._lock:
            row = self._db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return MomentRow(**dict(row)) if row else None

    def moments_for_scan(self, scan_id: str) -> list[MomentRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM moments WHERE scan_id=? ORDER BY rank",
                                    (scan_id,)).fetchall()
        return [MomentRow(**dict(r)) for r in rows]

    def moment_for_job(self, job_id: str) -> Optional[MomentRow]:
        with self._lock:
            row = self._db.execute("SELECT * FROM moments WHERE job_id=? LIMIT 1", (job_id,)).fetchone()
        return MomentRow(**dict(row)) if row else None

    def claim_moment(self, moment_id: str, job_id: str) -> bool:
        """Attach a job to a moment unless one is already attached (double taps)."""
        with self._lock:
            cur = self._db.execute("UPDATE moments SET job_id=? WHERE id=? AND job_id IS NULL",
                                   (job_id, moment_id))
        return cur.rowcount == 1

    # ---- watched channels --------------------------------------------------
    def add_watch(self, chat_id: int, platform: str, channel: str,
                  last_vod_id: Optional[str]) -> Optional[Watch]:
        """None when this chat already watches the channel."""
        watch = Watch(new_id(), chat_id, platform, channel, last_vod_id, None, None, time.time())
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO watches (id, chat_id, platform, channel, last_vod_id, pending_vod_id, "
                    "pending_duration, created) VALUES (?,?,?,?,?,?,?,?)",
                    (watch.id, chat_id, platform, channel, last_vod_id, None, None, watch.created))
            except sqlite3.IntegrityError:
                return None
        return watch

    def get_watch(self, watch_id: Optional[str]) -> Optional[Watch]:
        if not watch_id:
            return None
        with self._lock:
            row = self._db.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
        return Watch(**dict(row)) if row else None

    def delete_watch(self, watch_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM watches WHERE id=?", (watch_id,))
            self._db.execute("DELETE FROM vods WHERE watch_id=?", (watch_id,))
        return cur.rowcount > 0

    def save_vods(self, watch_id: str, vods: list[Any]) -> None:
        """Remember a channel's VOD list (LatestVod items); keeps when each was first seen."""
        now = time.time()
        with self._lock:
            for i, v in enumerate(vods):   # newest first: keep that order among same-batch rows
                self._db.execute(
                    "INSERT INTO vods (watch_id, vod_id, url, title, duration, published, live, seen) "
                    "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(watch_id, vod_id) DO UPDATE SET url=excluded.url, "
                    "title=COALESCE(excluded.title, title), duration=excluded.duration, "
                    "published=COALESCE(excluded.published, published), live=excluded.live",
                    (watch_id, v.vod_id, v.url, v.title, v.duration, v.published, int(v.live), now - i * 0.001))

    def vods_for_watch(self, watch_id: str, limit: int = 20) -> list[VodRow]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM vods WHERE watch_id=? ORDER BY COALESCE(published, seen) DESC, seen DESC LIMIT ?",
                (watch_id, limit)).fetchall()
        return [VodRow(**dict(r)) for r in rows]

    def remove_watch(self, chat_id: int, platform: str, channel: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM watches WHERE chat_id=? AND platform=? AND channel=?",
                                   (chat_id, platform, channel))
        return cur.rowcount > 0

    def watches(self, chat_id: Optional[int] = None) -> list[Watch]:
        with self._lock:
            if chat_id is None:
                rows = self._db.execute("SELECT * FROM watches ORDER BY created").fetchall()
            else:
                rows = self._db.execute("SELECT * FROM watches WHERE chat_id=? ORDER BY created",
                                        (chat_id,)).fetchall()
        return [Watch(**dict(r)) for r in rows]

    def update_watch(self, watch_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE watches SET {cols} WHERE id=?", (*fields.values(), watch_id))

    # ---- templates and app settings -------------------------------------------
    def seed_templates(self, built_in: dict[str, dict[str, Any]], default: str) -> None:
        """First start only: the built-in templates and the default. Later starts give built-ins saved
        before hooks were part of templates their own hook look."""
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM templates").fetchone()[0]:
                for tid, data in built_in.items():
                    row = self._db.execute("SELECT data FROM templates WHERE id=?", (tid,)).fetchone()
                    saved = json.loads(row["data"]) if row else None
                    if saved is not None and "hook" not in saved and "hook" in data:
                        self._db.execute("UPDATE templates SET data=? WHERE id=?",
                                         (json.dumps({**saved, "hook": data["hook"]}), tid))
                return
            now = time.time()
            for tid, data in built_in.items():
                self._db.execute("INSERT INTO templates VALUES (?,?,?,?)", (tid, json.dumps(data), now, now))
            self._db.execute("INSERT OR REPLACE INTO app_settings VALUES ('default_template', ?)", (default,))

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM templates ORDER BY created, rowid").fetchall()
        return [{"id": r["id"], "data": json.loads(r["data"]), "updated": r["updated"]} for r in rows]

    def get_template(self, template_id: Optional[str]) -> Optional[dict[str, Any]]:
        if not template_id:
            return None
        with self._lock:
            row = self._db.execute("SELECT data FROM templates WHERE id=?", (template_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def save_template(self, template_id: str, data: dict[str, Any]) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO templates VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, "
                "updated=excluded.updated", (template_id, json.dumps(data), now, now))

    def delete_template(self, template_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM templates WHERE id=?", (template_id,))
        return cur.rowcount == 1

    def get_setting(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO app_settings VALUES (?, ?)", (key, value))

    # ---- publishing (Postiz) -----------------------------------------------
    def queue_publish(self, clip_id: str, chat_id: int, publish_at: Optional[float] = None,
                      now: bool = False) -> PublishRequest:
        req = PublishRequest(clip_id, chat_id, "waiting", publish_at, int(now), None, 0, time.time(), time.time())
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO publish_queue VALUES (?,?,?,?,?,?,?,?,?)",
                             (clip_id, chat_id, "waiting", publish_at, int(now), None, 0, req.next_try, req.created))
        return req

    def get_publish(self, clip_id: str) -> Optional[PublishRequest]:
        with self._lock:
            row = self._db.execute("SELECT * FROM publish_queue WHERE clip_id=?", (clip_id,)).fetchone()
        return PublishRequest(**dict(row)) if row else None

    def publish_requests(self, *statuses: str) -> list[PublishRequest]:
        sql, args = "SELECT * FROM publish_queue", ()
        if statuses:
            sql, args = sql + " WHERE status IN (%s)" % ",".join("?" for _ in statuses), statuses
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created", args).fetchall()
        return [PublishRequest(**dict(r)) for r in rows]

    def due_publish_requests(self) -> list[PublishRequest]:
        return [r for r in self.publish_requests("waiting") if r.next_try <= time.time()]

    def update_publish(self, clip_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE publish_queue SET {cols} WHERE clip_id=?", (*fields.values(), clip_id))

    def add_post(self, post_id: str, clip_id: str, channel_id: str, platform: str,
                 channel_name: Optional[str], publish_at: float, media_id: Optional[str],
                 media_path: Optional[str]) -> Post:
        now = time.time()
        post = Post(post_id, clip_id, channel_id, platform, channel_name, "scheduled", publish_at,
                    media_id, media_path, None, None, now, now)
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO posts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (post_id, clip_id, channel_id, platform, channel_name, "scheduled", publish_at,
                              media_id, media_path, None, None, now, now))
        return post

    def update_post(self, post_id: str, **fields: Any) -> None:
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE posts SET {cols} WHERE id=?", (*fields.values(), post_id))

    def posts_for_clip(self, clip_id: str) -> list[Post]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM posts WHERE clip_id=? ORDER BY platform", (clip_id,)).fetchall()
        return [Post(**dict(r)) for r in rows]

    def list_posts(self, *statuses: str, since: float = 0, limit: int = 1000) -> list[Post]:
        where, args = ["publish_at >= ?"], [since]
        if statuses:
            where.append("status IN (%s)" % ",".join("?" for _ in statuses))
            args += statuses
        with self._lock:
            rows = self._db.execute(f"SELECT * FROM posts WHERE {' AND '.join(where)} "
                                    "ORDER BY publish_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [Post(**dict(r)) for r in rows]

    # ---- log (the dashboard's Logs page) ----------------------------------
    def add_log(self, ts: float, level: str, source: str, message: str, job_id: Optional[str] = None,
                scan_id: Optional[str] = None, detail: Optional[str] = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO logs (ts, level, source, message, job_id, scan_id, detail) VALUES (?,?,?,?,?,?,?)",
                (ts, level, source, message[:4000], job_id, scan_id, detail[:20000] if detail else None))
            self._log_inserts += 1
            if self._log_inserts % 500 == 0:
                self._db.execute("DELETE FROM logs WHERE ts < ?", (time.time() - LOG_KEEP_DAYS * 86400,))
                self._db.execute("DELETE FROM logs WHERE id <= (SELECT MAX(id) FROM logs) - ?",
                                 (LOG_KEEP_ROWS,))

    def query_logs(self, levels: Optional[list[str]] = None, source: Optional[str] = None,
                   text: Optional[str] = None, job_id: Optional[str] = None,
                   scan_id: Optional[str] = None, before_id: Optional[int] = None,
                   since: float = 0, limit: int = 100) -> list[dict[str, Any]]:
        where, args = ["ts >= ?"], [since]
        if levels:
            where.append("level IN (%s)" % ",".join("?" for _ in levels))
            args += levels
        for column, value in (("source", source), ("job_id", job_id), ("scan_id", scan_id)):
            if value:
                where.append(f"{column}=?")
                args.append(value)
        if text:
            where.append("(message LIKE ? OR detail LIKE ?)")
            args += [f"%{text}%", f"%{text}%"]
        if before_id:
            where.append("id < ?")
            args.append(before_id)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM logs WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    def log_sources(self) -> list[str]:
        with self._lock:
            return [r["source"] for r in self._db.execute("SELECT DISTINCT source FROM logs ORDER BY source")]

    # ---- outbox ----------------------------------------------------------
    def enqueue_event(self, payload: dict[str, Any]) -> None:
        if not payload.get("chat_id"):
            return  # jobs started from the dashboard when no Telegram chat is configured
        with self._lock:
            self._db.execute("INSERT INTO outbox (payload, next_try) VALUES (?, ?)",
                             (json.dumps(payload), time.time()))

    def due_events(self, limit: int = 20) -> list[tuple[int, dict[str, Any], int]]:
        """Oldest undelivered events, in order, up to the first one still waiting for a retry."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, payload, attempts, next_try FROM outbox WHERE dead=0 "
                "ORDER BY id LIMIT ?", (limit,)).fetchall()
        now, due = time.time(), []
        for r in rows:
            if r["next_try"] > now:
                break
            due.append((r["id"], json.loads(r["payload"]), r["attempts"]))
        return due

    def event_delivered(self, event_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE id=?", (event_id,))

    def outbox(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM outbox ORDER BY id").fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def retry_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE outbox SET dead=0, attempts=0, next_try=? WHERE id=?",
                                   (time.time(), event_id))
        return cur.rowcount == 1

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM outbox WHERE id=?", (event_id,))
        return cur.rowcount == 1

    def event_failed(self, event_id: int, attempts: int, retry_in: float, dead: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET attempts=?, next_try=?, dead=? WHERE id=?",
                (attempts, time.time() + retry_in, int(dead), event_id))
