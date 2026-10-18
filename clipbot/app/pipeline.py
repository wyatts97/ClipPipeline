"""The job loop: submit to OpenShorts, poll it, copy clips out, tell n8n.

Three background tasks:
  starter  - takes queued jobs one at a time (optional yt-dlp range download, then submit)
  poller   - checks submitted jobs every POLL_SECONDS and collects finished ones
  outbox   - delivers events to the n8n webhook, in order, retrying while n8n is down
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from . import media
from .config import Settings
from .openshorts import OpenShorts, OpenShortsError
from .parsing import format_timestamp
from .store import Clip, Job, Store

log = logging.getLogger("clipbot.pipeline")

CAPTION_LIMIT = 1024  # Telegram media caption limit


def range_label(job: Job) -> str:
    if job.range_start is None or job.range_end is None:
        return "whole video"
    return f"{format_timestamp(job.range_start)}–{format_timestamp(job.range_end)}"


def clip_caption(job: Job, clip: Clip, total: int) -> str:
    lines = [f"🎬 Clip {clip.idx + 1}/{total} · {format_timestamp(clip.duration or 0)}"]
    if clip.title:
        lines.append(clip.title)
    if clip.hook:
        lines.append(f"🪝 {clip.hook}")
    lines.append(f"📼 {job.source_url} ({range_label(job)})")
    lines.append(f"id {clip.id}")
    caption = "\n".join(lines)
    return caption if len(caption) <= CAPTION_LIMIT else caption[:CAPTION_LIMIT - 1] + "…"


class Pipeline:
    def __init__(self, settings: Settings, store: Store, openshorts: OpenShorts,
                 notify_client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.store = store
        self.openshorts = openshorts
        self.notify_client = notify_client or httpx.AsyncClient(timeout=20)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        # A restart mid-download leaves jobs half-started; begin those again.
        for job in self.store.jobs_with_status("downloading", "submitting"):
            self.store.update_job(job.id, status="queued")
        self._tasks = [asyncio.create_task(self._loop(self._start_next, "starter")),
                       asyncio.create_task(self._loop(self._poll_all, "poller")),
                       asyncio.create_task(self._loop(self.flush_outbox, "outbox"))]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.notify_client.aclose()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self, step, name: str) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the loop alive; the next round retries
                log.exception("%s loop error", name)
                await asyncio.sleep(10)

    # ---- starting jobs -----------------------------------------------------
    def openshorts_options(self, job: Job) -> dict[str, Any]:
        s = self.settings
        options: dict[str, Any] = {
            "output_format": "vertical",
            "target_clips": job.target_clips,
            "clip_min_seconds": s.clip_min_seconds,
            "clip_max_seconds": s.clip_max_seconds,
            "auto_hook": s.auto_hook,
            "layouts": [p.strip() for p in s.layouts.split(",") if p.strip()],
        }
        if not s.captions:
            options["captions"] = False
        return options

    async def _start_next(self) -> None:
        queued = self.store.jobs_with_status("queued")
        if not queued:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return
        await self.start_job(queued[0])

    async def start_job(self, job: Job) -> None:
        options = self.openshorts_options(job)
        tmp_dir = self.settings.folder("tmp") / job.id
        try:
            if job.range_start is not None and job.range_end is not None:
                self.store.update_job(job.id, status="downloading")
                log.info("job %s: downloading %s", job.id, range_label(job))
                source = await media.download_section(job.source_url, job.range_start, job.range_end,
                                                      tmp_dir, self.settings.cookies_file)
                self.store.update_job(job.id, status="submitting")
                upload_id = await self.openshorts.upload_file(source)
                os_job = await self.openshorts.process(upload_id=upload_id, options=options)
            else:
                self.store.update_job(job.id, status="submitting")
                os_job = await self.openshorts.process(url=job.source_url, options=options)
        except (media.MediaError, OpenShortsError, httpx.HTTPError) as e:
            self.fail(job, str(e) or type(e).__name__)
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        self.store.update_job(job.id, status="processing", openshorts_job=os_job)
        log.info("job %s: submitted as OpenShorts job %s", job.id, os_job)

    # ---- polling -----------------------------------------------------------
    async def _poll_all(self) -> None:
        for job in self.store.jobs_with_status("processing"):
            await self.poll_job(job)
        await asyncio.sleep(self.settings.poll_seconds)

    async def poll_job(self, job: Job) -> None:
        try:
            doc = await self.openshorts.status(job.openshorts_job)
        except (OpenShortsError, httpx.HTTPError) as e:
            log.warning("job %s: status check failed (%s); will retry", job.id, e)
            return
        if doc is None:
            self.fail(job, "OpenShorts no longer knows this job (its files may have expired).")
            return
        status = doc.get("status")
        if status == "completed":
            await self.collect(job, doc)
        elif status == "failed":
            logs = [str(line) for line in (doc.get("logs") or [])][-3:]
            self.fail(job, "OpenShorts failed: " + (" | ".join(logs) or "no log output"))
        elif time.time() - job.created > self.settings.job_timeout_minutes * 60:
            self.fail(job, f"Gave up after {self.settings.job_timeout_minutes} minutes "
                           f"(OpenShorts status: {status}).")

    async def collect(self, job: Job, doc: dict[str, Any]) -> None:
        s = self.settings
        results = (doc.get("result") or {}).get("clips") or []
        pending, previews = s.folder("pending"), s.folder("previews")
        made: list[Clip] = []
        problems: list[str] = []
        for idx, info in enumerate(results):
            video_url = info.get("video_url")
            if not video_url:
                continue
            master = pending / f"{job.id}_{idx}.mp4"
            preview = previews / f"{job.id}_{idx}.mp4"
            try:
                local = s.openshorts_output_dir / job.openshorts_job / video_url.rsplit("/", 1)[-1]
                if local.is_file():
                    shutil.copyfile(local, master)
                else:
                    await self.openshorts.download(video_url, master)
                duration = await media.probe_duration(master)
                await media.make_preview(master, preview, s.preview_max_mb, s.preview_height)
            except (media.MediaError, OpenShortsError, httpx.HTTPError, OSError) as e:
                problems.append(f"clip {idx + 1}: {e}")
                continue
            meta = {k: info.get(k) for k in (
                "start", "end", "video_title_for_youtube_short", "video_description_for_tiktok",
                "video_description_for_instagram", "viral_hook_text")}
            title = info.get("title") or info.get("video_title_for_youtube_short")
            made.append(self.store.add_clip(job.id, idx, title, info.get("viral_hook_text"), meta,
                                            duration, str(master), str(preview)))
        self.store.update_job(job.id, status="done")

        summary = f"✅ Job {job.id} finished: {len(made)} clip(s) from {range_label(job)}."
        if problems:
            summary += "\n⚠️ " + "\n⚠️ ".join(problems)
        if not made:
            summary = f"😕 Job {job.id} finished but produced no usable clips." + (
                "\n⚠️ " + "\n⚠️ ".join(problems) if problems else "")
        self.store.enqueue_event({"event": "message", "chat_id": job.chat_id, "text": summary})
        for clip in made:
            self.store.enqueue_event({
                "event": "clip_ready",
                "chat_id": job.chat_id,
                "job_id": job.id,
                "clip_id": clip.id,
                "caption": clip_caption(job, clip, len(made)),
                "preview_url": f"{s.self_url}/previews/{clip.id}",
                "approve_data": f"ok:{clip.id}",
                "skip_data": f"no:{clip.id}",
            })
        log.info("job %s: collected %d clip(s)", job.id, len(made))

    def fail(self, job: Job, error: str) -> None:
        log.warning("job %s failed: %s", job.id, error)
        self.store.update_job(job.id, status="failed", error=error[:2000])
        self.store.enqueue_event({"event": "message", "chat_id": job.chat_id,
                                  "text": f"❌ Job {job.id} failed: {error[:3500]}"})

    # ---- decisions ---------------------------------------------------------
    def decide(self, chat_id: int, data: str) -> tuple[str, Optional[str]]:
        """Handle a button press. Returns (toast for answerCallbackQuery, chat reply or None)."""
        action, _, clip_id = (data or "").partition(":")
        clip = self.store.get_clip(clip_id) if action in ("ok", "no") else None
        job = self.store.get_job(clip.job_id) if clip else None
        if not clip or not job or job.chat_id != chat_id:
            return "Unknown clip", None
        if clip.status != "pending":
            return f"Already {clip.status}", None

        if action == "ok":
            stamp = datetime.now().strftime("%Y-%m-%d")
            dest = self.settings.folder("approved") / f"{stamp}_{media.slugify(clip.title)}_{clip.id}.mp4"
            status, reply = "approved", "✅ Approved → approved/{name}"
        else:
            dest = self.settings.folder("rejected") / f"{clip.id}.mp4"
            status, reply = "rejected", "❌ Skipped clip {id}"

        try:
            shutil.move(clip.path, dest)
        except FileNotFoundError:
            return "Clip file is gone", f"⚠️ The file for clip {clip.id} is no longer in pending/."
        if status == "approved":
            dest.with_suffix(".json").write_text(json.dumps({
                "clip_id": clip.id, "job_id": job.id, "title": clip.title, "hook": clip.hook,
                "source_url": job.source_url, "range": range_label(job),
                "approved_at": datetime.now().isoformat(timespec="seconds"), **clip.meta,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        if clip.preview_path:
            Path(clip.preview_path).unlink(missing_ok=True)
        self.store.decide_clip(clip.id, status, str(dest))
        return status.capitalize(), reply.format(name=dest.name, id=clip.id)

    # ---- outbox ------------------------------------------------------------
    async def flush_outbox(self) -> None:
        headers = {"X-Clipbot-Token": self.settings.notify_token}
        for event_id, payload, attempts in self.store.due_events():
            try:
                resp = await self.notify_client.post(self.settings.notify_url, json=payload,
                                                     headers=headers)
                ok = resp.status_code < 300
                reason = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                ok, reason = False, str(e) or type(e).__name__
            if ok:
                self.store.event_delivered(event_id)
                continue
            attempts += 1
            dead = attempts >= 40
            self.store.event_failed(event_id, attempts, min(300, 5 * 2 ** attempts), dead)
            log.warning("event %d not delivered to n8n (%s), attempt %d%s", event_id, reason,
                        attempts, " - giving up" if dead else "")
            break  # keep order: later events wait for this one
        await asyncio.sleep(2)
