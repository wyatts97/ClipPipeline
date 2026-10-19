"""Run the dashboard against seeded demo data, for design work and screenshots.

    python dev/demo/serve.py            # http://127.0.0.1:8090, password "demo"
    python dev/demo/serve.py --reseed   # throw the demo data away and start over

Real clipbot API and dashboard build (clipbot/web/dist); OpenShorts is mocked and the
background workers are off, so nothing changes unless you click something.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "clipbot"))

from app import media, storage  # noqa: E402
from app.chat import ChatMessage  # noqa: E402
from app.clips_api import ClipMarker  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.openshorts import OpenShorts  # noqa: E402
from app.postiz import Postiz  # noqa: E402
from app.publisher import Publisher  # noqa: E402
from app.scoring import StreamSignals, find_moments, score, timeline  # noqa: E402
from app.store import Store  # noqa: E402

DATA = Path(__file__).with_name(".data")
PORT = 8090
SOURCES = ["testsrc2", "mandelbrot", "smptehdbars", "life=s=540x960:mold=10:r=30:ratio=0.1", "cellauto=s=540x960:rule=110"]
CHAT = 111
GB = 1024 ** 3
CHANNELS = [
    {"id": "yt-demo", "name": "Clip Lab", "identifier": "youtube", "disabled": False, "picture": None},
    {"id": "tt-demo", "name": "cliplab.tt", "identifier": "tiktok", "disabled": False, "picture": None},
    {"id": "ig-demo", "name": "cliplab", "identifier": "instagram-standalone", "disabled": False, "picture": None},
]


def fake_file(path: Path, size: int) -> None:
    """A tiny stand-in for a big file: it holds the size it pretends to have (see demo_size)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_name(path.name + ".fake").write_text(str(size))


def demo_size(path: Path) -> int:
    """storage.size_of for the demo: .fake files count as the size written in them."""
    files = [path] if path.is_file() else list(path.rglob("*")) if path.is_dir() else []
    return sum(int(f.read_text()) if f.suffix == ".fake" else f.stat().st_size for f in files if f.is_file())


def make_video(path: Path, source: str, seconds: int = 6) -> None:
    src = source if "=" in source else f"{source}=size=540x960:rate=30"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", src,
                    "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-t", str(seconds),
                    "-vf", "scale=540:960,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                    "-c:a", "aac", str(path)], check=True)


def synthetic_stream(duration: int) -> StreamSignals:
    rng = random.Random(7)
    words = ["what", "true", "ok", "this game", "real", "he is cooking", "nice"]
    msgs = [ChatMessage(t + rng.random() * 5, f"u{rng.randrange(600)}", rng.choice(words))
            for t in range(0, duration, 5) for _ in range(rng.randint(1, 4))]
    for at, n, word in ((2280, 90, "KEKW"), (4410, 60, "OMEGALUL"), (6720, 110, "Clap"), (8105, 45, "???"),
                        (9430, 70, "LUL")):
        msgs += [ChatMessage(at + i * 18 / n, f"fan{at}-{i}", word) for i in range(n)]
    loud = [-32 + rng.random() * 4 for _ in range(duration)]
    for at in (2270, 6715, 9425):
        for s in range(at, at + 9):
            loud[s] = -9 + rng.random()
    markers = [ClipMarker(2262, 30, 4100), ClipMarker(2275, 25, 380), ClipMarker(6700, 30, 1250), ClipMarker(9410, 28, 90)]
    return StreamSignals(duration, messages=msgs, loudness_db=loud, markers=markers)


