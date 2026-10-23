"""JSON API for the web dashboard, behind a single-password login (DASHBOARD_PASSWORD).

The dashboard is served by clipbot on the LAN; nothing here is routed through the tunnel.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import time
import zipfile
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import captions, director, editing, fonts, images, media, render, sounds, storage, templates
from .config import Settings
from .parsing import format_timestamp, parse_timestamp
from .platforms import Channel, channel_url, detect_channel, detect_vod, timestamp_link
from .openshorts import OpenShortsError
from .postiz import PostizError
from .publisher import caption_for, next_free_slot, youtube_title
from .store import Clip, Job, MomentRow, Post, Scan, Store, Watch

COOKIE = "cp_session"
SESSION_DAYS = 30
MIN_RANGE_SECONDS = 20           # the shortest range to clip (OpenShorts takes 15 s: docker-compose)
LEVELS = {"error": ["ERROR", "CRITICAL"], "warning": ["WARNING", "ERROR", "CRITICAL"],
          "info": ["INFO", "WARNING", "ERROR", "CRITICAL"]}
ACTIVE = ("queued", "downloading", "submitting", "processing")

router = APIRouter(prefix="/api")
log = logging.getLogger("clipbot.dashboard")
_login_failures: dict[str, list[float]] = {}


# ---- session ---------------------------------------------------------------------
def _secret(settings: Settings) -> bytes:
    # Changing DASHBOARD_PASSWORD signs everyone out.
    return hashlib.sha256(f"{settings.dashboard_password}|{settings.notify_token}|clippipeline".encode()).digest()


def make_session(settings: Settings, now: Optional[float] = None) -> str:
    exp = int((time.time() if now is None else now) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(settings), str(exp).encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def session_valid(settings: Settings, value: Optional[str]) -> bool:
    if not value or "." not in value:
        return False
    exp, _, sig = value.partition(".")
    if not exp.isdigit() or int(exp) < time.time():
        return False
    expected = hmac.new(_secret(settings), exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def store_of(request: Request) -> Store:
    return request.app.state.store


def require_session(request: Request) -> None:
    settings = settings_of(request)
    if not settings.dashboard_password:
        raise HTTPException(503, "Set DASHBOARD_PASSWORD in .env to use the dashboard.")
    if not session_valid(settings, request.cookies.get(COOKIE)):
        raise HTTPException(401, "Sign in first.")


authed = [Depends(require_session)]


class LoginIn(BaseModel):
    password: str


@router.get("/session")
async def session(request: Request):
    settings = settings_of(request)
    return {"configured": bool(settings.dashboard_password),
            "authenticated": session_valid(settings, request.cookies.get(COOKIE))}


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    settings = settings_of(request)
    if not settings.dashboard_password:
        raise HTTPException(503, "Set DASHBOARD_PASSWORD in .env to use the dashboard.")
    client = request.client.host if request.client else "?"
    recent = [t for t in _login_failures.get(client, []) if t > time.time() - 600]
    if len(recent) >= 5:
        raise HTTPException(429, "Too many wrong passwords. Try again in 10 minutes.")
    if not hmac.compare_digest(body.password.encode(), settings.dashboard_password.encode()):
        _login_failures[client] = recent + [time.time()]
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Wrong password.")
    _login_failures.pop(client, None)
    response.set_cookie(COOKIE, make_session(settings), max_age=SESSION_DAYS * 86400,
                        httponly=True, samesite="strict", path="/")
    return {"authenticated": True}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"authenticated": False}


# ---- shapes ---------------------------------------------------------------------
def _range(job: Job) -> Optional[dict[str, float]]:
    if job.range_start is None or job.range_end is None:
        return None
    return {"start": job.range_start, "end": job.range_end}


def job_out(store: Store, job: Job) -> dict[str, Any]:
    vod = detect_vod(job.source_url)
    retry = store.retried(job.id) if job.status in ("failed", "cancelled") else None
    return {
        "id": job.id, "status": job.status, "source_url": job.source_url, "platform": vod.platform,
        "range": _range(job), "target_clips": job.target_clips, "error": job.error,
        "created": job.created, "updated": job.updated, "retry_of": job.retry_of,
        "retried_by": retry.id if retry else None, "openshorts_job": job.openshorts_job,
        "clip_count": len(store.clips_for_job(job.id)) if job.status == "done" else 0,
        "template": job.template,
        "note": job.note,
        # The Director's clips: what it saw in the range (VOD seconds), or the scan's hints until then.
        "picks": [{k: p.get(k) for k in ("start", "end", "score", "category", "what_happened")}
                  for p in job.preset_picks()],
    }


def thumb_path(settings: Settings, clip: Clip) -> Path:
    return settings.data_dir / "thumbs" / f"{clip.job_id}_{clip.idx}.jpg"


def edited(clip: Clip) -> bool:
    """Cut or zoomed in the editor."""
    edit = json.loads(clip.edit) if clip.edit else {}
    segments = edit.get("segments") or []
    return bool(edit.get("zooms")) or len(segments) > 1 or bool(segments and segments[0][0] > 0.05)


def clip_out(settings: Settings, clip: Clip, job: Optional[Job], store: Optional[Store] = None) -> dict[str, Any]:
    meta = clip.meta or {}
    video = Path(clip.path)
    version = int(video.stat().st_mtime) if video.is_file() else 0
    clean = settings.data_dir / "clean" / f"{clip.job_id}_{clip.idx}.mp4"
    template = store.get_template(clip.template) if store and clip.template else None
    own_hook = (json.loads(clip.edit) if clip.edit else {}).get("hook") or {}
    hook_look = captions.normalize_hook((template or {}).get("hook"))
    return {
        "template": clip.template, "template_name": (template or {}).get("name"),
        "sfx": clip.manual_sfx(), "render_state": clip.render_state, "render_error": clip.render_error,
        "can_render": clip.status == "pending" and clean.is_file(), "credit": meta.get("credit"),
        "edited": edited(clip),
        "hook_rendered": meta.get("hook_rendered"),
        "what_happened": meta.get("what_happened"),    # the Director's words for the clip
        # Whether a render draws the hook: the clip's own switch, else its template's and AUTO_HOOK.
        "hook_on": bool(own_hook.get("enabled", hook_look["enabled"] and settings.auto_hook)),
        "word_count": len(clip.word_list()),
        "download_url": f"/api/clips/{clip.id}/download",
        "post_text": {"youtube_title": youtube_title(clip), "youtube": caption_for("youtube", clip),
                      "tiktok": caption_for("tiktok", clip), "instagram": caption_for("instagram", clip)},
        "id": clip.id, "job_id": clip.job_id, "index": clip.idx, "status": clip.status,
        "title": clip.title, "hook": clip.hook, "duration": clip.duration, "decided": clip.decided,
        "description_tiktok": meta.get("video_description_for_tiktok"),
        "description_instagram": meta.get("video_description_for_instagram"),
        "source_url": job.source_url if job else None, "range": _range(job) if job else None,
        "created": job.updated if job else None, "file": Path(clip.path).name,
        "video_url": f"/api/clips/{clip.id}/video?v={version}",
        "thumb_url": f"/api/clips/{clip.id}/thumb" if thumb_path(settings, clip).is_file() else None,
    }


def scan_out(store: Store, scan: Scan) -> dict[str, Any]:
    vod = detect_vod(scan.source_url)
    return {
        "id": scan.id, "status": scan.status, "source_url": scan.source_url, "title": scan.title,
        "duration": scan.duration, "error": scan.error, "created": scan.created, "updated": scan.updated,
        "signals": json.loads(scan.signals) if scan.signals else [], "platform": vod.platform,
        "vod_id": vod.vod_id, "moment_count": len(store.moments_for_scan(scan.id)), "mode": scan.mode or "signals",
    }


def moment_out(store: Store, scan: Scan, m: MomentRow) -> dict[str, Any]:
    job = store.get_job(m.job_id) if m.job_id else None
    return {"id": m.id, "rank": m.rank, "peak": m.peak, "start": m.start, "end": m.end,
            "score": round(m.score, 2), "details": m.details,
            "link": timestamp_link(detect_vod(scan.source_url), m.start),
            "job": {"id": job.id, "status": job.status} if job else None}


def default_chat(settings: Settings) -> int:
    return min(settings.allowed_chat_ids) if settings.allowed_chat_ids else 0


def _seconds(value: Union[str, float, int, None], name: str) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return parse_timestamp(str(value))
    except ValueError:
        raise HTTPException(422, f"{name} isn't a time. Use h:mm:ss, mm:ss or seconds.")


def check_range(settings: Settings, start: Optional[float], end: Optional[float],
                duration: Optional[float] = None) -> None:
    if (start is None) != (end is None):
        raise HTTPException(422, "Give both a start and an end, or neither.")
    if start is None:
        return
    if start < 0 or end <= start:
        raise HTTPException(422, "The end has to come after the start.")
    if end - start < MIN_RANGE_SECONDS:
        raise HTTPException(422, f"Make the range at least {MIN_RANGE_SECONDS} seconds so there's something to clip.")
    if end - start > settings.max_range_minutes * 60:
        raise HTTPException(422, f"Ranges are limited to {settings.max_range_minutes} minutes (MAX_RANGE_MINUTES).")
    if duration and end > duration + 1:
        raise HTTPException(422, f"The video is only {format_timestamp(duration)} long.")


# ---- overview -------------------------------------------------------------------
async def _n8n_ok(settings: Settings) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            return (await client.get(f"{settings.n8n_url}/healthz")).status_code < 400
    except httpx.HTTPError:
        return False


@router.get("/overview", dependencies=authed)
async def overview(request: Request):
    settings, store = settings_of(request), store_of(request)
    pipeline = request.app.state.pipeline
    now = time.time()
    publisher = request.app.state.publisher
    openshorts_ok, n8n_ok, postiz_ok = await asyncio.gather(
        pipeline.openshorts.healthy(), _n8n_ok(settings),
        publisher.postiz.healthy() if publisher.enabled else asyncio.sleep(0, None))

    working = []
    for job in store.jobs_with_status("downloading", "submitting", "processing"):
        item = job_out(store, job)
        if job.status == "processing" and job.openshorts_job:
            try:
                doc = await asyncio.wait_for(pipeline.openshorts.status(job.openshorts_job), 3)
                lines = [str(line) for line in ((doc or {}).get("logs") or [])]
                item["last_log"] = lines[-1][:300] if lines else None
            except Exception:
                item["last_log"] = None
        working.append(item)

    pending = store.list_clips("pending", limit=500)
    outbox = store.outbox()
    problems = []
    for job in store.list_jobs("failed", limit=20):
        if job.updated > now - 3 * 86400 and not store.retried(job.id):
            problems.append({"kind": "job", "id": job.id, "ts": job.updated, "title": f"Job {job.id} failed",
                             "detail": job.error, "source_url": job.source_url})
    for scan in store.scans_with_status("failed"):
        if scan.updated > now - 3 * 86400:
            problems.append({"kind": "scan", "id": scan.id, "ts": scan.updated, "title": f"Scan {scan.id} failed",
                             "detail": scan.error, "source_url": scan.source_url})
    for event in outbox:
        if event["dead"]:
            problems.append({"kind": "event", "id": event["id"], "ts": event["next_try"],
                             "title": "A Telegram message was never delivered",
                             "detail": (event["payload"].get("text") or event["payload"].get("caption") or "")[:300]})
    problems.sort(key=lambda p: p["ts"], reverse=True)

    activity = []
    for job in store.list_jobs(limit=25):
        activity.append({"ts": job.updated, "kind": "job", "id": job.id, "status": job.status,
                         "text": job.source_url, "range": _range(job)})
    for scan in store.list_scans(limit=15):
        activity.append({"ts": scan.updated, "kind": "scan", "id": scan.id, "status": scan.status,
                         "text": scan.title or scan.source_url})
    for clip in store.list_clips("approved", limit=15) + store.list_clips("rejected", limit=15):
        activity.append({"ts": clip.decided or 0, "kind": "clip", "id": clip.id, "status": clip.status,
                         "text": clip.title or clip.id})
    activity.sort(key=lambda a: a["ts"], reverse=True)

    disk = shutil.disk_usage(settings.data_dir)
    week = now - 7 * 86400
    decided = store.count_by_status("clips", since=week)
    return {
        "working": working,
        "queued_jobs": len(store.jobs_with_status("queued")),
        "scans": [scan_out(store, s) for s in store.scans_with_status("running", "queued")],
        "pending_clips": len(pending),
        "pending_preview": [clip_out(settings, c, store.get_job(c.job_id), store) for c in pending[:8]],
        "problems": problems[:20],
        "errors_24h": len(store.query_logs(levels=LEVELS["error"], since=now - 86400, limit=1000)),
        "activity": activity[:30],
        "week": {"approved": decided.get("approved", 0), "rejected": decided.get("rejected", 0),
                 "jobs": sum(store.count_by_status("jobs", since=week).values()),
                 "scans": sum(store.count_by_status("scans", since=week).values())},
        "scheduled_posts": len(store.list_posts("scheduled", since=now)),
        "health": {"openshorts": openshorts_ok, "n8n": n8n_ok, "postiz": postiz_ok,
                   "janitor_stuck": storage.janitor_stuck(settings),
                   "telegram_waiting": sum(1 for e in outbox if not e["dead"]),
                   "telegram_failed": sum(1 for e in outbox if e["dead"]),
                   "disk": {"total": disk.total, "free": disk.free}},
    }


# ---- clips ----------------------------------------------------------------------
class ClipEdit(BaseModel):
    title: Optional[str] = None
    hook: Optional[str] = None
    description_tiktok: Optional[str] = None
    description_instagram: Optional[str] = None


class DecisionIn(BaseModel):
    decision: str            # approve | reject


class BulkDecisionIn(DecisionIn):
    ids: list[str]


def _clip_or_404(store: Store, clip_id: str) -> Clip:
    clip = store.get_clip(clip_id)
    if not clip:
        raise HTTPException(404, "No such clip.")
    return clip


@router.get("/clips", dependencies=authed)
async def list_clips(request: Request, status: str = "pending", limit: int = 200):
    settings, store = settings_of(request), store_of(request)
    return {"items": [clip_out(settings, c, store.get_job(c.job_id), store)
                      for c in store.list_clips(status, limit=min(limit, 500))]}


@router.patch("/clips/{clip_id}", dependencies=authed)
async def edit_clip(clip_id: str, body: ClipEdit, request: Request):
    settings, store = settings_of(request), store_of(request)
    clip = _clip_or_404(store, clip_id)
    meta = dict(clip.meta or {})
    title = clip.title if body.title is None else body.title.strip()
    hook = clip.hook if body.hook is None else body.hook.strip()
    if body.description_tiktok is not None:
        meta["video_description_for_tiktok"] = body.description_tiktok
    if body.description_instagram is not None:
        meta["video_description_for_instagram"] = body.description_instagram
    meta["viral_hook_text"] = hook
    store.update_clip_text(clip.id, title, hook, meta)
    sidecar = Path(clip.path).with_suffix(".json")
    if clip.status == "approved" and sidecar.is_file():  # keep the publishing metadata in step
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data.update({"title": title, "hook": hook, **{k: v for k, v in meta.items() if k.startswith("video_")}})
        sidecar.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    clip = store.get_clip(clip.id)
    return clip_out(settings, clip, store.get_job(clip.job_id), store)


def _decide(request: Request, clip_id: str, decision: str) -> dict[str, Any]:
    if decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    settings, store = settings_of(request), store_of(request)
    clip = _clip_or_404(store, clip_id)
    job = store.get_job(clip.job_id)
    toast, reply = request.app.state.pipeline.decide(job.chat_id, f"{'ok' if decision == 'approve' else 'no'}:{clip.id}")
    clip = store.get_clip(clip.id)
    return {"result": toast, "message": reply, "clip": clip_out(settings, clip, job, store)}


@router.post("/clips/{clip_id}/decision", dependencies=authed)
async def decide_clip(clip_id: str, body: DecisionIn, request: Request):
    return _decide(request, clip_id, body.decision)


@router.post("/clips/decisions", dependencies=authed)
async def decide_clips(body: BulkDecisionIn, request: Request):
    return {"items": [_decide(request, clip_id, body.decision) for clip_id in body.ids]}


@router.get("/clips/{clip_id}/video", dependencies=authed)
async def clip_video(clip_id: str, request: Request):
    clip = _clip_or_404(store_of(request), clip_id)
    if not Path(clip.path).is_file():
        raise HTTPException(404, "The clip's file is gone.")
    return FileResponse(clip.path, media_type="video/mp4")


def download_name(clip: Clip) -> str:
    """Approved clips keep their approved/ name; others get title + id."""
    if clip.status == "approved":
        return Path(clip.path).name
    return f"{media.slugify(clip.title)}_{clip.id}.mp4"


@router.get("/clips/{clip_id}/download", dependencies=authed)
async def clip_download(clip_id: str, request: Request):
    clip = _clip_or_404(store_of(request), clip_id)
    if not Path(clip.path).is_file():
        raise HTTPException(404, "The clip's file is gone.")
    return FileResponse(clip.path, media_type="video/mp4", filename=download_name(clip))


class _ZipSink:
    """A write-only file for zipfile that hands back what was written, so the zip streams out
    without a temporary file (zipfile handles the missing seek/tell itself)."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def zip_stream(files: list[tuple[Path, str]]) -> Iterator[bytes]:
    """Stored (not compressed: video already is) zip of (path, name in the zip) pairs."""
    sink = _ZipSink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for path, name in files:
            info = zipfile.ZipInfo.from_file(path, name)
            with path.open("rb") as src, archive.open(info, "w", force_zip64=True) as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


