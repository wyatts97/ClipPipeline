"""clipbot's HTTP server.

For n8n (X-Clipbot-Token):
POST /messages   {chat_id, text}  -> {reply}           a Telegram text message
POST /decisions  {chat_id, data}  -> {toast, reply}    a button press (ok:/no: clips, sc: moments)
GET  /previews/{clip_id}                               the small preview n8n sends to Telegram

For the browser (DASHBOARD_PASSWORD login): /api/* (dashboard.py) and the dashboard itself at /.
GET  /health
"""
from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import dashboard, logbook
from .config import Settings
from .openshorts import OpenShorts
from .parsing import format_timestamp, parse_message
from .pipeline import Pipeline, range_label
from .scout import Scout
from .store import Store

log = logging.getLogger("clipbot")

HELP = """Send me a video link and I'll cut it into vertical clips, then send each one here for you to approve.

Examples:
https://www.twitch.tv/videos/123456789 1:02:00-1:12:00
https://youtu.be/VIDEO_ID clips=5

• A time range (h:mm:ss-h:mm:ss) processes only that part. Much faster on this server, so use it for long streams.
• clips=N sets how many clips to make (1-15).

🔎 /scan <VOD link> finds a stream's best moments (chat spikes, hype, loudness, viewer clips) and sends each with a 🎬 button.
👀 /watch <channel link> scans every new VOD when the stream ends. /watching lists them, /unwatch <channel> stops.
📋 /status lists your recent jobs and scans.

✅ Post moves a clip to approved/, ❌ Skip moves it to rejected/."""


class MessageIn(BaseModel):
    chat_id: int
    text: Optional[str] = ""


class DecisionIn(BaseModel):
    chat_id: int
    data: str


