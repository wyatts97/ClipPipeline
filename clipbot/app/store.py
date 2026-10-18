"""SQLite state: jobs, clips and an outbox of events for n8n.

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
    updated REAL NOT NULL
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
    job_id TEXT
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
"""

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

    def close(self) -> None:
        self._db.close()

    # ---- jobs ------------------------------------------------------------
    def create_job(self, chat_id: int, source_url: str, range_start: Optional[float],
                   range_end: Optional[float], target_clips: int) -> Job:
        now = time.time()
        job = Job(new_id(), chat_id, source_url, range_start, range_end, target_clips,
                  "queued", None, None, now, now)
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, job.chat_id, job.source_url, job.range_start, job.range_end,
                 job.target_clips, job.status, None, None, now, now))
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
                "INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (clip.id, job_id, idx, title, hook, json.dumps(meta), duration, "pending",
                 path, preview_path, None))
        return clip

    def get_clip(self, clip_id: str) -> Optional[Clip]:
        with self._lock:
            row = self._db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["meta"] = json.loads(data["meta"] or "{}")
        return Clip(**data)

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
    def create_scan(self, chat_id: int, source_url: str) -> Scan:
        now = time.time()
        scan = Scan(new_id(), chat_id, source_url, "queued", None, None, None, None, now, now)
        with self._lock:
            self._db.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (scan.id, chat_id, source_url, "queued", None, None, None, None, now, now))
        return scan

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

    def add_moment(self, scan_id: str, rank: int, peak: float, start: float, end: float,
                   score: float) -> MomentRow:
        row = MomentRow(new_id(), scan_id, rank, peak, start, end, score, None)
        with self._lock:
            self._db.execute("INSERT INTO moments VALUES (?,?,?,?,?,?,?,?)",
                             (row.id, scan_id, rank, peak, start, end, score, None))
        return row

    def get_moment(self, moment_id: str) -> Optional[MomentRow]:
        with self._lock:
            row = self._db.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        return MomentRow(**dict(row)) if row else None

    def moments_for_scan(self, scan_id: str) -> list[MomentRow]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM moments WHERE scan_id=? ORDER BY rank",
                                    (scan_id,)).fetchall()
        return [MomentRow(**dict(r)) for r in rows]

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
                self._db.execute("INSERT INTO watches VALUES (?,?,?,?,?,?,?,?)",
                                 (watch.id, chat_id, platform, channel, last_vod_id, None, None,
                                  watch.created))
            except sqlite3.IntegrityError:
                return None
        return watch

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

    # ---- outbox ----------------------------------------------------------
    def enqueue_event(self, payload: dict[str, Any]) -> None:
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

    def event_failed(self, event_id: int, attempts: int, retry_in: float, dead: bool) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET attempts=?, next_try=?, dead=? WHERE id=?",
                (attempts, time.time() + retry_in, int(dead), event_id))