async def seed(settings: Settings) -> None:
    store = Store(settings.db_path)
    now = time.time()
    pending, thumbs = settings.folder("pending"), settings.folder("thumbs")
    approved = settings.folder("approved")

    def job(url, start, end, clips, status, age, error=None, os_job=None):
        j = store.create_job(CHAT, url, start, end, clips)
        store._db.execute("UPDATE jobs SET status=?, error=?, openshorts_job=?, created=?, updated=? WHERE id=?",
                          (status, error, os_job, now - age, now - age + 240, j.id))
        return store.get_job(j.id)

    vod = "https://www.twitch.tv/videos/2901337436"
    done = job(vod, 2190, 2325, 3, "done", 3600, os_job="5a1c0d4e-0000-4000-8000-00000000d0e1")
    titles = [("He really said that on stream", "He said WHAT 😳"), ("Chat lost it after this play", "Wait for it…"),
              ("The cleanest clutch you'll see today", "1v4. No way 🔥"), ("Boss fight goes sideways", "This is fine 🙂"),
              ("Streamer vs. chat, round two", "Chat was right 💀")]
    for i, (title, hook) in enumerate(titles):
        master = pending / f"{done.id}_{i}.mp4"
        make_video(master, SOURCES[i % len(SOURCES)], 5 + i)
        await media.make_thumbnail(master, thumbs / f"{done.id}_{i}.jpg")
        store.add_clip(done.id, i, title, hook, {
            "video_title_for_youtube_short": title, "viral_hook_text": hook,
            "video_description_for_tiktok": f"{title} #twitch #streamer #clips",
            "video_description_for_instagram": f"{title}. Follow for daily clips."}, float(5 + i), str(master), None)
    old = job("https://youtu.be/zn1dXa6W7t4", 600, 735, 2, "done", 86400 * 2,
              os_job="5a1c0d4e-0000-4000-8000-0000000001d0")
    for i, decision in enumerate(("ok", "no")):
        master = pending / f"{old.id}_{i}.mp4"
        make_video(master, SOURCES[(i + 3) % len(SOURCES)], 6)
        await media.make_thumbnail(master, thumbs / f"{old.id}_{i}.jpg")
        clip = store.add_clip(old.id, i, ["Award nomination reaction", "The WiFi gave up"][i], None, {}, 6.0, str(master), None)
        if decision == "ok":
            dest = approved / f"2026-10-16_award-nomination-reaction_{clip.id}.mp4"
            shutil.move(master, dest)
            store.decide_clip(clip.id, "approved", str(dest))
        else:
            dest = settings.folder("rejected") / f"{clip.id}.mp4"
            shutil.move(master, dest)
            store.decide_clip(clip.id, "rejected", str(dest))

    running_job = job(vod, 6600, 6735, 1, "processing", 420, os_job="5a1c0d4e-0000-4000-8000-00000000c0de")
    job("https://kick.com/xqc/videos/4bf8def5-b4cd-4084-b875-c47f781215e1", 22000, 22135, 1, "queued", 60)
    failed = job("https://www.youtube.com/watch?v=abcdefghijk", None, None, 3, "failed", 5400,
                 error="OpenShorts refused the job (400): This URL can't be processed: Sign in to confirm you're not a bot",
                 os_job="5a1c0d4e-0000-4000-8000-0000000fa11d")

    # What OpenShorts keeps on disk for these jobs (Storage page).
    for j, source, work in ((done, 1.6, 0.4), (old, 0.9, 0.25), (running_job, 1.2, 0.1), (failed, 3.4, 0.05)):
        fake_file(settings.openshorts_uploads_dir / f"{j.openshorts_job}_source.mp4", int(source * GB))
        fake_file(settings.openshorts_output_dir / j.openshorts_job / "clip_0.mp4", int(work * GB))

    # Publishing: two fresh clips approved and scheduled, the older one posted (Instagram failed),
    # and one clip whose scheduling failed.
    fresh = store.clips_for_job(done.id)
    for k, clip in enumerate(fresh[:3]):
        dest = approved / f"2026-10-18_{clip.id}.mp4"
        shutil.move(clip.path, dest)
        store.decide_clip(clip.id, "approved", str(dest))
        store.queue_publish(clip.id, CHAT)
        if k == 2:
            store.update_publish(clip.id, status="failed", attempts=0,
                                 error="Postiz answered 400: YouTube: the title is longer than 100 characters")
            continue
        store.update_publish(clip.id, status="scheduled")
        at = now + (5 + k * 6) * 3600
        for ch in CHANNELS:
            platform = "instagram" if ch["identifier"].startswith("instagram") else ch["identifier"]
            store.add_post(f"post-{clip.id}-{platform}", clip.id, ch["id"], platform, ch["name"], at, "m1", "x")
    posted = store.list_clips("approved")
    posted = [c for c in posted if c.job_id == old.id][0]
    store.queue_publish(posted.id, CHAT)
    store.update_publish(posted.id, status="scheduled")
    for ch in CHANNELS:
        platform = "instagram" if ch["identifier"].startswith("instagram") else ch["identifier"]
        pid = f"post-{posted.id}-{platform}"
        store.add_post(pid, posted.id, ch["id"], platform, ch["name"], now - 86400, "m0", "x")
        if platform == "instagram":
            store.update_post(pid, status="failed", error="Postiz couldn't post it")
        else:
            store.update_post(pid, status="published", url={"youtube": "https://youtube.com/shorts/demo",
                                                            "tiktok": "https://www.tiktok.com/messages"}[platform])
    job(vod, 9300, 9435, 2, "cancelled", 7200, error="Cancelled from the dashboard")

    # A finished scan with a real timeline, a running one and a failed one.
    sig = synthetic_stream(3 * 3600)
    scan = store.create_scan(CHAT, vod)
    store.update_scan(scan.id, status="done", title="VALOR MORTIS! Enshrouded starts SOON (After Valor Mortis!)",
                      duration=float(sig.duration), signals='["chat 9,812 msgs", "audio", "4 viewer clips"]')
    scored = score(sig)
    for rank, m in enumerate(find_moments(sig, scored=scored), 1):
        row = store.add_moment(scan.id, rank, m.peak, m.start, m.end, m.score, info={
            "chat_ratio": m.chat_ratio, "loud": m.loud, "clips": m.clips, "clip_views": m.clip_views,
            "replayed": m.replayed, "top_tokens": m.top_tokens})
        if rank == 1:
            store.claim_moment(row.id, done.id)
    store.save_timeline(scan.id, timeline(sig, *scored))
    running = store.create_scan(CHAT, "https://www.youtube.com/watch?v=zn1dXa6W7t4")
    store.update_scan(running.id, status="running")
    bad = store.create_scan(CHAT, "https://kick.com/somebody/videos/11111111-2222-3333-4444-555555555555")
    store.update_scan(bad.id, status="failed", error="this stream hasn't finished yet. Scan it after it ends, or /watch the channel.")

    log = [("INFO", "pipeline", f"job {done.id}: collected 5 clip(s)", done.id, None, None),
           ("WARNING", "pipeline", f"job {failed.id} failed: OpenShorts refused the job (400): Sign in to confirm you're not a bot", failed.id, None, None),
           ("WARNING", "scout", f"scan signal chat failed: TwitchDownloaderCLI failed: Video is sub-only", None, bad.id, None),
           ("ERROR", "pipeline", "outbox loop error", None, None,
            'Traceback (most recent call last):\n  File "/app/app/pipeline.py", line 88, in _loop\n    await step()\n'
            '  File "/app/app/pipeline.py", line 281, in flush_outbox\n    resp = await self.notify_client.post(...)\n'
            'httpx.ConnectError: [Errno 111] Connection refused'),
           ("INFO", "scout", f"scan {scan.id}: 5 moment(s) from chat 9,812 msgs, audio, 4 viewer clips", None, scan.id, None)]
    for i, (level, source, message, job_id, scan_id, detail) in enumerate(log):
        store.add_log(now - 3000 + i * 400, level, source, message, job_id, scan_id, detail)

    store.enqueue_event({"event": "message", "chat_id": CHAT, "text": f"✅ Job {done.id} finished: 5 clip(s)."})
    store.enqueue_event({"event": "clip_ready", "chat_id": CHAT, "caption": "🎬 Clip 2/5 · 0:06\nChat lost it after this play"})
    dead = store.outbox()[0]["id"]
    store.event_failed(dead, 40, 0, dead=True)
    store.close()