@router.get("/clips/download.zip", dependencies=authed)
async def clips_zip(request: Request, status: str = "approved", ids: Optional[str] = None):
    """Every clip with this status (or just `ids`, comma separated) as one zip, approved ones with
    their .json (title, hook, captions)."""
    store = store_of(request)
    wanted = {i for i in (ids or "").split(",") if i}
    clips = [c for c in store.list_clips(status, limit=1000)
             if (not wanted or c.id in wanted) and Path(c.path).is_file()]
    if not clips:
        raise HTTPException(404, f"No {status} clips to download.")
    files: list[tuple[Path, str]] = []
    for clip in clips:
        name = download_name(clip)
        files.append((Path(clip.path), name))
        sidecar = Path(clip.path).with_suffix(".json")
        if sidecar.is_file():
            files.append((sidecar, Path(name).with_suffix(".json").name))
    filename = f"clippipeline-{status}-{date.today().isoformat()}.zip"
    return StreamingResponse(zip_stream(files), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/clips/{clip_id}/thumb", dependencies=authed)
async def clip_thumb(clip_id: str, request: Request):
    clip = _clip_or_404(store_of(request), clip_id)
    path = thumb_path(settings_of(request), clip)
    if not path.is_file():
        raise HTTPException(404, "No thumbnail for this clip.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


# ---- jobs -----------------------------------------------------------------------
class JobIn(BaseModel):
    url: str
    start: Union[str, float, None] = None
    end: Union[str, float, None] = None
    clips: Optional[int] = None
    template: Optional[str] = None
    note: Optional[str] = None        # what to look for ("friend smashes the monitor")


def _job_or_404(store: Store, job_id: str) -> Job:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job


@router.get("/jobs", dependencies=authed)
async def list_jobs(request: Request, status: Optional[str] = None, limit: int = 100):
    store = store_of(request)
    return {"items": [job_out(store, j) for j in store.list_jobs(status, limit=min(limit, 500))]}


@router.get("/jobs/{job_id}", dependencies=authed)
async def get_job(job_id: str, request: Request):
    settings, store = settings_of(request), store_of(request)
    job = _job_or_404(store, job_id)
    openshorts = None
    if job.openshorts_job and job.status in ("processing", "failed"):  # OpenShorts keeps logs ~24 h
        try:
            doc = await asyncio.wait_for(request.app.state.pipeline.openshorts.status(job.openshorts_job), 4)
            openshorts = {"status": (doc or {}).get("status"),
                          "logs": [str(line)[:500] for line in ((doc or {}).get("logs") or [])][-80:]}
        except Exception as e:
            openshorts = {"status": "unreachable", "logs": [str(e) or type(e).__name__]}
    usage = storage.job_usage(settings, store, job)
    return {**job_out(store, job),
            "clips": [clip_out(settings, c, job, store) for c in store.clips_for_job(job.id)],
            "logs": store.query_logs(job_id=job.id, limit=200), "openshorts": openshorts,
            "files": {**usage, "freeable": storage.freeable(usage),
                      "deleting": storage.purge_requested(settings, job.openshorts_job)}}


@router.post("/jobs", dependencies=authed, status_code=201)
async def create_job(body: JobIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "Paste a full video link starting with https://")
    start, end = _seconds(body.start, "Start"), _seconds(body.end, "End")
    check_range(settings, start, end)
    clips = body.clips or settings.target_clips
    if not 1 <= clips <= 15:
        raise HTTPException(422, "Clips must be between 1 and 15.")
    if body.template and store.get_template(body.template) is None:
        raise HTTPException(422, "No such template.")
    job = store.create_job(default_chat(settings), url, start, end, clips, template=body.template or None,
                           note=(body.note or "").strip()[:300] or None)
    request.app.state.pipeline.wake()
    return job_out(store, job)


@router.post("/jobs/{job_id}/retry", dependencies=authed, status_code=201)
async def retry_job(job_id: str, request: Request):
    store = store_of(request)
    job = _job_or_404(store, job_id)
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
    new = store.create_job(job.chat_id, job.source_url, job.range_start, job.range_end, job.target_clips,
                           retry_of=job.id, template=job.template, note=job.note,
                           preset=job.preset_picks() or None)
    request.app.state.pipeline.wake()
    return job_out(store, new)


@router.post("/jobs/{job_id}/cancel", dependencies=authed)
async def cancel_job(job_id: str, request: Request):
    store = store_of(request)
    _job_or_404(store, job_id)
    if not request.app.state.pipeline.cancel(job_id):
        raise HTTPException(409, "This job has already finished.")
    return job_out(store, store.get_job(job_id))


@router.post("/jobs/{job_id}/delete-files", dependencies=authed)
async def delete_job_files(job_id: str, request: Request):
    settings, store = settings_of(request), store_of(request)
    job = _job_or_404(store, job_id)
    try:
        freed = storage.delete_job_files(settings, store, job)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"freed": freed}


@router.delete("/jobs/{job_id}", dependencies=authed)
async def delete_job(job_id: str, request: Request):
    """A failed or cancelled job, gone: its files and its record."""
    settings, store = settings_of(request), store_of(request)
    job = _job_or_404(store, job_id)
    try:
        freed = storage.delete_job(settings, store, job)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"freed": freed}


