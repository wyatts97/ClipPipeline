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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

from . import captions, editing, fonts, media, render, storage, templates
from .config import Settings
from .diagnose import failure_hint, failure_reason, retry_delay
from .openshorts import OpenShorts, OpenShortsError
from .parsing import format_timestamp
from .store import Clip, Job, Store

log = logging.getLogger("clipbot.pipeline")

CAPTION_LIMIT = 1024  # Telegram media caption limit


def range_label(job: Job) -> str:
    if job.range_start is None or job.range_end is None:
        return "whole video"
    return f"{format_timestamp(job.range_start)}–{format_timestamp(job.range_end)}"


def clip_caption(job: Job, clip: Clip, total: int, template_name: Optional[str] = None) -> str:
    lines = [f"🎬 Clip {clip.idx + 1}/{total} · {format_timestamp(clip.duration or 0)}"
             + (f" · 🎨 {template_name}" if template_name else "")]
    if clip.title:
        lines.append(clip.title)
    if clip.hook:
        lines.append(f"🪝 {clip.hook}")
    lines.append(f"📼 {job.source_url} ({range_label(job)})")
    lines.append(f"id {clip.id}")
    caption = "\n".join(lines)
    return caption if len(caption) <= CAPTION_LIMIT else caption[:CAPTION_LIMIT - 1] + "…"


@dataclass
class Rendered:
    template_id: str
    template: dict[str, Any]
    moments: Optional[list[dict[str, Any]]] = None   # Gemini's moments, cached on the clip
    notes: list[str] = field(default_factory=list)   # e.g. a sound that isn't in data/sounds
    error: Optional[str] = None                      # the render failed; the clip has no template
    duration: Optional[float] = None                 # of the finished clip (after the editor's cuts)


