"""Publishing approved clips through Postiz.

✅ Post (Telegram or the dashboard) queues the clip. The publisher uploads it to Postiz and
schedules one post per connected YouTube / TikTok / Instagram channel at the next free daily slot
(PUBLISH_SLOTS, in TZ). It then watches Postiz and reports back to Telegram when a post goes live
or fails. Postiz does the actual posting; its web app stays usable for anything by hand.

Two background tasks:
  queue  - schedules waiting clips, retrying a few times while Postiz is down
  sync   - reads the state of scheduled posts back from Postiz every SYNC_SECONDS
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings
from .postiz import PLATFORM_NAMES, Postiz, PostizError, as_html, iso, parse_iso
from .store import Clip, Post, PublishRequest, Store

log = logging.getLogger("clipbot.publish")

SYNC_SECONDS = 120
LEAD_SECONDS = 10 * 60           # a slot closer than this is skipped
RETRY_DELAYS = (60, 300, 900)    # then the request is marked failed
DEFAULT_SLOTS = ((12, 0), (17, 0), (21, 0))
# OpenShorts' prompt is written for podcasts and sometimes adds "comment X and I'll send you ..."
_CTA = re.compile(r"\b(?:comment|dm)\b[^.!?\n]*\b(?:send|get)\s+(?:you|it)\b[^.!?\n]*[.!?]?\s*",
                  re.IGNORECASE)


# ---- pure helpers ------------------------------------------------------------------
def zone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def parse_slots(text: str) -> list[tuple[int, int]]:
    slots = set()
    for part in re.split(r"[,\s]+", text or ""):
        m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", part.strip())
        if m and int(m.group(1)) < 24 and int(m.group(2) or 0) < 60:
            slots.add((int(m.group(1)), int(m.group(2) or 0)))
    return sorted(slots) or list(DEFAULT_SLOTS)


def next_free_slot(slots: list[tuple[int, int]], tz: tzinfo, now: float, taken: set[int],
                   lead: int = LEAD_SECONDS, days: int = 90) -> float:
    """The first daily slot at least `lead` seconds away whose minute isn't in `taken`."""
    today = datetime.fromtimestamp(now, tz).date()
    for offset in range(days):
        day = today + timedelta(days=offset)
        for hour, minute in slots:
            ts = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz).timestamp()
            if ts >= now + lead and int(ts // 60) not in taken:
                return ts
    return now + lead


def when_label(ts: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%a %d %b, %H:%M")


def clean_caption(text: Optional[str]) -> str:
    return re.sub(r"[ \t]+\n", "\n", _CTA.sub("", text or "")).strip()


def youtube_title(clip: Clip) -> str:
    title = clean_caption(clip.meta.get("video_title_for_youtube_short") or clip.title or clip.hook) or "Clip"
    return (title if len(title) >= 2 else f"{title} clip")[:100]


def caption_for(platform: str, clip: Clip) -> str:
    """The post text. A template's credit line (e.g. for CC-BY music) always goes at the end."""
    meta = clip.meta or {}
    title = clean_caption(clip.title or meta.get("video_title_for_youtube_short") or "")
    if platform == "youtube":
        text = "\n".join(x for x in (clean_caption(clip.hook), "#shorts") if x)
    elif platform == "tiktok":
        text = clean_caption(meta.get("video_description_for_tiktok")) or title
    else:
        text = clean_caption(meta.get("video_description_for_instagram")) or title
    credit = str(meta.get("credit") or "").strip()
    return f"{text}\n\n{credit}".strip() if credit else text


def channel_settings(settings: Settings, channel: dict[str, Any], clip: Clip) -> dict[str, Any]:
    platform, identifier = channel["platform"], channel["identifier"]
    if platform == "youtube":
        visibility = settings.youtube_visibility if settings.youtube_visibility in ("public", "private", "unlisted") else "public"
        return {"__type": "youtube", "title": youtube_title(clip), "type": visibility,
                "selfDeclaredMadeForKids": "no", "tags": []}
    if platform == "tiktok":
        mode = settings.tiktok_post_mode if settings.tiktok_post_mode in ("UPLOAD", "DIRECT_POST") else "UPLOAD"
        return {"__type": identifier, "title": youtube_title(clip)[:90], "privacy_level": "PUBLIC_TO_EVERYONE",
                "duet": False, "stitch": False, "comment": True, "autoAddMusic": "no",
                "brand_content_toggle": False, "brand_organic_toggle": False, "video_made_with_ai": False,
                "content_posting_method": mode}
    return {"__type": identifier, "post_type": "post", "collaborators": []}   # a single video = a Reel


def build_post(settings: Settings, clip: Clip, channels: list[dict[str, Any]], media: dict[str, Any],
               when: Optional[float]) -> dict[str, Any]:
    """Postiz create-post body: one post per channel, all at `when` (None = right away)."""
    return {
        "type": "now" if when is None else "schedule",
        "date": iso(when if when is not None else time.time()),
        "shortLink": False,
        "tags": [],
        "posts": [{"integration": {"id": ch["id"]},
                   "value": [{"content": as_html(caption_for(ch["platform"], clip)),
                              "image": [{"id": media["id"], "path": media["path"]}]}],
                   "settings": channel_settings(settings, ch, clip)} for ch in channels],
    }


class Publisher:
    def __init__(self, settings: Settings, store: Store, postiz: Optional[Postiz] = None):
        self.settings = settings
        self.store = store
        self.enabled = bool(settings.postiz_api_key)
        self.postiz = postiz or Postiz(settings.postiz_url, settings.postiz_api_key)
        self.tz = zone(settings.timezone)
        self.slots = parse_slots(settings.publish_slots)
        self.wanted = {p.strip().lower() for p in settings.publish_to.split(",") if p.strip()}
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ---- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            log.info("publishing is off (no POSTIZ_API_KEY)")
            return
        self._tasks = [asyncio.create_task(self._loop(self._queue_round, "publish queue")),
                       asyncio.create_task(self._loop(self._sync_round, "publish sync"))]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.postiz.aclose()

    def wake(self) -> None:
        self._wake.set()

    async def _loop(self, step, name: str) -> None:
        while True:
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s loop error", name)
                await asyncio.sleep(30)

    async def _queue_round(self) -> None:
        await self.process_queue()
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), 10)
        except asyncio.TimeoutError:
            pass

    async def _sync_round(self) -> None:
        await self.sync()
        await asyncio.sleep(SYNC_SECONDS)

    def slot_label(self) -> str:
        return " / ".join(f"{h:02d}:{m:02d}" for h, m in self.slots)

    # ---- queueing ---------------------------------------------------------
    def on_approved(self, clip: Clip, chat_id: int) -> Optional[str]:
        """Called by ✅ Post. Returns a line for the approval reply."""
        if not self.enabled:
            return None
        self.store.queue_publish(clip.id, chat_id)
        self.wake()
        return f"🗓 Scheduling it for the next free slot ({self.slot_label()})…"

    def queue(self, clip: Clip, chat_id: int, publish_at: Optional[float] = None, now: bool = False) -> None:
        if clip.status != "approved":
            raise ValueError("Only approved clips can be published.")
        if any(p.status == "scheduled" for p in self.store.posts_for_clip(clip.id)):
            raise ValueError("This clip is already scheduled. Move or cancel it instead.")
        self.store.queue_publish(clip.id, chat_id, publish_at, now)
        self.wake()

    async def process_queue(self) -> None:
        for req in self.store.due_publish_requests():
            await self.schedule(req)

    async def taken_minutes(self, channels: list[dict[str, Any]]) -> set[int]:
        now = time.time()
        taken = {int(p.publish_at // 60) for p in self.store.list_posts("scheduled", since=now)}
        ids = {c["id"] for c in channels}
        for doc in await self.postiz.posts(now - 3600, now + 90 * 86400):
            ts = parse_iso(doc.get("publishDate"))
            if ts and (doc.get("integration") or {}).get("id") in ids and doc.get("state") != "ERROR":
                taken.add(int(ts // 60))
        return taken

    async def schedule(self, req: PublishRequest) -> None:
        clip = self.store.get_clip(req.clip_id)
        if not clip or clip.status != "approved" or not Path(clip.path).is_file():
            self._give_up(req, "The approved clip's file is gone.")
            return
        try:
            channels = [c for c in await self.postiz.channels()
                        if not c["disabled"] and c["platform"] in self.wanted]
            if not channels:
                self._give_up(req, "No YouTube, TikTok or Instagram channel is connected in Postiz yet. "
                                   "Add them under Postiz → Launches, then retry here.")
                return
            when = None if req.now else (req.publish_at or next_free_slot(
                self.slots, self.tz, time.time(), await self.taken_minutes(channels)))
            media = self._earlier_media(clip.id) or await self.postiz.upload(Path(clip.path))
            created = await self.postiz.create_post(build_post(self.settings, clip, channels, media, when))
        except PostizError as e:
            if e.lasting:
                self._give_up(req, str(e))
            else:
                self._retry_later(req, str(e))
            return
        by_id = {c["id"]: c for c in channels}
        for item in created:
            ch = by_id.get(item.get("integration")) or {}
            self.store.add_post(item["postId"], clip.id, item.get("integration", ""), ch.get("platform", "?"),
                                ch.get("name"), when or time.time(), media["id"], media["path"])
        self.store.update_publish(clip.id, status="scheduled", error=None)
        names = ", ".join(self._channel_label(c) for c in channels)
        log.info("clip %s: scheduled for %s on %s", clip.id, iso(when) if when else "now", names)
        text = (f"⚡ Posting clip {clip.id} now: {names}." if when is None else
                f"🗓 Clip {clip.id} ({clip.title or 'untitled'}) is scheduled for {when_label(when, self.tz)}: {names}.")
        self.store.enqueue_event({"event": "choice", "chat_id": req.chat_id, "text": text,
                                  "buttons": [{"text": "⚡ Post now", "data": f"pn:{clip.id}"},
                                              {"text": "✖️ Unschedule", "data": f"pu:{clip.id}"}]}
                                 if when is not None else {"event": "message", "chat_id": req.chat_id, "text": text})

    def _channel_label(self, channel: dict[str, Any]) -> str:
        name = PLATFORM_NAMES.get(channel["platform"], channel["platform"])
        if channel["platform"] == "tiktok" and self.settings.tiktok_post_mode != "DIRECT_POST":
            name += " (to your inbox)"
        return name

    def _earlier_media(self, clip_id: str) -> Optional[dict[str, Any]]:
        for post in self.store.posts_for_clip(clip_id):
            if post.media_id and post.media_path:
                return {"id": post.media_id, "path": post.media_path}
        return None

    def _retry_later(self, req: PublishRequest, error: str) -> None:
        attempts = req.attempts + 1
        if attempts > len(RETRY_DELAYS):
            self._give_up(req, error)
            return
        log.warning("clip %s: couldn't schedule (%s), retrying in %ds", req.clip_id, error,
                    RETRY_DELAYS[attempts - 1])
        self.store.update_publish(req.clip_id, attempts=attempts, error=error,
                                  next_try=time.time() + RETRY_DELAYS[attempts - 1])

    def _give_up(self, req: PublishRequest, error: str) -> None:
        log.warning("clip %s: not scheduled: %s", req.clip_id, error)
        self.store.update_publish(req.clip_id, status="failed", error=error)
        self.store.enqueue_event({"event": "message", "chat_id": req.chat_id,
                                  "text": f"⚠️ Clip {req.clip_id} wasn't scheduled: {error}\n"
                                          "Retry it from the dashboard's Publishing page."})

    # ---- changing scheduled posts -------------------------------------------
    async def unschedule(self, clip_id: str) -> int:
        """Delete the clip's scheduled posts in Postiz. Returns how many were cancelled."""
        scheduled = [p for p in self.store.posts_for_clip(clip_id) if p.status == "scheduled"]
        if scheduled:
            await self.postiz.delete_post(scheduled[0].id)   # deletes the whole group
        for post in scheduled:
            self.store.update_post(post.id, status="cancelled")
        req = self.store.get_publish(clip_id)
        if req and req.status in ("waiting", "failed"):
            self.store.update_publish(clip_id, status="cancelled")
        return len(scheduled)

    async def reschedule(self, clip_id: str, chat_id: int, publish_at: Optional[float], now: bool = False) -> None:
        await self.unschedule(clip_id)
        self.store.queue_publish(clip_id, chat_id, publish_at, now)
        self.wake()

    async def button(self, chat_id: int, data: str) -> tuple[str, Optional[str]]:
        """pn:<clip> posts now, pu:<clip> unschedules."""
        action, _, clip_id = data.partition(":")
        clip = self.store.get_clip(clip_id)
        req = self.store.get_publish(clip_id)
        if not clip or not req or req.chat_id != chat_id:
            return "Unknown clip", None
        if not any(p.status == "scheduled" for p in self.store.posts_for_clip(clip_id)):
            return "Not scheduled", "That clip has no scheduled posts any more."
        try:
            if action == "pn":
                await self.reschedule(clip_id, chat_id, None, now=True)
                return "Posting now", None
            n = await self.unschedule(clip_id)
            return "Unscheduled", f"✖️ Cancelled {n} scheduled post(s) of clip {clip_id}."
        except PostizError as e:
            return "Postiz error", f"⚠️ {e}"

    # ---- reading results back ---------------------------------------------
    async def sync(self) -> None:
        scheduled = self.store.list_posts("scheduled")
        if not scheduled:
            return
        now = time.time()
        start = min(p.publish_at for p in scheduled) - 86400
        try:
            docs = await self.postiz.posts(start, max(now, max(p.publish_at for p in scheduled)) + 90 * 86400)
        except PostizError as e:
            log.warning("can't read post states from Postiz: %s", e)
            return
        by_id = {d.get("id"): d for d in docs}
        for post in scheduled:
            doc = by_id.get(post.id)
            if doc is None:
                if post.publish_at < now - 300 or post.publish_at > now + 600:
                    self.store.update_post(post.id, status="cancelled", error="Deleted in Postiz")
                continue
            state = doc.get("state")
            moved = parse_iso(doc.get("publishDate"))
            if state == "PUBLISHED":
                self.store.update_post(post.id, status="published", url=doc.get("releaseURL"))
                self._notify_published(post, doc.get("releaseURL"))
            elif state == "ERROR":
                self.store.update_post(post.id, status="failed", error="Postiz couldn't post it")
                self._notify_failed(post)
            elif moved and abs(moved - post.publish_at) > 30:
                self.store.update_post(post.id, publish_at=moved)   # moved in Postiz's calendar

    def _chat_of(self, post: Post) -> int:
        req = self.store.get_publish(post.clip_id)
        return req.chat_id if req else 0

    def _notify_published(self, post: Post, url: Optional[str]) -> None:
        name = PLATFORM_NAMES.get(post.platform, post.platform)
        if post.platform == "tiktok" and self.settings.tiktok_post_mode != "DIRECT_POST":
            text = (f"📥 Clip {post.clip_id} is in your TikTok inbox. Open TikTok, add the campaign's sound "
                    "if it needs one, and post it within 24 h.")
        else:
            text = f"📤 Clip {post.clip_id} is live on {name}: {url or '(no link from Postiz)'}"
            text += "\nSubmit the link to the campaign on Content Rewards."
        log.info("clip %s: published on %s %s", post.clip_id, name, url or "")
        self.store.enqueue_event({"event": "message", "chat_id": self._chat_of(post), "text": text})

    def _notify_failed(self, post: Post) -> None:
        name = PLATFORM_NAMES.get(post.platform, post.platform)
        link = f" ({self.settings.postiz_public_url})" if self.settings.postiz_public_url else ""
        log.warning("clip %s: posting to %s failed in Postiz", post.clip_id, name)
        self.store.enqueue_event({"event": "message", "chat_id": self._chat_of(post),
                                  "text": f"⚠️ Posting clip {post.clip_id} to {name} failed. Postiz shows why "
                                          f"under the post in its calendar{link}."})