@router.post("/jobs/clear-failed", dependencies=authed)
async def clear_failed_jobs(request: Request):
    """Every failed and cancelled job, except ones holding approved clips."""
    settings, store = settings_of(request), store_of(request)
    deleted = freed = 0
    for job in store.jobs_with_status("failed", "cancelled"):
        try:
            freed += storage.delete_job(settings, store, job)
            deleted += 1
        except ValueError:
            continue
    return {"deleted": deleted, "freed": freed}


# ---- storage --------------------------------------------------------------------
@router.get("/storage", dependencies=authed)
async def storage_report(request: Request):
    settings, store = settings_of(request), store_of(request)
    report = await asyncio.to_thread(storage.usage_report, settings, store)
    jobs = [{**job_out(store, item["job"]),
             "files": {**item["usage"], "freeable": storage.freeable(item["usage"]), "deleting": item["deleting"]}}
            for item in report["jobs"]]
    jobs.sort(key=lambda j: j["files"]["freeable"], reverse=True)
    return {**report, "jobs": jobs}


@router.post("/storage/cleanup", dependencies=authed)
async def storage_cleanup(request: Request):
    settings, store = settings_of(request), store_of(request)
    candidates = storage.cleanup_candidates(settings, store)
    freed = sum(storage.delete_job_files(settings, store, job) for job, _ in candidates)
    return {"jobs": len(candidates), "freed": freed}


# ---- publishing -----------------------------------------------------------------
class PublishIn(BaseModel):
    at: Optional[float] = None     # unix time; empty = the next free slot
    now: bool = False


def post_out(post: Post) -> dict[str, Any]:
    return {"id": post.id, "platform": post.platform, "channel": post.channel_name, "status": post.status,
            "publish_at": post.publish_at, "url": post.url, "error": post.error}


def _group_status(posts: list[Post]) -> str:
    states = {p.status for p in posts}
    for status in ("failed", "scheduled", "published"):
        if status in states:
            return status
    return "cancelled"


