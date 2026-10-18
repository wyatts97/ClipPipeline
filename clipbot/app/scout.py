"""/scan and /watch: find the best moments of a stream and offer them as buttons.

A scan reads whatever the platform offers (chat replay, audio loudness, viewer clips,
YouTube's "most replayed"), scores the stream (scoring.py) and sends each top moment to
Telegram with 🎬 buttons. A tap turns the moment into a normal range job.

The watcher polls watched channels every WATCH_INTERVAL_MINUTES and scans a VOD once
it stops growing (= the stream ended).
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Optional

import httpx

from . import chat, clips_api, signals
from .config import Settings
from .media import MediaError
from .parsing import format_timestamp as ts
from .platforms import Channel, LatestVod, Vod, detect_channel, detect_vod, latest_vod, timestamp_link
from .scoring import Moment, StreamSignals, find_moments
from .store import Scan, Store, Watch

log = logging.getLogger("clipbot.scout")


class ScanError(RuntimeError):
    pass


def _views(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def moment_text(rank: int, vod: Vod, m: Moment) -> str:
    details = []
    if m.chat_ratio is not None:
        details.append(f"chat {m.chat_ratio:g}× normal")
    if m.loud:
        details.append("🔊 loud")
    if m.clips:
        details.append(f"✂️ {m.clips} viewer clip{'s' if m.clips > 1 else ''} ({_views(m.clip_views)} views)")
    if m.replayed:
        details.append("📈 most replayed")
    lines = [f"#{rank} 🔥 {ts(m.peak)}  (window {ts(m.start)}–{ts(m.end)})"]
    if details:
        lines.append(" · ".join(details))
    if m.top_tokens:
        lines.append("💬 " + " · ".join(f"{tok} ×{n}" for tok, n in m.top_tokens))
    lines.append(f"▶️ {timestamp_link(vod, m.start)}")
    return "\n".join(lines)


class Scout:
    def __init__(self, settings: Settings, store: Store, pipeline,
                 http: Optional[httpx.AsyncClient] = None):
        self.settings = settings
        self.store = store
        self.pipeline = pipeline          # for wake() after queueing a clip job
        self.http = http or httpx.AsyncClient(timeout=30, follow_redirects=True)
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        for scan in self.store.scans_with_status("running"):
            self.store.update_scan(scan.id, status="queued")
        self._tasks = [asyncio.create_task(self._loop(self._scan_next, "scanner")),
                       asyncio.create_task(self._loop(self._watch_round, "watcher"))]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.http.aclose()

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

    def _event(self, chat_id: int, text: str) -> None:
        self.store.enqueue_event({"event": "message", "chat_id": chat_id, "text": text})

    # ---- scanning ------------------------------------------------------------
    def queue_scan(self, chat_id: int, url: str) -> Scan:
        scan = self.store.create_scan(chat_id, url)
        self.wake()
        return scan

    async def _scan_next(self) -> None:
        queued = self.store.scans_with_status("queued")
        if not queued:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=10)
            except asyncio.TimeoutError:
                pass
            return
        await self.run_scan(queued[0])

    async def run_scan(self, scan: Scan) -> None:
        s = self.settings
        vod = detect_vod(scan.source_url)
        work = s.folder("tmp") / f"scan-{scan.id}"
        self.store.update_scan(scan.id, status="running")
        try:
            info = await signals.probe(scan.source_url, s.cookies_file)
            if info.get("live_status") in ("is_live", "post_live"):
                raise ScanError("this stream hasn't finished yet. Scan it after it ends, or /watch the channel.")
            duration = float(info.get("duration") or 0)
            if not duration and info.get("url"):
                duration = await signals.media_duration(info["url"])
            if duration <= 0:
                raise ScanError("couldn't tell how long the video is.")
            title = info.get("title") or scan.source_url
            self.store.update_scan(scan.id, title=title, duration=duration)

            sig, notes = await self.gather(vod, info, duration, work)
            if not (sig.messages or sig.loudness_db or sig.markers or sig.heatmap):
                raise ScanError("no signal could be read (" + "; ".join(notes) + ").")
            moments = find_moments(sig, top=s.scan_top, before=s.scan_before_seconds,
                                   after=s.scan_after_seconds)
        except (ScanError, MediaError, httpx.HTTPError, KeyError, ValueError) as e:
            log.warning("scan %s failed: %s", scan.id, e)
            self.store.update_scan(scan.id, status="failed", error=str(e)[:2000])
            self._event(scan.chat_id, f"❌ Scan {scan.id} failed: {str(e)[:3000]}")
            return
        finally:
            shutil.rmtree(work, ignore_errors=True)

        self.store.update_scan(scan.id, status="done", signals=json.dumps(notes))
        head = f"🔎 Scan done: {title}\n{ts(duration)} · " + " · ".join(notes)
        if not moments:
            self._event(scan.chat_id, head + "\nNothing stood out above this stream's normal level.")
            return
        self._event(scan.chat_id, head + f"\nTop {len(moments)} moment(s) below. 🎬 clips one; "
                                         "▶️ opens the VOD there so you can check first.")
        for rank, m in enumerate(moments, 1):
            row = self.store.add_moment(scan.id, rank, m.peak, m.start, m.end, m.score)
            self.store.enqueue_event({
                "event": "choice", "chat_id": scan.chat_id, "text": moment_text(rank, vod, m),
                "buttons": [{"text": "🎬 Clip it", "data": f"sc:{row.id}:1"},
                            {"text": "➕ Wider ×2", "data": f"sc:{row.id}:2"}],
            })
        log.info("scan %s: %d moment(s) from %s", scan.id, len(moments), ", ".join(notes))

    async def gather(self, vod: Vod, info: dict, duration: float, work) -> tuple[StreamSignals, list[str]]:
        """Collect every signal this platform offers; a failing one is noted, not fatal."""
        s = self.settings
        tasks: dict[str, asyncio.Future] = {}
        if vod.platform == "twitch":
            tasks["chat"] = asyncio.ensure_future(
                chat.fetch_twitch_chat(vod.vod_id, work / "chat", s.twitch_downloader))
            if s.twitch_client_id and s.twitch_client_secret:
                tasks["clips"] = asyncio.ensure_future(clips_api.twitch_clip_markers(
                    self.http, vod.vod_id, s.twitch_client_id, s.twitch_client_secret))
        elif vod.platform == "youtube" and "live_chat" in (info.get("subtitles") or {}):
            tasks["chat"] = asyncio.ensure_future(
                chat.fetch_youtube_chat(vod.url, work / "chat", s.cookies_file))
        elif vod.platform == "kick":
            tasks["clips"] = asyncio.ensure_future(self._kick_markers(vod.vod_id))
        if s.scan_audio and info.get("url"):
            tasks["audio"] = asyncio.ensure_future(self._audio(vod, info, duration, work))

        results = dict(zip(tasks, await asyncio.gather(*tasks.values(), return_exceptions=True)))
        sig = StreamSignals(duration=duration, heatmap=info.get("heatmap") or None,
                            extra_hype=s.scan_hype_words)
        notes = []
        for name, result in results.items():
            if isinstance(result, BaseException):
                log.warning("scan signal %s failed: %s", name, result)
                notes.append(f"{name} unavailable ({str(result)[:120]})")
            elif name == "chat":
                sig.messages = result
                notes.append(f"chat {len(result):,} msgs")
            elif name == "audio":
                sig.loudness_db = result
                notes.append("audio")
            elif name == "clips":
                sig.markers = result
                notes.append(f"{len(result)} viewer clips")
        if sig.heatmap:
            notes.append("most replayed")
        if vod.platform == "kick":
            notes.append("no chat replay on Kick")
        return sig, notes

    async def _audio(self, vod: Vod, info: dict, duration: float, work) -> list[float]:
        # HLS (Twitch, Kick) streams fine through ffmpeg; anything else is downloaded first.
        if str(info.get("protocol") or "").startswith("m3u8"):
            return await signals.loudness(info["url"], info.get("http_headers"), duration)
        path = await signals.download_audio(vod.url, work / "audio", self.settings.cookies_file)
        try:
            return await signals.loudness(str(path), None, duration)
        finally:
            path.unlink(missing_ok=True)

    async def _kick_markers(self, uuid: str):
        meta = await clips_api.kick_vod(self.http, uuid)
        if not meta.get("slug"):
            return []
        return await clips_api.kick_clip_markers(self.http, meta["slug"], meta["livestream_id"], meta["start"])

    def choose(self, chat_id: int, data: str) -> tuple[str, Optional[str]]:
        """A 🎬 tap: sc:<moment id>:<1 = as found | 2 = wider window, two clips>."""
        _, _, rest = data.partition(":")
        moment_id, _, mode = rest.partition(":")
        moment = self.store.get_moment(moment_id)
        scan = self.store.get_scan(moment.scan_id) if moment else None
        if not moment or not scan or scan.chat_id != chat_id:
            return "Unknown moment", None
        if moment.job_id:
            return "Already queued", None
        start, end, clips = moment.start, moment.end, self.settings.scan_clips_per_moment
        if mode == "2":
            start, end, clips = max(0.0, start - 60), min(scan.duration or end + 60, end + 60), 2
        job = self.store.create_job(chat_id, scan.source_url, start, end, clips)
        self.store.claim_moment(moment.id, job.id)
        self.pipeline.wake()
        return "Queued", (f"👍 Job {job.id} queued: {clips} clip(s) from {ts(start)}–{ts(end)} "
                          f"({ts(end - start)}) of moment #{moment.rank}.")

    # ---- watching -------------------------------------------------------------
    async def add_watch(self, chat_id: int, text: str) -> str:
        channel = detect_channel(text)
        if not channel:
            return ("⚠️ I need a channel link: https://twitch.tv/<name>, https://kick.com/<name> "
                    "or https://youtube.com/@<handle>")
        try:
            latest = await latest_vod(channel, self.http)
        except (MediaError, httpx.HTTPError, ValueError) as e:
            return f"⚠️ Couldn't read {channel.label}'s videos: {str(e)[:300]}"
        watch = self.store.add_watch(chat_id, channel.platform, channel.name,
                                     latest.vod_id if latest else None)
        if not watch:
            return f"Already watching {channel.label}."
        if latest:
            # Remember its length: if it grows by the next check, it was live and gets scanned too.
            self.store.update_watch(watch.id, pending_vod_id=latest.vod_id, pending_duration=latest.duration)
        msg = (f"👀 Watching {channel.label}. When a stream ends I'll scan the VOD and send you its top "
               f"moments (checked every {self.settings.watch_interval_minutes} min).")
        if latest and latest.live:
            msg += "\nIt's live right now; that stream gets scanned when it ends."
        elif latest:
            msg += f"\nLatest VOD: {latest.url}\nSend /scan with it to scan that one now."
        return msg

    def remove_watch(self, chat_id: int, text: str) -> str:
        channel = detect_channel(text)
        if not channel:
            return "⚠️ Which channel? e.g. /unwatch https://twitch.tv/<name>"
        if self.store.remove_watch(chat_id, channel.platform, channel.name):
            return f"Stopped watching {channel.label}."
        return f"I wasn't watching {channel.label}. /watching lists what I watch."

    def list_watches(self, chat_id: int) -> str:
        watches = self.store.watches(chat_id)
        if not watches:
            return "Not watching any channel. Add one with /watch https://twitch.tv/<name>"
        return "Watching:\n" + "\n".join(f"• {w.platform}/{w.channel}" for w in watches)

    async def _watch_round(self) -> None:
        for watch in self.store.watches():
            channel = Channel(watch.platform, watch.channel)
            try:
                latest = await latest_vod(channel, self.http)
            except (MediaError, httpx.HTTPError, ValueError) as e:
                log.warning("watch %s: listing failed: %s", channel.label, e)
                continue
            self.step_watch(watch, channel, latest)
        await asyncio.sleep(self.settings.watch_interval_minutes * 60)

    def step_watch(self, w: Watch, channel: Channel, latest: Optional[LatestVod]) -> Optional[Scan]:
        """Advance one watch by one observation; returns the scan it started, if any."""
        if latest is None:
            return None
        grew = (latest.duration is not None and w.pending_duration is not None
                and latest.duration > w.pending_duration + 30)
        if latest.vod_id == w.last_vod_id:
            if w.pending_vod_id == latest.vod_id:  # the VOD that was newest when /watch was sent
                if latest.live or grew:            # ...was still live: treat it as a new one
                    self.store.update_watch(w.id, last_vod_id=None, pending_duration=latest.duration)
                else:
                    self.store.update_watch(w.id, pending_vod_id=None, pending_duration=None)
            return None
        if (latest.live or latest.duration is None or w.pending_vod_id != latest.vod_id
                or w.pending_duration is None or grew):
            self.store.update_watch(w.id, pending_vod_id=latest.vod_id, pending_duration=latest.duration)
            return None
        # Same VOD, same length as last check: the stream is over.
        self.store.update_watch(w.id, last_vod_id=latest.vod_id, pending_vod_id=None, pending_duration=None)
        scan = self.queue_scan(w.chat_id, latest.url)
        self._event(w.chat_id, f"📺 {channel.label} finished a {ts(latest.duration or 0)} stream. "
                               f"Scanning it (scan {scan.id})…\n{latest.url}")
        return scan