def create_app(settings: Optional[Settings] = None, openshorts: Optional[OpenShorts] = None,
               pipeline_factory=Pipeline, run_background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = Store(settings.db_path)
        log_handler = logbook.install(store)
        # yt-dlp and TwitchDownloaderCLI expect $HOME to exist; on a fresh ./data it doesn't.
        for folder in (os.environ.get("HOME"), os.environ.get("XDG_CACHE_HOME")):
            if folder:
                try:
                    Path(folder).mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    log.warning("can't create %s: %s", folder, e)
        client = openshorts or OpenShorts(settings.openshorts_url)
        pipeline = pipeline_factory(settings, store, client)
        scout = Scout(settings, store, pipeline)
        app.state.store, app.state.pipeline, app.state.scout = store, pipeline, scout
        if run_background:
            await pipeline.start()
            await scout.start()
        if not settings.notify_token:
            log.warning("CLIPBOT_TOKEN is empty: the API is unauthenticated")
        try:
            yield
        finally:
            if run_background:
                await scout.stop()
                await pipeline.stop()
            await client.aclose()
            logbook.uninstall(log_handler)
            store.close()

    app = FastAPI(title="clipbot", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(dashboard.router)

    def require_token(x_clipbot_token: Optional[str] = Header(default=None)) -> None:
        expected = settings.notify_token
        if expected and not hmac.compare_digest(x_clipbot_token or "", expected):
            raise HTTPException(status_code=401, detail="bad or missing X-Clipbot-Token")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/messages", dependencies=[Depends(require_token)])
    async def messages(body: MessageIn):
        return {"reply": await handle_message(app, settings, body.chat_id, body.text or "")}

    @app.post("/decisions", dependencies=[Depends(require_token)])
    async def decisions(body: DecisionIn):
        if body.chat_id not in settings.allowed_chat_ids:
            return {"toast": "Not allowed", "reply": None}
        if body.data.startswith("sc:"):
            toast, reply = app.state.scout.choose(body.chat_id, body.data)
        else:
            toast, reply = app.state.pipeline.decide(body.chat_id, body.data)
        return {"toast": toast, "reply": reply}

    @app.get("/previews/{clip_id}", dependencies=[Depends(require_token)])
    async def preview(clip_id: str):
        clip = app.state.store.get_clip(clip_id)
        if not clip or not clip.preview_path or not Path(clip.preview_path).is_file():
            raise HTTPException(status_code=404, detail="no preview for this clip")
        return FileResponse(clip.preview_path, media_type="video/mp4", filename=f"{clip_id}.mp4")

    web_dir = settings.web_dir.resolve()

    @app.get("/{path:path}", include_in_schema=False)
    async def web(path: str):
        """The dashboard: built files from clipbot/web, index.html for every client-side route."""
        if path.startswith("api/"):
            raise HTTPException(404, "No such endpoint.")
        target = (web_dir / path).resolve()
        if path and target.is_file() and web_dir in target.parents:
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(target, headers={"Cache-Control": cache})
        index = web_dir / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<p>The dashboard isn't built. Rebuild the clipbot image: "
                            "<code>docker compose build clipbot</code></p>", status_code=404)

    return app


async def handle_message(app: FastAPI, settings: Settings, chat_id: int, text: str) -> Optional[str]:
    if chat_id not in settings.allowed_chat_ids:
        if not settings.allowed_chat_ids:
            return (f"👋 Setup: your chat id is {chat_id}.\nPut ALLOWED_CHAT_IDS={chat_id} in .env, "
                    "then run: docker compose up -d clipbot")
        log.warning("ignoring message from chat %s (not in ALLOWED_CHAT_IDS)", chat_id)
        return None

    store: Store = app.state.store
    scout: Scout = app.state.scout
    cmd = parse_message(text, max_range_minutes=settings.max_range_minutes)
    if cmd.kind == "help":
        return HELP
    if cmd.kind == "invalid":
        return f"⚠️ {cmd.error}"
    if cmd.kind == "status":
        jobs, scans = store.recent_jobs(chat_id), store.recent_scans(chat_id)
        if not jobs and not scans:
            return "No jobs yet. Send me a video link, or /scan one."
        lines = []
        for job in jobs:
            line = f"• job {job.id} · {job.status} · {range_label(job)}\n  {job.source_url}"
            if job.status == "failed" and job.error:
                line += f"\n  {job.error[:200]}"
            lines.append(line)
        for scan in scans:
            line = f"• scan {scan.id} · {scan.status}\n  {scan.title or scan.source_url}"
            if scan.status == "failed" and scan.error:
                line += f"\n  {scan.error[:200]}"
            lines.append(line)
        return "Recent:\n" + "\n".join(lines)
    if cmd.kind == "scan":
        scan = scout.queue_scan(chat_id, cmd.url)
        waiting = len(store.scans_with_status("queued", "running"))
        ahead = f" {waiting - 1} scan(s) ahead of it." if waiting > 1 else ""
        return (f"🔎 Scan {scan.id} queued.{ahead} I'll read the chat replay, loudness and viewer clips, "
                "then send the top moments. A long stream takes a few minutes.")
    if cmd.kind == "watch":
        return await scout.add_watch(chat_id, cmd.arg or "")
    if cmd.kind == "unwatch":
        return scout.remove_watch(chat_id, cmd.arg or "")
    if cmd.kind == "watching":
        return scout.list_watches(chat_id)

    job = store.create_job(chat_id, cmd.url, cmd.start, cmd.end,
                           cmd.target_clips or settings.target_clips)
    app.state.pipeline.wake()
    position = store.queue_position(job.id)
    part = (f"{format_timestamp(cmd.start)}–{format_timestamp(cmd.end)} "
            f"({format_timestamp(cmd.end - cmd.start)})" if cmd.start is not None else "the whole video")
    ahead = f" {position - 1} job(s) ahead of it." if position > 1 else ""
    return (f"👍 Job {job.id} queued: {job.target_clips} clip(s) from {part}.{ahead}\n"
            "Clips arrive here when OpenShorts is done. On CPU that can take a while.")