@router.get("/publishing", dependencies=authed)
async def publishing(request: Request):
    settings, store = settings_of(request), store_of(request)
    publisher = request.app.state.publisher
    base = {"configured": publisher.enabled, "postiz_url": settings.postiz_public_url or None,
            "slots": publisher.slot_label(), "timezone": settings.timezone,
            "tiktok_mode": settings.tiktok_post_mode}
    if not publisher.enabled:
        return base
    channels, error = [], None
    try:
        channels = await asyncio.wait_for(publisher.postiz.channels(), 6)
    except (PostizError, asyncio.TimeoutError) as e:
        error = str(e) or "Postiz didn't answer in time."
    now = time.time()
    by_clip: dict[str, list[Post]] = {}
    for post in store.list_posts(since=now - 30 * 86400, limit=1000):
        by_clip.setdefault(post.clip_id, []).append(post)
    groups = []
    for clip_id, posts in by_clip.items():
        clip = store.get_clip(clip_id)
        if not clip:
            continue
        live = [p for p in posts if p.status != "cancelled"] or posts
        groups.append({"clip": clip_out(settings, clip, store.get_job(clip.job_id), store),
                       "status": _group_status(live), "publish_at": min(p.publish_at for p in live),
                       "posts": [post_out(p) for p in sorted(live, key=lambda p: p.platform)]})
    upcoming = sorted((g for g in groups if g["status"] == "scheduled"), key=lambda g: g["publish_at"])
    recent = sorted((g for g in groups if g["status"] != "scheduled"), key=lambda g: g["publish_at"], reverse=True)
    queue = []
    for req in store.publish_requests("waiting", "failed"):
        clip = store.get_clip(req.clip_id)
        if clip:
            queue.append({"clip": clip_out(settings, clip, store.get_job(clip.job_id), store), "status": req.status,
                          "error": req.error, "attempts": req.attempts, "next_try": req.next_try})
    busy = {q["clip"]["id"] for q in queue} | {
        clip_id for clip_id, posts in by_clip.items() if any(p.status in ("scheduled", "published") for p in posts)}
    unscheduled = [clip_out(settings, c, store.get_job(c.job_id), store)
                   for c in store.list_clips("approved", limit=100) if c.id not in busy]
    taken = {int(p.publish_at // 60) for p in store.list_posts("scheduled", since=now)}
    return {**base, "channels": channels, "error": error,
            "next_slot": next_free_slot(publisher.slots, publisher.tz, now, taken),
            "upcoming": upcoming, "recent": recent[:50], "queue": queue, "unscheduled": unscheduled[:50]}


def _publisher(request: Request):
    publisher = request.app.state.publisher
    if not publisher.enabled:
        raise HTTPException(503, "Publishing is off. Set POSTIZ_API_KEY in .env (see the README's Publishing section).")
    return publisher


@router.post("/clips/{clip_id}/publish", dependencies=authed)
async def publish_clip(clip_id: str, body: PublishIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    publisher = _publisher(request)
    clip = _clip_or_404(store, clip_id)
    job = store.get_job(clip.job_id)
    chat_id = job.chat_id if job else default_chat(settings)
    try:
        if any(p.status == "scheduled" for p in store.posts_for_clip(clip.id)):
            await publisher.reschedule(clip.id, chat_id, body.at, body.now)
        else:
            publisher.queue(clip, chat_id, body.at, body.now)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except PostizError as e:
        raise HTTPException(502, str(e))
    return {"queued": True}


@router.post("/clips/{clip_id}/unschedule", dependencies=authed)
async def unschedule_clip(clip_id: str, request: Request):
    publisher = _publisher(request)
    _clip_or_404(store_of(request), clip_id)
    try:
        return {"cancelled": await publisher.unschedule(clip_id)}
    except PostizError as e:
        raise HTTPException(502, str(e))


# ---- templates, sounds and fonts --------------------------------------------------------
MAX_SOUND_BYTES = 30 * 1024 * 1024
MAX_FONT_BYTES = 10 * 1024 * 1024
AUDIO_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
               ".aac": "audio/aac", ".flac": "audio/flac", ".opus": "audio/ogg"}


class TemplateIn(BaseModel):
    name: str
    captions: dict[str, Any] = {}
    hook: dict[str, Any] = {}
    music: dict[str, Any] = {}
    voice_volume: float = 1.0
    sfx: list[dict[str, Any]] = []
    credit: str = ""


class RenderIn(BaseModel):
    template: Optional[str] = None
    sfx: Optional[list[dict[str, Any]]] = None    # effects placed by hand: [{sound, t, volume}]


def template_out(settings: Settings, store: Store, item: dict[str, Any]) -> dict[str, Any]:
    t = templates.normalize(item["data"])
    return {"id": item["id"], **t, "default": item["id"] == store.get_setting("default_template"),
            "missing_sounds": templates.missing_sounds(settings, t)}


def _template_or_404(store: Store, template_id: str) -> dict[str, Any]:
    data = store.get_template(template_id)
    if data is None:
        raise HTTPException(404, "No such template.")
    return {"id": template_id, "data": data}


async def _sound_items(settings: Settings) -> list[dict[str, Any]]:
    folder = settings.data_dir / "sounds"
    paths = sorted(p for p in folder.iterdir() if p.suffix.lower() in templates.SOUND_EXTS) if folder.is_dir() else []
    meta = sounds.load_meta(settings)
    return list(await asyncio.gather(*(_sound_item(settings, p, meta) for p in paths)))


async def _sound_item(settings: Settings, path: Path, meta: Optional[dict] = None) -> dict[str, Any]:
    try:
        duration = await media.probe_duration(path)
    except media.MediaError:
        duration = None
    return {"name": path.stem, "file": path.name, "size": path.stat().st_size, "duration": duration,
            **sounds.describe(settings, path.stem, meta)}


@router.get("/templates", dependencies=authed)
async def list_templates(request: Request):
    settings, store = settings_of(request), store_of(request)
    return {"items": [template_out(settings, store, item) for item in store.list_templates()],
            "default": store.get_setting("default_template"),
            "presets": captions.PRESETS, "effects": list(captions.EFFECTS), "positions": list(captions.POSITIONS),
            "triggers": list(templates.TRIGGERS), "fonts": fonts.list_fonts(settings),
            "sounds": [s["name"] for s in await _sound_items(settings)],
            "kinds": templates.MOMENT_KINDS, "gemini": bool(settings.gemini_api_key),
            "hook_styles": list(captions.HOOK_STYLES), "hook_default": captions.DEFAULT_HOOK,
            "auto_hook": settings.auto_hook}


@router.post("/templates", dependencies=authed, status_code=201)
async def create_template(body: TemplateIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    data = templates.normalize(body.model_dump())
    base = templates.slug(data["name"])
    tid, n = base, 2
    while store.get_template(tid) is not None:
        tid, n = f"{base}-{n}", n + 1
    store.save_template(tid, data)
    return template_out(settings, store, {"id": tid, "data": data})


@router.put("/templates/{template_id}", dependencies=authed)
async def update_template(template_id: str, body: TemplateIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    _template_or_404(store, template_id)
    data = templates.normalize(body.model_dump())
    store.save_template(template_id, data)
    return template_out(settings, store, {"id": template_id, "data": data})


@router.delete("/templates/{template_id}", dependencies=authed)
async def delete_template(template_id: str, request: Request):
    store = store_of(request)
    _template_or_404(store, template_id)
    if store.get_setting("default_template") == template_id:
        raise HTTPException(409, "This is the default template. Make another one the default first.")
    store.delete_template(template_id)
    return {"ok": True}


@router.post("/templates/{template_id}/default", dependencies=authed)
async def make_default_template(template_id: str, request: Request):
    store = store_of(request)
    _template_or_404(store, template_id)
    store.set_setting("default_template", template_id)
    return {"default": template_id}


@router.get("/sounds", dependencies=authed)
async def list_sounds(request: Request):
    settings = settings_of(request)
    return {"items": await _sound_items(settings), "categories": list(sounds.CATEGORIES),
            "freesound": bool(settings.freesound_api_key), "pack": _pack_state(request)}


def _pack_state(request: Request) -> dict[str, Any]:
    state = getattr(request.app.state, "sound_pack", None) or {}
    return {"running": bool(state.get("running")), "result": state.get("result"),
            "size": len(sounds.MEME_PACK) + len(sounds.PACK_MUSIC)}


@router.post("/sounds/packs/meme", dependencies=authed, status_code=202)
async def install_meme_pack(request: Request):
    """Download the meme pack onto this server, in the background (GET /sounds shows progress)."""
    settings = settings_of(request)
    state = getattr(request.app.state, "sound_pack", None)
    if not (state and state.get("running")):
        state = request.app.state.sound_pack = {"running": True, "result": None}

        async def run() -> None:
            try:
                state["result"] = await sounds.install_pack(settings)
            except Exception as e:      # noqa: BLE001 - reported on the page
                log.exception("meme pack failed")
                state["result"] = {"added": [], "kept": [], "failed": {"pack": str(e)}}
            finally:
                state["running"] = False

        _background(request, run())
    return _pack_state(request)


class SoundEdit(BaseModel):
    category: Optional[str] = None
    favorite: Optional[bool] = None


@router.patch("/sounds/{name}", dependencies=authed)
async def edit_sound(name: str, body: SoundEdit, request: Request):
    settings = settings_of(request)
    path = templates.find_sound(settings, templates.sound_name(name))
    if not path:
        raise HTTPException(404, "No such sound.")
    if body.category is not None and body.category not in sounds.CATEGORIES:
        raise HTTPException(422, "No such category.")
    sounds.set_meta(settings, path.stem, category=body.category or sounds.describe(settings, path.stem)["category"],
                    favorite=body.favorite)
    return await _sound_item(settings, path)


@router.get("/sounds/search", dependencies=authed)
async def search_sounds(request: Request, q: str = "", source: str = "myinstants"):
    if source not in ("myinstants", "freesound"):
        raise HTTPException(422, "source is myinstants or freesound")
    try:
        return {"items": await sounds.search(settings_of(request), q, source)}
    except sounds.SoundError as e:
        raise HTTPException(503 if "FREESOUND_API_KEY" in str(e) else 502, str(e))


class SoundImport(BaseModel):
    item: dict[str, Any]
    name: Optional[str] = None
    category: Optional[str] = None


@router.post("/sounds/import", dependencies=authed, status_code=201)
async def import_sound(body: SoundImport, request: Request):
    settings = settings_of(request)
    try:
        name = await sounds.import_sound(settings, body.item, body.name, body.category)
    except sounds.SoundError as e:
        raise HTTPException(422, str(e))
    return await _sound_item(settings, templates.find_sound(settings, name))


@router.put("/sounds/{filename}", dependencies=authed, status_code=201)
async def upload_sound(filename: str, request: Request):
    settings = settings_of(request)
    suffix = Path(filename).suffix.lower()
    name = templates.sound_name(filename)
    if suffix not in templates.SOUND_EXTS or not name:
        raise HTTPException(422, "Upload an audio file: " + ", ".join(templates.SOUND_EXTS))
    body = await request.body()
    if not body or len(body) > MAX_SOUND_BYTES:
        raise HTTPException(413, f"Sounds can be up to {MAX_SOUND_BYTES // 1024 // 1024} MB.")
    for old in (settings.data_dir / "sounds").glob("*"):
        if old.suffix.lower() in templates.SOUND_EXTS and old.stem.lower() == name.lower():
            old.unlink()                   # replacing a sound of the same name, maybe another format
    path = templates.sounds_dir(settings) / f"{name}{suffix}"
    path.write_bytes(body)
    try:
        duration = await media.probe_duration(path)
    except media.MediaError:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "That file isn't audio ffmpeg can read.")
    if name not in sounds.load_meta(settings):
        sounds.set_meta(settings, name, source="upload")
    return {"name": name, "file": path.name, "size": len(body), "duration": duration, **sounds.describe(settings, name)}


@router.delete("/sounds/{name}", dependencies=authed)
async def delete_sound(name: str, request: Request):
    path = templates.find_sound(settings_of(request), templates.sound_name(name))
    if not path:
        raise HTTPException(404, "No such sound.")
    path.unlink()
    sounds.drop_meta(settings_of(request), path.stem)
    return {"ok": True}


@router.get("/sounds/{name}/file", dependencies=authed)
async def sound_file(name: str, request: Request):
    path = templates.find_sound(settings_of(request), templates.sound_name(name))
    if not path:
        raise HTTPException(404, "No such sound.")
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


# ---- pictures (the editor's overlays) -------------------------------------------------------------
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}


@router.get("/images", dependencies=authed)
async def list_images(request: Request):
    return {"items": images.items(settings_of(request)),
            "stickers": [{"name": n, "ref": f"{images.STICKER}{n}", "url": f"/api/stickers/{n}.png"} for n in images.stickers()]}


@router.put("/images/{filename}", dependencies=authed, status_code=201)
async def upload_image(filename: str, request: Request):
    body = await request.body()
    if not body:
        raise HTTPException(422, "That file is empty.")
    settings = settings_of(request)
    try:
        name = await images.save_upload(settings, filename, body)
    except images.ImageError as e:
        raise HTTPException(422, str(e))
    return images.item(settings, images.find_image(settings, name))


class ImageImport(BaseModel):
    url: str
    name: Optional[str] = None


@router.post("/images/import", dependencies=authed, status_code=201)
async def import_image(body: ImageImport, request: Request):
    settings = settings_of(request)
    try:
        name = await images.import_url(settings, body.url.strip(), body.name)
    except images.ImageError as e:
        raise HTTPException(422, str(e))
    return images.item(settings, images.find_image(settings, name))


@router.delete("/images/{name}", dependencies=authed)
async def delete_image(name: str, request: Request):
    settings = settings_of(request)
    path = images.find_image(settings, images.image_name(name))
    if not path:
        raise HTTPException(404, "No such picture.")
    path.unlink()
    images.drop_meta(settings, path.stem)
    return {"ok": True}


@router.get("/images/{name}/file", dependencies=authed)
async def image_file(name: str, request: Request):
    path = images.find_image(settings_of(request), images.image_name(name))
    if not path:
        raise HTTPException(404, "No such picture.")
    return FileResponse(path, media_type=IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@router.get("/stickers/{filename}", dependencies=authed)
async def sticker_file(filename: str, request: Request):
    path = images.find_image(settings_of(request), images.STICKER + Path(filename).stem)
    if not path:
        raise HTTPException(404, "No such sticker.")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "max-age=604800"})


class FrameGrab(BaseModel):
    t: float
    crop: Optional[dict[str, float]] = None      # x, y, w, h: shares of the frame
    name: Optional[str] = None


@router.post("/clips/{clip_id}/frame", dependencies=authed, status_code=201)
async def grab_frame(clip_id: str, body: FrameGrab, request: Request):
    """A frame of the clip (or a box of it) into the picture library: a reaction face to bring back later."""
    settings = settings_of(request)
    clip = _clip_or_404(store_of(request), clip_id)
    source = request.app.state.pipeline.clean_source(clip)
    if not source.is_file():
        source = Path(clip.path)
    try:
        name = await images.grab_frame(settings, source, body.t, body.crop, body.name)
    except images.ImageError as e:
        raise HTTPException(422, str(e))
    return images.item(settings, images.find_image(settings, name))


@router.get("/fonts", dependencies=authed)
async def list_font_files(request: Request):
    return {"items": fonts.list_fonts(settings_of(request))}


@router.put("/fonts/{filename}", dependencies=authed, status_code=201)
async def upload_font(filename: str, request: Request):
    settings = settings_of(request)
    safe = Path(filename).name
    if Path(safe).suffix.lower() not in fonts.FONT_EXTS:
        raise HTTPException(422, "Upload a .ttf or .otf font.")
    body = await request.body()
    if not body or len(body) > MAX_FONT_BYTES:
        raise HTTPException(413, f"Fonts can be up to {MAX_FONT_BYTES // 1024 // 1024} MB.")
    path = settings.folder("fonts") / safe
    path.write_bytes(body)
    family = fonts.family_name(path)
    if not family:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "That file isn't a font clipbot can read.")
    return {"family": family, "file": safe, "bundled": False, **fonts.metrics(path)}