class Pipeline:
    def __init__(self, settings: Settings, store: Store, openshorts: OpenShorts,
                 notify_client: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.store = store
        self.openshorts = openshorts
        self.notify_client = notify_client or httpx.AsyncClient(timeout=20)
        self.publisher = None   # set by main.create_app when publishing is configured
        self.render_lock = asyncio.Lock()   # one template render at a time (CPU)
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
            # clipbot burns the captions itself, in the clip's template (render_clip_files)
            "captions": False,
        }
        return options

    def ready_jobs(self) -> list[Job]:
        """Queued jobs whose start time has come (automatic retries are queued for later)."""
        now = time.time()
        return [j for j in self.store.jobs_with_status("queued") if j.created <= now]

    async def _start_next(self) -> None:
        queued = self.ready_jobs()
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
                log.info("job %s: downloading %s", job.id, range_label(job), extra={"job_id": job.id})
                source = await media.download_section(job.source_url, job.range_start, job.range_end,
                                                      tmp_dir, self.settings.cookies_file)
                if self.cancelled(job.id):
                    return
                self.store.update_job(job.id, status="submitting")
                upload_id = await self.openshorts.upload_file(source)
                os_job = await self.openshorts.process(upload_id=upload_id, options=options)
            else:
                self.store.update_job(job.id, status="submitting")
                os_job = await self.openshorts.process(url=job.source_url, options=options)
        except (media.MediaError, OpenShortsError, httpx.HTTPError) as e:
            if not self.cancelled(job.id):
                self.fail(job, str(e) or type(e).__name__)
            return
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if self.cancelled(job.id):
            log.info("job %s: cancelled; ignoring OpenShorts job %s", job.id, os_job, extra={"job_id": job.id})
            return
        self.store.update_job(job.id, status="processing", openshorts_job=os_job)
        log.info("job %s: submitted as OpenShorts job %s", job.id, os_job, extra={"job_id": job.id})

    def cancelled(self, job_id: str) -> bool:
        job = self.store.get_job(job_id)
        return job is not None and job.status == "cancelled"

    def cancel(self, job_id: str) -> bool:
        """Stop a job that hasn't finished. OpenShorts keeps rendering a submitted one; its result
        is ignored."""
        job = self.store.get_job(job_id)
        if not job or job.status not in ("queued", "downloading", "submitting", "processing"):
            return False
        self.store.update_job(job_id, status="cancelled", error="Cancelled from the dashboard")
        log.info("job %s: cancelled from the dashboard", job_id, extra={"job_id": job_id})
        return True

    # ---- polling -----------------------------------------------------------
    async def _poll_all(self) -> None:
        for job in self.store.jobs_with_status("processing"):
            await self.poll_job(job)
        await asyncio.sleep(self.settings.poll_seconds)

    async def poll_job(self, job: Job) -> None:
        try:
            doc = await self.openshorts.status(job.openshorts_job)
        except (OpenShortsError, httpx.HTTPError) as e:
            log.warning("job %s: status check failed (%s); will retry", job.id, e, extra={"job_id": job.id})
            return
        if doc is None:
            self.fail(job, "OpenShorts no longer knows this job (its files may have expired).")
            return
        status = doc.get("status")
        if status == "completed":
            await self.collect(job, doc)
        elif status == "failed":
            lines = [str(line) for line in (doc.get("logs") or [])]
            hint = failure_hint(lines)
            reason = "OpenShorts failed: " + failure_reason(lines)
            if hint:
                reason += f"\n💡 {hint}"
            retry = self.auto_retry(job, retry_delay(lines))
            if retry:
                reason += f"\n🔁 Retrying automatically in {retry_delay(lines) // 60} min as job {retry.id}."
            self.fail(job, reason, detail="\n".join(lines[-150:]) or None)
        elif time.time() - job.created > self.settings.job_timeout_minutes * 60:
            self.fail(job, f"Gave up after {self.settings.job_timeout_minutes} minutes "
                           f"(OpenShorts status: {status}).")

    async def collect(self, job: Job, doc: dict[str, Any]) -> None:
        s = self.settings
        results = (doc.get("result") or {}).get("clips") or []
        pending, previews, thumbs = s.folder("pending"), s.folder("previews"), s.folder("thumbs")
        clean = s.folder("clean")
        made: list[Clip] = []
        names: dict[str, str] = {}
        problems: list[str] = []
        for idx, info in enumerate(results):
            video_url = info.get("video_url")
            if not video_url:
                continue
            source = clean / f"{job.id}_{idx}.mp4"       # as OpenShorts made it: no captions
            master = pending / f"{job.id}_{idx}.mp4"
            preview = previews / f"{job.id}_{idx}.mp4"
            meta = {k: info.get(k) for k in (
                "start", "end", "video_title_for_youtube_short", "video_description_for_tiktok",
                "video_description_for_instagram", "viral_hook_text")}
            try:
                local = s.openshorts_output_dir / job.openshorts_job / video_url.rsplit("/", 1)[-1]
                if local.is_file():
                    shutil.copyfile(local, source)
                else:
                    await self.openshorts.download(video_url, source)
                duration = await media.probe_duration(source)
                words, splits = await self.clip_text(job, idx, local)
                done = await self.render_clip_files(job, source, master, words, splits, job.template,
                                                    [], meta, duration, seed=f"{job.id}_{idx}")
                await media.make_preview(master, preview, s.preview_max_mb, s.preview_height)
            except (media.MediaError, OpenShortsError, httpx.HTTPError, OSError) as e:
                problems.append(f"clip {idx + 1}: {e}")
                log.warning("job %s: clip %d unusable: %s", job.id, idx + 1, e, extra={"job_id": job.id})
                source.unlink(missing_ok=True)
                continue
            try:
                await media.make_thumbnail(master, thumbs / f"{job.id}_{idx}.jpg")
            except media.MediaError as e:  # the dashboard falls back to the video itself
                log.warning("job %s: no thumbnail for clip %d: %s", job.id, idx + 1, e, extra={"job_id": job.id})
            problems += [f"clip {idx + 1}: {note}" for note in done.notes]
            if done.error:
                problems.append(f"clip {idx + 1}: the {done.template['name']} template failed ({done.error[:200]}); "
                                "it has no captions. Re-render it from the dashboard's Review page.")
            if not words and s.captions and done.template["captions"]["enabled"]:
                problems.append(f"clip {idx + 1}: no words from OpenShorts, so no captions")
            if done.moments is not None:
                meta["moments"] = done.moments
            if done.template.get("credit"):
                meta["credit"] = done.template["credit"]
            title = info.get("title") or info.get("video_title_for_youtube_short")
            clip = self.store.add_clip(job.id, idx, title, info.get("viral_hook_text"), meta,
                                       duration, str(master), str(preview))
            self.store.update_clip(clip.id, template=done.template_id, words=json.dumps(words) if words else None,
                                   splits=json.dumps(splits) if splits else None,
                                   render_error=done.error[:1000] if done.error else None)
            made.append(self.store.get_clip(clip.id))
            names[clip.id] = done.template["name"]
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
                "caption": clip_caption(job, clip, len(made), names.get(clip.id)),
                "preview_url": f"{s.self_url}/previews/{clip.id}",
                "approve_data": f"ok:{clip.id}",
                "skip_data": f"no:{clip.id}",
            })
        log.info("job %s: collected %d clip(s)", job.id, len(made), extra={"job_id": job.id})

    # ---- templates -------------------------------------------------------------
    async def clip_text(self, job: Job, idx: int, local: Path) -> tuple[list[dict[str, Any]], list[tuple[float, float]]]:
        """The clip's words from OpenShorts, and where it stacked two speakers (layout sidecar)."""
        words: list[dict[str, Any]] = []
        try:
            words = captions.words_from_openshorts(await self.openshorts.transcript(job.openshorts_job, idx))
        except (OpenShortsError, httpx.HTTPError, ValueError) as e:
            log.warning("job %s: no words for clip %d: %s", job.id, idx + 1, e, extra={"job_id": job.id})
        splits: list[tuple[float, float]] = []
        sidecar = Path(str(local) + ".layout.json")
        if sidecar.is_file():
            try:
                splits = captions.split_ranges_from_layout(json.loads(sidecar.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
        return words, splits

    def resolve_template(self, template_id: Optional[str]) -> tuple[str, dict[str, Any]]:
        """The template to use: the one asked for, else the default, else the built-in Standard."""
        for tid in (template_id, self.store.get_setting("default_template"), templates.DEFAULT_TEMPLATE):
            data = self.store.get_template(tid)
            if data:
                return tid, templates.normalize(data)
        return templates.DEFAULT_TEMPLATE, templates.normalize(templates.BUILT_IN[templates.DEFAULT_TEMPLATE])

    async def render_clip_files(self, job: Job, source: Path, dst: Path, words: list[dict[str, Any]],
                                splits: list[tuple[float, float]], template_id: Optional[str],
                                manual_sfx: list[dict[str, Any]], meta: dict[str, Any],
                                duration: float, seed: str = "",
                                edit: Optional[dict[str, Any]] = None) -> Rendered:
        """Render source (no captions) into dst with a template and the editor's edit. A failed
        render copies the source, so the clip still exists; the error says why. Words, effects and
        zooms come in on the source's timeline and are moved onto the edited one here."""
        s = self.settings
        tid, template = self.resolve_template(template_id)
        done = Rendered(tid, template, moments=templates.cached_moments(meta))
        work = s.folder("tmp") / f"render-{dst.stem}"
        work.mkdir(parents=True, exist_ok=True)
        edit = editing.normalize(edit, duration)
        segments = edit["segments"]
        cut = editing.is_cut(edit, duration)
        out_duration = editing.output_duration(edit) if cut else duration
        done.duration = out_duration
        plan = render.Plan(duration=out_duration, voice_volume=template["voice_volume"],
                           segments=segments if cut else None, zooms=editing.zoom_ranges(edit))
        try:
            if plan.zooms:
                plan.size = await render.video_size(source)
            shown = editing.map_words(words, segments) if cut else words
            shown_splits = [r for a, b in splits for r in editing.map_range(a, b, segments)] if cut else splits
            style = template["captions"]
            if s.captions and style["enabled"] and shown:
                ass = work / "captions.ass"
                if captions.write_ass(ass, shown, style, shown_splits):
                    plan.ass, plan.fonts_dir = ass, fonts.render_fonts_dir(s)
            music = template["music"]
            if music["sound"]:
                path = templates.find_sound(s, music["sound"])
                if path:
                    plan.music = render.Music(path, music["volume"], music["duck"], music["start"])
                else:
                    done.notes.append(f"the {template['name']} template's music ({music['sound']}) isn't in "
                                      "data/sounds; upload it on the dashboard's Templates page")
            if templates.wants_moments(template) and done.moments is None and words:
                done.moments = await templates.find_moments(s, words)
                if done.moments is None:     # asked again on the next render
                    done.notes.append("moment effects were skipped: " + (
                        "Gemini didn't answer" if s.gemini_api_key else "clipbot needs GEMINI_API_KEY"))
            moment = self.store.moment_for_job(job.id)
            peak = templates.peak_in_clip(moment.peak if moment else None,
                                          (job.range_start or 0.0) + float(meta.get("start") or 0.0)
                                          if moment else None)
            moments, manual = done.moments, manual_sfx
            if cut:
                peak = editing.map_time(peak, segments) if peak is not None else None
                moments = editing.map_points(moments or [], segments) if moments is not None else None
                manual = editing.map_points(manual_sfx, segments)
            missing = set()
            for event in templates.place_effects(template, shown, out_duration, peak, moments, manual, seed):
                path = templates.find_sound(s, event["sound"])
                if path:
                    plan.effects.append(render.Effect(path, event["t"], event["volume"]))
                else:
                    missing.add(event["sound"])
            if missing:
                done.notes.append("sound effect(s) not in data/sounds: " + ", ".join(sorted(missing)))
            plan.has_audio = await render.has_audio(source)
            out = work / "out.mp4"
            async with self.render_lock:
                rendered = await render.render(source, out, plan)
            if rendered:
                shutil.move(str(out), dst)
            else:
                shutil.copyfile(source, dst)
        except (media.MediaError, OSError) as e:
            log.warning("job %s: %s template render failed: %s", job.id, template["name"], e,
                        extra={"job_id": job.id})
            done.error = str(e) or type(e).__name__
            shutil.copyfile(source, dst)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return done

    def clean_source(self, clip: Clip) -> Path:
        return self.settings.data_dir / "clean" / f"{clip.job_id}_{clip.idx}.mp4"

    async def rerender(self, clip_id: str, template_id: Optional[str] = None,
                       sfx: Optional[list[dict[str, Any]]] = None, edit: Optional[dict[str, Any]] = None,
                       words: Optional[list[dict[str, Any]]] = None) -> None:
        """Render a pending clip again: another template, effects placed by hand, and/or the editor's
        cuts, zooms and caption text. Whatever isn't given stays as it was."""
        clip = self.store.get_clip(clip_id)
        job = self.store.get_job(clip.job_id) if clip else None
        if not clip or not job:
            return
        source = self.clean_source(clip)
        manual = clip.manual_sfx() if sfx is None else sfx
        template_id = template_id or clip.template
        meta = dict(clip.meta)
        if words is not None:
            meta.setdefault("words_original", clip.word_list())      # for "reset captions"
            self.store.update_clip(clip.id, words=json.dumps(words), meta=meta)
        tmp = Path(clip.path).with_name(f"{clip.job_id}_{clip.idx}.rendering.mp4")
        try:
            source_duration = await media.probe_duration(source)
            edit = editing.normalize(edit if edit is not None else (json.loads(clip.edit) if clip.edit else None),
                                     source_duration)
            self.store.update_clip(clip.id, edit=json.dumps(edit))
            clip = self.store.get_clip(clip.id)
            done = await self.render_clip_files(job, source, tmp, clip.word_list(), clip.split_list(), template_id,
                                                manual, dict(clip.meta), source_duration,
                                                seed=f"{clip.job_id}_{clip.idx}", edit=edit)
            tmp.replace(clip.path)
            preview = Path(clip.preview_path or self.settings.folder("previews") / f"{clip.job_id}_{clip.idx}.mp4")
            await media.make_preview(Path(clip.path), preview, self.settings.preview_max_mb,
                                     self.settings.preview_height)
            if not clip.preview_path:
                self.store.update_clip(clip.id, preview_path=str(preview))
            await media.make_thumbnail(Path(clip.path), self.settings.folder("thumbs") / f"{clip.job_id}_{clip.idx}.jpg")
        except (media.MediaError, OSError) as e:
            tmp.unlink(missing_ok=True)
            self.store.update_clip(clip.id, render_state="failed", render_error=str(e)[:1000])
            log.warning("clip %s: re-render failed: %s", clip.id, e, extra={"job_id": job.id})
            return
        meta = dict(clip.meta)
        if done.moments is not None:
            meta["moments"] = done.moments
            meta.pop("punchlines", None)
        meta.pop("credit", None)
        if done.template.get("credit"):
            meta["credit"] = done.template["credit"]
        error = done.error or ("; ".join(done.notes) if done.notes else None)
        self.store.update_clip(clip.id, template=done.template_id, sfx=json.dumps(manual) if manual else None,
                               meta=meta, render_state="failed" if done.error else None,
                               render_error=error[:1000] if error else None, duration=done.duration)
        log.info("clip %s: rendered with the %s template", clip.id, done.template["name"], extra={"job_id": job.id})
        self.resend_clip(job, self.store.get_clip(clip.id), done)

    async def recut(self, clip_id: str, extend_start: float = 0.0, extend_end: float = 0.0,
                    framing: Optional[str] = None) -> None:
        """Have OpenShorts cut the clip again from its source: longer at either end, and/or framed
        differently. Then the caption-free copy, words and layout are replaced, the editor's edit and
        hand-placed effects move along with the new start, and the clip renders again."""
        clip = self.store.get_clip(clip_id)
        job = self.store.get_job(clip.job_id) if clip else None
        if not clip or not job or not job.openshorts_job:
            return
        source = self.clean_source(clip)
        try:
            edl = await self.openshorts.edl(job.openshorts_job, clip.idx)
            segments = [{"start": float(x["start"]), "end": float(x["end"])} for x in edl.get("segments") or []]
            if not segments:
                raise OpenShortsError("OpenShorts has no recipe for this clip.")
            limit = (edl.get("source") or {}).get("duration") or float("inf")
            new_start = max(0.0, segments[0]["start"] - max(0.0, extend_start))
            moved_by = segments[0]["start"] - new_start
            segments[0]["start"] = round(new_start, 3)
            segments[-1]["end"] = round(min(limit, segments[-1]["end"] + max(0.0, extend_end)), 3)
            result = await self.openshorts.recut(job.openshorts_job, clip.idx, segments,
                                                 framing if framing in ("auto", "full", "track") else None)
            old_duration = await media.probe_duration(source)
            fresh = source.with_name(source.stem + ".recut.mp4")
            await self.openshorts.download(result["new_video_url"], fresh)
            new_duration = await media.probe_duration(fresh)
            fresh.replace(source)
            words = captions.words_from_openshorts(await self.openshorts.transcript(job.openshorts_job, clip.idx))
            splits: list[tuple[float, float]] = []
            doc = await self.openshorts.status(job.openshorts_job)
            clips = ((doc or {}).get("result") or {}).get("clips") or []
            if clip.idx < len(clips):
                splits = captions.split_ranges_from_layout({"ranges": clips[clip.idx].get("layout_ranges") or []})
        except (OpenShortsError, httpx.HTTPError, media.MediaError, OSError, KeyError, ValueError) as e:
            message = str(e) or type(e).__name__
            if isinstance(e, OpenShortsError) and e.status == 404:
                message = "OpenShorts no longer has this clip's files (it keeps them about a day)."
            self.store.update_clip(clip.id, render_state="failed", render_error=message[:1000])
            log.warning("clip %s: re-cut failed: %s", clip.id, message, extra={"job_id": job.id})
            return
        edit, sfx = editing.shift(json.loads(clip.edit) if clip.edit else None, clip.manual_sfx(), moved_by,
                                  old_duration, new_duration)
        meta = {k: v for k, v in clip.meta.items() if k not in ("moments", "punchlines", "words_original")}
        meta.update(start=result.get("start", meta.get("start")), end=result.get("end", meta.get("end")))
        self.store.update_clip(clip.id, words=json.dumps(words) if words else None,
                               splits=json.dumps(splits) if splits else None, meta=meta,
                               edit=json.dumps(edit), sfx=json.dumps(sfx) if sfx else None)
        log.info("clip %s: re-cut by OpenShorts (%s path), %.1f s longer at the start", clip.id,
                 result.get("render_path"), moved_by, extra={"job_id": job.id})
        await self.rerender(clip.id)

    def resend_clip(self, job: Job, clip: Clip, done: Rendered) -> None:
        """Send a re-rendered clip to Telegram again, with fresh ✅/❌ buttons. The earlier message's
        buttons keep working; they act on the same clip, which now is this render."""
        if not clip.preview_path or not Path(clip.preview_path).is_file():
            return
        total = len(self.store.clips_for_job(job.id))
        lines = ["🔁 Re-rendered"]
        if done.error:
            lines.append(f"⚠️ The {done.template['name']} template failed, so this one has no captions.")
        elif done.notes:
            lines += [f"⚠️ {note}" for note in done.notes]
        caption = "\n".join(lines) + "\n" + clip_caption(job, clip, total, done.template["name"])
        self.store.enqueue_event({
            "event": "clip_ready", "chat_id": job.chat_id, "job_id": job.id, "clip_id": clip.id,
            "caption": caption if len(caption) <= CAPTION_LIMIT else caption[:CAPTION_LIMIT - 1] + "…",
            "preview_url": f"{self.settings.self_url}/previews/{clip.id}",
            "approve_data": f"ok:{clip.id}", "skip_data": f"no:{clip.id}",
        })

    def auto_retry(self, job: Job, delay: Optional[int]) -> Optional[Job]:
        """Queue one delayed retry for a passing failure. A job that is itself a retry (automatic
        or from the dashboard) isn't retried again, so a lasting problem stops after one go."""
        if not delay or not self.settings.auto_retry or job.retry_of:
            return None
        retry = self.store.create_job(job.chat_id, job.source_url, job.range_start, job.range_end,
                                      job.target_clips, retry_of=job.id)
        self.store.update_job(retry.id, created=time.time() + delay)
        log.info("job %s: retrying in %ds as job %s", job.id, delay, retry.id, extra={"job_id": job.id})
        return retry

    def fail(self, job: Job, error: str, detail: Optional[str] = None) -> None:
        log.warning("job %s failed: %s", job.id, error, extra={"job_id": job.id, "detail": detail})
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
        self.clean_source(clip).unlink(missing_ok=True)      # no more re-renders after a decision
        self.store.decide_clip(clip.id, status, str(dest))
        reply = reply.format(name=dest.name, id=clip.id)
        if status == "approved" and self.publisher:
            line = self.publisher.on_approved(self.store.get_clip(clip.id), job.chat_id)
            if line:
                reply += "\n" + line
        storage.offer_cleanup(self.settings, self.store, job)
        return status.capitalize(), reply

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
