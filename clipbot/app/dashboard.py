"""JSON API for the web dashboard, behind a single-password login (DASHBOARD_PASSWORD).

The dashboard is served by clipbot on the LAN; nothing here is routed through the tunnel.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import shutil
import time
from pathlib import Path
from typing import Any, Optional, Union

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import captions, fonts, media, storage, templates
from .config import Settings
from .parsing import format_timestamp, parse_timestamp
from .platforms import detect_vod, timestamp_link
from .postiz import PostizError
from .publisher import next_free_slot
from .store import Clip, Job, MomentRow, Post, Scan, Store

COOKIE = "cp_session"
SESSION_DAYS = 30
MIN_SOURCE_SECONDS = 45          # OpenShorts refuses shorter sources
LEVELS = {"error": ["ERROR", "CRITICAL"], "warning": ["WARNING", "ERROR", "CRITICAL"],
          "info": ["INFO", "WARNING", "ERROR", "CRITICAL"]}
ACTIVE = ("queued", "downloading", "submitting", "processing")

router = APIRouter(prefix="/api")
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
    }


def thumb_path(settings: Settings, clip: Clip) -> Path:
    return settings.data_dir / "thumbs" / f"{clip.job_id}_{clip.idx}.jpg"


def clip_out(settings: Settings, clip: Clip, job: Optional[Job], store: Optional[Store] = None) -> dict[str, Any]:
    meta = clip.meta or {}
    video = Path(clip.path)
    version = int(video.stat().st_mtime) if video.is_file() else 0
    clean = settings.data_dir / "clean" / f"{clip.job_id}_{clip.idx}.mp4"
    template = store.get_template(clip.template) if store and clip.template else None
    return {
        "template": clip.template, "template_name": (template or {}).get("name"),
        "sfx": clip.manual_sfx(), "render_state": clip.render_state, "render_error": clip.render_error,
        "can_render": clip.status == "pending" and clean.is_file(), "credit": meta.get("credit"),
        "word_count": len(clip.word_list()),
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
        "vod_id": vod.vod_id, "moment_count": len(store.moments_for_scan(scan.id)),
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
    if end - start < MIN_SOURCE_SECONDS:
        raise HTTPException(422, f"Make the range at least {MIN_SOURCE_SECONDS} seconds; OpenShorts needs that much.")
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
    job = store.create_job(default_chat(settings), url, start, end, clips, template=body.template or None)
    request.app.state.pipeline.wake()
    return job_out(store, job)


@router.post("/jobs/{job_id}/retry", dependencies=authed, status_code=201)
async def retry_job(job_id: str, request: Request):
    store = store_of(request)
    job = _job_or_404(store, job_id)
    if job.status not in ("failed", "cancelled"):
        raise HTTPException(409, "Only failed or cancelled jobs can be retried.")
    new = store.create_job(job.chat_id, job.source_url, job.range_start, job.range_end, job.target_clips,
                           retry_of=job.id, template=job.template)
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

    async def item(path: Path) -> dict[str, Any]:
        try:
            duration = await media.probe_duration(path)
        except media.MediaError:
            duration = None
        return {"name": path.stem, "file": path.name, "size": path.stat().st_size, "duration": duration}

    return list(await asyncio.gather(*(item(p) for p in paths)))


@router.get("/templates", dependencies=authed)
async def list_templates(request: Request):
    settings, store = settings_of(request), store_of(request)
    return {"items": [template_out(settings, store, item) for item in store.list_templates()],
            "default": store.get_setting("default_template"),
            "presets": captions.PRESETS, "effects": list(captions.EFFECTS), "positions": list(captions.POSITIONS),
            "triggers": list(templates.TRIGGERS), "fonts": fonts.list_fonts(settings),
            "sounds": [s["name"] for s in await _sound_items(settings)],
            "punchlines": bool(settings.gemini_api_key)}


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
    return {"items": await _sound_items(settings_of(request))}


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
        if old.stem.lower() == name.lower():
            old.unlink()                   # replacing a sound of the same name, maybe another format
    path = templates.sounds_dir(settings) / f"{name}{suffix}"
    path.write_bytes(body)
    try:
        duration = await media.probe_duration(path)
    except media.MediaError:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "That file isn't audio ffmpeg can read.")
    return {"name": name, "file": path.name, "size": len(body), "duration": duration}


@router.delete("/sounds/{name}", dependencies=authed)
async def delete_sound(name: str, request: Request):
    path = templates.find_sound(settings_of(request), templates.sound_name(name))
    if not path:
        raise HTTPException(404, "No such sound.")
    path.unlink()
    return {"ok": True}


@router.get("/sounds/{name}/file", dependencies=authed)
async def sound_file(name: str, request: Request):
    path = templates.find_sound(settings_of(request), templates.sound_name(name))
    if not path:
        raise HTTPException(404, "No such sound.")
    return FileResponse(path, media_type=AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream"))


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
    return {"family": family, "file": safe, "bundled": False}


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
    task = asyncio.create_task(pipeline.rerender(clip.id, body.template, sfx))
    request.app.state.render_tasks = getattr(request.app.state, "render_tasks", set())
    request.app.state.render_tasks.add(task)
    task.add_done_callback(request.app.state.render_tasks.discard)
    clip = store.get_clip(clip.id)
    return clip_out(settings, clip, store.get_job(clip.job_id), store)


# ---- scans ----------------------------------------------------------------------
class ScanIn(BaseModel):
    url: str


class ScanClipIn(BaseModel):
    start: Union[str, float]
    end: Union[str, float]
    clips: int = 1
    moment_id: Optional[str] = None
    template: Optional[str] = None


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
    return {**scan_out(store, scan),
            "moments": [moment_out(store, scan, m) for m in store.moments_for_scan(scan.id)],
            "timeline": store.get_timeline(scan.id),
            "logs": store.query_logs(scan_id=scan.id, limit=100)}


@router.post("/scans", dependencies=authed, status_code=201)
async def create_scan(body: ScanIn, request: Request):
    settings, store = settings_of(request), store_of(request)
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(422, "Paste a full VOD link starting with https://")
    scan = request.app.state.scout.queue_scan(default_chat(settings), url)
    return scan_out(store, scan)


@router.post("/scans/{scan_id}/rescan", dependencies=authed, status_code=201)
async def rescan(scan_id: str, request: Request):
    store = store_of(request)
    scan = _scan_or_404(store, scan_id)
    return scan_out(store, request.app.state.scout.queue_scan(scan.chat_id, scan.source_url))


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
    job = store.create_job(scan.chat_id or default_chat(settings), scan.source_url, start, end, body.clips,
                           template=body.template or None)
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