@router.delete("/fonts/{filename}", dependencies=authed)
async def delete_font(filename: str, request: Request):
    path = settings_of(request).data_dir / "fonts" / Path(filename).name
    if not path.is_file():
        raise HTTPException(404, "Only fonts you uploaded can be deleted.")
    path.unlink()
    return {"ok": True}


@router.get("/fonts/{filename}/file", dependencies=authed)
async def font_data(filename: str, request: Request):
    path = fonts.font_file(settings_of(request), filename)
    if not path:
        raise HTTPException(404, "No such font.")
    return FileResponse(path, media_type="font/otf" if path.suffix.lower() == ".otf" else "font/ttf",
                        headers={"Cache-Control": "max-age=86400"})


@router.post("/clips/{clip_id}/render", dependencies=authed, status_code=202)
async def render_clip(clip_id: str, body: RenderIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    clip = _clip_or_404(store, clip_id)
    pipeline = request.app.state.pipeline
    if clip.status != "pending":
        raise HTTPException(409, "Only clips waiting for review can be re-rendered.")
    if clip.render_state == "rendering":
        raise HTTPException(409, "This clip is rendering already.")
    if not pipeline.clean_source(clip).is_file():
        raise HTTPException(409, "The copy without captions is gone, so this clip can't be re-rendered.")
    if body.template and store.get_template(body.template) is None:
        raise HTTPException(422, "No such template.")
    sfx = None
    if body.sfx is not None:
        sfx = [{"sound": templates.sound_name(e.get("sound")), "t": float(e.get("t") or 0),
                "volume": float(e.get("volume") or 1.0)} for e in body.sfx if templates.sound_name(e.get("sound"))]
    store.update_clip(clip.id, render_state="rendering", render_error=None)
    _background(request, pipeline.rerender(clip.id, body.template, sfx))
    clip = store.get_clip(clip.id)
    return clip_out(settings, clip, store.get_job(clip.job_id), store)


def _background(request: Request, coro) -> None:
    """Run a render after answering; keep a reference so the task isn't garbage-collected."""
    task = asyncio.create_task(coro)
    request.app.state.render_tasks = getattr(request.app.state, "render_tasks", set())
    request.app.state.render_tasks.add(task)
    task.add_done_callback(request.app.state.render_tasks.discard)


def _editable(request: Request, clip_id: str) -> Clip:
    store = store_of(request)
    clip = _clip_or_404(store, clip_id)
    if clip.status != "pending":
        raise HTTPException(409, "Only clips waiting for review can be edited.")
    if clip.render_state == "rendering":
        raise HTTPException(409, "This clip is rendering already.")
    if not request.app.state.pipeline.clean_source(clip).is_file():
        raise HTTPException(409, "The copy without captions is gone, so this clip can't be edited.")
    return clip


# ---- the clip editor ------------------------------------------------------------------
class WordIn(BaseModel):
    w: str
    s: float
    e: float


class EditIn(BaseModel):
    template: Optional[str] = None
    sfx: Optional[list[dict[str, Any]]] = None
    edit: Optional[dict[str, Any]] = None
    words: Optional[list[WordIn]] = None
    hook_text: Optional[str] = None       # the hook line ("" = none on this clip)


class RecutIn(BaseModel):
    extend_start: float = 0
    extend_end: float = 0
    framing: Optional[str] = None      # auto | full | track; None keeps the clip's


@router.get("/clips/{clip_id}/editor", dependencies=authed)
async def clip_editor(clip_id: str, request: Request):
    settings, store = settings_of(request), store_of(request)
    clip = _clip_or_404(store, clip_id)
    pipeline = request.app.state.pipeline
    source = pipeline.clean_source(clip)
    if clip.status != "pending" or not source.is_file():
        raise HTTPException(409, "Only clips waiting for review can be edited (their copy without captions "
                                 "is deleted once you decide).")
    duration = await media.probe_duration(source)
    job = store.get_job(clip.job_id)
    openshorts: dict[str, Any] = {"available": False, "source_kept": False, "framing": None, "reason": None}
    if job and job.openshorts_job:
        try:
            edl = await asyncio.wait_for(pipeline.openshorts.edl(job.openshorts_job, clip.idx), 5)
            openshorts.update(available=True, source_kept=bool((edl.get("source") or {}).get("available")),
                              framing=edl.get("framing") or "auto")
            if not openshorts["source_kept"]:
                openshorts["reason"] = ("OpenShorts deleted this clip's source, so it can only be cut shorter, "
                                        "not extended or reframed.")
        except (OpenShortsError, httpx.HTTPError, asyncio.TimeoutError) as e:
            openshorts["reason"] = ("OpenShorts no longer has this clip (it keeps job files about a day)."
                                    if isinstance(e, OpenShortsError) and e.status == 404
                                    else f"OpenShorts didn't answer: {str(e)[:200] or type(e).__name__}")
    version = int(source.stat().st_mtime)
    return {"clip": clip_out(settings, clip, job, store), "source_url": f"/api/clips/{clip.id}/source?v={version}",
            "peaks_url": f"/api/clips/{clip.id}/peaks?v={version}", "source_duration": duration,
            "words": clip.word_list(), "words_original": clip.meta.get("words_original"),
            "edit": editing.normalize(json.loads(clip.edit) if clip.edit else None, duration),
            "sfx": clip.manual_sfx(), "template": clip.template, "openshorts": openshorts}


@router.get("/clips/{clip_id}/source", dependencies=authed)
async def clip_source(clip_id: str, request: Request):
    clip = _clip_or_404(store_of(request), clip_id)
    source = request.app.state.pipeline.clean_source(clip)
    if not source.is_file():
        raise HTTPException(404, "The copy without captions is gone.")
    return FileResponse(source, media_type="video/mp4")


@router.get("/clips/{clip_id}/peaks", dependencies=authed)
async def clip_peaks(clip_id: str, request: Request):
    settings = settings_of(request)
    clip = _clip_or_404(store_of(request), clip_id)
    source = request.app.state.pipeline.clean_source(clip)
    if not source.is_file():
        raise HTTPException(404, "The copy without captions is gone.")
    cache = settings.folder("tmp") / "peaks" / f"{clip.id}-{int(source.stat().st_mtime)}.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    try:
        data = {"per_second": 50, "peaks": await render.peaks(source, 50)}
    except media.MediaError:
        data = {"per_second": 50, "peaks": []}
    cache.parent.mkdir(parents=True, exist_ok=True)
    for old in cache.parent.glob(f"{clip.id}-*.json"):
        old.unlink(missing_ok=True)
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


@router.post("/clips/{clip_id}/edit", dependencies=authed, status_code=202)
async def edit_clip_render(clip_id: str, body: EditIn, request: Request):
    """Save the editor's work (any of: template, hand-placed effects, cuts and zooms, caption words)
    and render the clip again."""
    settings, store = settings_of(request), store_of(request)
    clip = _editable(request, clip_id)
    sfx, words, hook_text = _edit_inputs(request, clip, body)
    store.update_clip(clip.id, render_state="rendering", render_error=None)
    _background(request, request.app.state.pipeline.rerender(clip.id, body.template, sfx, body.edit, words,
                                                             hook_text))
    clip = store.get_clip(clip.id)
    return clip_out(settings, clip, store.get_job(clip.job_id), store)


def _edit_inputs(request: Request, clip: Clip, body: EditIn) -> tuple[Optional[list], Optional[list], Optional[str]]:
    """The editor's effects, words and hook text, cleaned up (None: keep the clip's)."""
    if body.template and store_of(request).get_template(body.template) is None:
        raise HTTPException(422, "No such template.")
    sfx = None
    if body.sfx is not None:
        sfx = [{"sound": templates.sound_name(e.get("sound")), "t": round(float(e.get("t") or 0), 3),
                "volume": float(e.get("volume") or 1.0)} for e in body.sfx if templates.sound_name(e.get("sound"))]
    words = None
    if body.words is not None:
        words = [{"w": w.w.strip(), "s": round(w.s, 3), "e": round(max(w.e, w.s + 0.05), 3)}
                 for w in body.words if w.w.strip()]
    hook_text = body.hook_text[:200] if body.hook_text is not None else None
    if hook_text is not None and "hook_rendered" not in clip.meta:
        raise HTTPException(409, "This clip's hook is part of the video OpenShorts made (before clipbot drew hooks), "
                                 "so it can't be changed. New clips can.")
    return sfx, words, hook_text


def _drafts(request: Request) -> dict[str, dict[str, Any]]:
    request.app.state.drafts = getattr(request.app.state, "drafts", None) or {}
    return request.app.state.drafts


@router.post("/clips/{clip_id}/draft", dependencies=authed, status_code=202)
async def draft_render(clip_id: str, body: EditIn, request: Request):
    """A quick 480p render of the editor's unsaved work (GET .../draft says when it's ready). The clip
    itself doesn't change and Telegram hears nothing."""
    clip = _editable(request, clip_id)
    sfx, words, hook_text = _edit_inputs(request, clip, body)
    drafts = _drafts(request)
    if (drafts.get(clip.id) or {}).get("state") == "rendering":
        return drafts[clip.id]
    state = drafts[clip.id] = {"state": "rendering", "url": None, "error": None}

    async def run() -> None:
        try:
            path = await request.app.state.pipeline.draft(clip.id, body.template, sfx, body.edit, words, hook_text)
            state.update(state="done", url=f"/api/clips/{clip.id}/draft.mp4?v={int(path.stat().st_mtime * 1000)}")
        except (media.MediaError, OSError) as e:
            state.update(state="failed", error=str(e)[:500] or type(e).__name__)

    _background(request, run())
    return state


@router.get("/clips/{clip_id}/draft", dependencies=authed)
async def draft_state(clip_id: str, request: Request):
    return _drafts(request).get(clip_id) or {"state": None, "url": None, "error": None}


@router.get("/clips/{clip_id}/draft.mp4", dependencies=authed)
async def draft_file(clip_id: str, request: Request):
    path = settings_of(request).data_dir / "tmp" / "drafts" / f"{_clip_or_404(store_of(request), clip_id).id}.mp4"
    if not path.is_file():
        raise HTTPException(404, "No draft yet.")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "no-store"})