def openshorts_mock(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/api/status/5a1c0d4e-0000-4000-8000-00000000c0de":
        return httpx.Response(200, json={"status": "processing", "logs": [
            "📥 Source received (2:15)", "🎙️ Transcribing with faster-whisper (small, int8)…",
            "🧠 Picking moments with gemini-3.1-flash-lite", "🎬 Rendering clip 1/1: reframing 9:16 (screencast layout)"]})
    return httpx.Response(404, json={"detail": "not found"})


def postiz_mock(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api/public/v1")
    if path == "/is-connected":
        return httpx.Response(200, json={"connected": True})
    if path == "/integrations":
        return httpx.Response(200, json=CHANNELS)
    if path == "/posts" and request.method == "GET":
        return httpx.Response(200, json={"posts": []})
    if request.method == "DELETE":
        return httpx.Response(200, json={"error": False})
    return httpx.Response(404, json={"msg": "not in the demo"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    if args.reseed and DATA.exists():
        shutil.rmtree(DATA)
    settings = dataclasses.replace(
        Settings.from_env(), data_dir=DATA, dashboard_password="demo", notify_token="demo-token",
        allowed_chat_ids=frozenset({CHAT}), web_dir=ROOT / "clipbot" / "web" / "dist",
        n8n_url=f"http://127.0.0.1:{args.port}", openshorts_url="http://openshorts.demo",
        openshorts_output_dir=DATA / "os-output", openshorts_uploads_dir=DATA / "os-uploads",
        postiz_api_key="demo", postiz_url="http://postiz.demo/api", postiz_public_url="https://postiz.example.com")
    if not settings.db_path.exists():
        DATA.mkdir(parents=True, exist_ok=True)
        asyncio.run(seed(settings))
    storage.size_of = demo_size
    app = create_app(settings, openshorts=OpenShorts(settings.openshorts_url,
                                                     httpx.AsyncClient(transport=httpx.MockTransport(openshorts_mock))),
                     run_background=False,
                     publisher_factory=lambda s, st: Publisher(s, st, Postiz(
                         s.postiz_url, s.postiz_api_key, httpx.AsyncClient(transport=httpx.MockTransport(postiz_mock)))))

    @app.get("/healthz", include_in_schema=False)
    async def n8n_health():  # stands in for n8n
        return {"status": "ok"}

    # Registered after the catch-all, so move it to the front.
    app.router.routes.insert(0, app.router.routes.pop())
    print(f"Dashboard demo on http://127.0.0.1:{args.port}  (password: demo)", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