@router.post("/clips/{clip_id}/hook-suggestions", dependencies=authed)
async def hook_suggestions(clip_id: str, request: Request):
    """Fresh hook lines from Gemini, written from the clip's words."""
    settings, store = settings_of(request), store_of(request)
    clip = _clip_or_404(store, clip_id)
    if not settings.gemini_api_key:
        raise HTTPException(503, "Hook ideas need GEMINI_API_KEY for clipbot (docker compose up -d clipbot after setting it).")
    watch = request.app.state.pipeline.job_streamer(store.get_job(clip.job_id))[0] if store.get_job(clip.job_id) else None
    examples, _ = director.learned(store, (clip.meta or {}).get("streamer") or "")
    hooks = await templates.suggest_hooks(
        settings, clip.word_list(), clip.title, clip.hook, what_happened=(clip.meta or {}).get("what_happened") or "",
        streamer=f"{watch.channel} ({watch.platform.title()})" if watch else "",
        notes=(watch.notes or "") if watch else "", examples=[e["hook"] for e in examples if e["hook"]][:6])
    if hooks is None:
        raise HTTPException(502, "Gemini didn't answer. Try again in a moment.")
    return {"suggestions": hooks}


@router.post("/clips/{clip_id}/recut", dependencies=authed, status_code=202)
async def recut_clip(clip_id: str, body: RecutIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    clip = _editable(request, clip_id)
    job = store.get_job(clip.job_id)
    if not job or not job.openshorts_job:
        raise HTTPException(409, "This clip didn't come from an OpenShorts job.")
    if body.framing not in (None, "auto", "full", "track"):
        raise HTTPException(422, "framing must be auto, full or track.")
    extend_start, extend_end = max(0.0, min(60.0, body.extend_start)), max(0.0, min(60.0, body.extend_end))
    if not extend_start and not extend_end and body.framing is None:
        raise HTTPException(422, "Nothing to change: extend the clip or pick a framing.")
    store.update_clip(clip.id, render_state="rendering", render_error=None)
    _background(request, request.app.state.pipeline.recut(clip.id, extend_start, extend_end, body.framing))
    clip = store.get_clip(clip.id)
    return clip_out(settings, clip, job, store)


# ---- streamers (watched channels) -------------------------------------------------------
class StreamerIn(BaseModel):
    url: str
    auto_scan: bool = True
    auto_clip: int = 0
    template: Optional[str] = None
    backfill: int = 0             # also scan this many of the latest VODs now
    mode: Optional[str] = None    # "viewers": its scans put the most-viewed viewer clips first


class StreamerEdit(BaseModel):
    auto_scan: Optional[bool] = None
    auto_clip: Optional[int] = None
    template: Optional[str] = None    # "" = the default template
    notes: Optional[str] = None       # who's who, nicknames, running jokes ("" = none)
    mode: Optional[str] = None        # "viewers" or "signals"


class BackfillIn(BaseModel):
    count: int = 3


class VodScanIn(BaseModel):
    vod_id: str


def streamer_out(store: Store, watch: Watch) -> dict[str, Any]:
    vods = []
    for vod in store.vods_for_watch(watch.id):
        scans = store.scans_for_url(vod.url)
        scan = scans[0] if scans else None
        vods.append({"vod_id": vod.vod_id, "url": vod.url, "title": vod.title, "duration": vod.duration,
                     "published": vod.published, "seen": vod.seen, "live": bool(vod.live),
                     "scan": {"id": scan.id, "status": scan.status, "moment_count": len(store.moments_for_scan(scan.id)),
                              "error": scan.error} if scan else None})
    template = store.get_template(watch.template) if watch.template else None
    return {"id": watch.id, "platform": watch.platform, "channel": watch.channel,
            "label": f"{watch.platform}/{watch.channel}", "url": channel_url(Channel(watch.platform, watch.channel)),
            "auto_scan": bool(watch.auto_scan), "auto_clip": watch.auto_clip, "template": watch.template,
            "template_name": (template or {}).get("name"), "checked": watch.checked,
            "check_error": watch.check_error, "created": watch.created, "notes": watch.notes or "",
            "mode": watch.mode or "signals", "vods": vods}


def _watch_or_404(store: Store, watch_id: str) -> Watch:
    watch = store.get_watch(watch_id)
    if not watch:
        raise HTTPException(404, "No such streamer.")
    return watch


def _check_template(store: Store, template: Optional[str]) -> Optional[str]:
    if template and store.get_template(template) is None:
        raise HTTPException(422, "No such template.")
    return template or None


@router.get("/streamers", dependencies=authed)
async def list_streamers(request: Request):
    store = store_of(request)
    return {"items": [streamer_out(store, w) for w in store.watches()],
            "interval_minutes": settings_of(request).watch_interval_minutes}


@router.post("/streamers", dependencies=authed, status_code=201)
async def add_streamer(body: StreamerIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    channel = detect_channel(body.url)
    if not channel:
        raise HTTPException(422, "Paste a channel link: twitch.tv/<name>, kick.com/<name> or youtube.com/@<handle>.")
    template = _check_template(store, body.template)
    scout = request.app.state.scout
    try:
        watch, _ = await scout.add_streamer(default_chat(settings), channel, body.auto_scan, body.auto_clip, template,
                                            _mode(body.mode))
    except ValueError as e:
        raise HTTPException(409, str(e))
    except (media.MediaError, httpx.HTTPError) as e:
        raise HTTPException(502, f"Couldn't read {channel.label}'s videos: {str(e)[:300]}")
    if body.backfill > 0:
        scout.backfill(watch, min(10, body.backfill))
    return streamer_out(store, store.get_watch(watch.id))


@router.patch("/streamers/{watch_id}", dependencies=authed)
async def edit_streamer(watch_id: str, body: StreamerEdit, request: Request):
    store = store_of(request)
    _watch_or_404(store, watch_id)
    fields: dict[str, Any] = {}
    if body.auto_scan is not None:
        fields["auto_scan"] = int(body.auto_scan)
    if body.auto_clip is not None:
        fields["auto_clip"] = max(0, min(10, body.auto_clip))
    if body.template is not None:
        fields["template"] = _check_template(store, body.template)
    if body.notes is not None:
        fields["notes"] = body.notes.strip()[:2000] or None
    if body.mode is not None:
        fields["mode"] = _mode(body.mode)
    if fields:
        store.update_watch(watch_id, **fields)
    return streamer_out(store, store.get_watch(watch_id))


@router.delete("/streamers/{watch_id}", dependencies=authed)
async def remove_streamer(watch_id: str, request: Request):
    store = store_of(request)
    _watch_or_404(store, watch_id)
    store.delete_watch(watch_id)
    return {"ok": True}


@router.post("/streamers/{watch_id}/refresh", dependencies=authed)
async def refresh_streamer(watch_id: str, request: Request):
    store = store_of(request)
    watch = _watch_or_404(store, watch_id)
    try:
        await request.app.state.scout.refresh(watch)
    except (media.MediaError, httpx.HTTPError, ValueError) as e:
        raise HTTPException(502, f"Couldn't read the channel's videos: {str(e)[:300]}")
    return streamer_out(store, store.get_watch(watch_id))


@router.post("/streamers/{watch_id}/backfill", dependencies=authed)
async def backfill_streamer(watch_id: str, body: BackfillIn, request: Request):
    store = store_of(request)
    watch = _watch_or_404(store, watch_id)
    scans = request.app.state.scout.backfill(watch, max(1, min(10, body.count)))
    return {"queued": [s.id for s in scans], "streamer": streamer_out(store, store.get_watch(watch_id))}


@router.post("/streamers/{watch_id}/scan", dependencies=authed, status_code=201)
async def scan_streamer_vod(watch_id: str, body: VodScanIn, request: Request):
    store = store_of(request)
    watch = _watch_or_404(store, watch_id)
    vod = next((v for v in store.vods_for_watch(watch.id, limit=200) if v.vod_id == body.vod_id), None)
    if not vod:
        raise HTTPException(404, "That VOD isn't in this streamer's list. Refresh it first.")
    if vod.live:
        raise HTTPException(409, "That stream is still live. It gets scanned when it ends.")
    scan = request.app.state.scout.queue_scan(watch.chat_id, vod.url, watch.id)
    return scan_out(store, scan)


# ---- scans ----------------------------------------------------------------------
SCAN_MODES = ("signals", "viewers")


def _mode(value: Optional[str]) -> Optional[str]:
    """A scan mode as stored: "viewers" (viewer clips first) or None (chat, loudness...)."""
    if value is not None and value not in SCAN_MODES:
        raise HTTPException(422, "The mode is signals or viewers.")
    return "viewers" if value == "viewers" else None


class ScanIn(BaseModel):
    url: str
    mode: Optional[str] = None     # "viewers": the most-viewed viewer clips first


class RescanIn(BaseModel):
    mode: Optional[str] = None     # None: as the scan was


class ClipTopIn(BaseModel):
    count: int = 5
    template: Optional[str] = None


class ScanClipIn(BaseModel):
    start: Union[str, float]
    end: Union[str, float]
    clips: int = 1
    moment_id: Optional[str] = None
    template: Optional[str] = None
    note: Optional[str] = None


def _scan_or_404(store: Store, scan_id: str) -> Scan:
    scan = store.get_scan(scan_id)
    if not scan:
        raise HTTPException(404, "No such scan.")
    return scan


@router.get("/scans", dependencies=authed)
async def list_scans(request: Request, limit: int = 100):
    store = store_of(request)
    return {"items": [scan_out(store, s) for s in store.list_scans(limit=min(limit, 500))]}


@router.get("/scans/{scan_id}", dependencies=authed)
async def get_scan(scan_id: str, request: Request):
    store = store_of(request)
    scan = _scan_or_404(store, scan_id)
    settings = settings_of(request)
    return {**scan_out(store, scan),
            "moments": [moment_out(store, scan, m) for m in store.moments_for_scan(scan.id)],
            "timeline": store.get_timeline(scan.id),
            "logs": store.query_logs(scan_id=scan.id, limit=100),
            "limits": {"min_range_seconds": MIN_RANGE_SECONDS, "max_range_minutes": settings.max_range_minutes,
                       "clip_min_seconds": settings.clip_min_seconds, "max_clips": 15}}


@router.post("/scans", dependencies=authed, status_code=201)
async def create_scan(body: ScanIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "Paste a full VOD link starting with https://")
    scan = request.app.state.scout.queue_scan(default_chat(settings), url, mode=_mode(body.mode) or "signals")
    return scan_out(store, scan)


@router.post("/scans/{scan_id}/rescan", dependencies=authed, status_code=201)
async def rescan(scan_id: str, request: Request, body: Optional[RescanIn] = None):
    store = store_of(request)
    scan = _scan_or_404(store, scan_id)
    mode = _mode(body.mode) if body and body.mode else scan.mode
    return scan_out(store, request.app.state.scout.queue_scan(scan.chat_id, scan.source_url, scan.watch_id,
                                                               mode or "signals"))


@router.post("/scans/{scan_id}/clip-top", dependencies=authed, status_code=201)
async def clip_top(scan_id: str, body: ClipTopIn, request: Request):
    """Clip the scan's top moments that have no job yet, each cut as found."""
    store = store_of(request)
    scan = _scan_or_404(store, scan_id)
    if not 1 <= body.count <= 15:
        raise HTTPException(422, "Clip between 1 and 15 moments.")
    if body.template and store.get_template(body.template) is None:
        raise HTTPException(422, "No such template.")
    free = [m for m in store.moments_for_scan(scan.id) if not m.job_id][:body.count]
    if not free:
        raise HTTPException(409, "Every moment already has a job.")
    jobs = request.app.state.scout.clip_moments(scan, free, body.template or None)
    return {"jobs": [job_out(store, job) for _, job in jobs]}


@router.post("/scans/{scan_id}/clip", dependencies=authed, status_code=201)
async def clip_from_scan(scan_id: str, body: ScanClipIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    scan = _scan_or_404(store, scan_id)
    start, end = _seconds(body.start, "Start"), _seconds(body.end, "End")
    check_range(settings, start, end, scan.duration)
    if not 1 <= body.clips <= 15:
        raise HTTPException(422, "Clips must be between 1 and 15.")
    moment = store.get_moment(body.moment_id) if body.moment_id else None
    if moment and moment.job_id:
        raise HTTPException(409, "That moment already has a job.")
    if body.template and store.get_template(body.template) is None:
        raise HTTPException(422, "No such template.")
    # The moment as Gemini found it: its picks guide the exact cut. A different range (wider) gets
    # what Gemini saw as the note instead, and Gemini picks the clips in it.
    found = moment.details if moment and moment.scan_id == scan.id else {}
    picks = found.get("picks") if moment and (start, end) == (moment.start, moment.end) else None
    note = (body.note or "").strip()[:300] or (None if picks else found.get("what_happened"))
    job = store.create_job(scan.chat_id or default_chat(settings), scan.source_url, start, end, body.clips,
                           template=body.template or None, preset=picks or None, note=note)
    if moment and moment.scan_id == scan.id:
        store.claim_moment(moment.id, job.id)
    request.app.state.pipeline.wake()
    return job_out(store, job)


# ---- logs and delivery ---------------------------------------------------------------
@router.get("/logs", dependencies=authed)
async def logs(request: Request, level: str = "info", source: Optional[str] = None, q: Optional[str] = None,
               job_id: Optional[str] = None, scan_id: Optional[str] = None, before: Optional[int] = None,
               limit: int = 100):
    store = store_of(request)
    items = store.query_logs(levels=LEVELS.get(level, LEVELS["info"]), source=source, text=q,
                             job_id=job_id, scan_id=scan_id, before_id=before, limit=min(limit, 500))
    return {"items": items, "sources": store.log_sources(),
            "next_before": items[-1]["id"] if len(items) == min(limit, 500) else None}


@router.get("/outbox", dependencies=authed)
async def outbox(request: Request):
    items = []
    for e in store_of(request).outbox():
        payload = e["payload"]
        items.append({"id": e["id"], "event": payload.get("event"), "attempts": e["attempts"],
                      "next_try": e["next_try"], "failed": bool(e["dead"]),
                      "text": (payload.get("text") or payload.get("caption") or "")[:500]})
    return {"items": items}


@router.post("/outbox/{event_id}/retry", dependencies=authed)
async def retry_event(event_id: int, request: Request):
    if not store_of(request).retry_event(event_id):
        raise HTTPException(404, "No such message.")
    return {"ok": True}


@router.delete("/outbox/{event_id}", dependencies=authed)
async def delete_event(event_id: int, request: Request):
    if not store_of(request).delete_event(event_id):
        raise HTTPException(404, "No such message.")
    return {"ok": True}


@router.get("/n8n/failures", dependencies=authed)
async def n8n_failures(request: Request):
    settings = settings_of(request)
    if not settings.n8n_api_key:
        return {"configured": False, "items": []}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.n8n_url}/api/v1/executions",
                                    params={"status": "error", "limit": 25},
                                    headers={"X-N8N-API-KEY": settings.n8n_api_key})
        resp.raise_for_status()
        data = resp.json().get("data") or []
    except (httpx.HTTPError, ValueError) as e:
        return {"configured": True, "items": [], "error": f"n8n didn't answer: {e}"}
    return {"configured": True, "items": [
        {"id": str(x.get("id")), "workflow_id": x.get("workflowId"), "started": x.get("startedAt"),
         "stopped": x.get("stoppedAt"), "mode": x.get("mode")} for x in data]}
