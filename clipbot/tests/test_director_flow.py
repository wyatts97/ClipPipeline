"""The Director wired in: a range job's clips chosen by Gemini (with the streamer's notes, your note,
chat and the scan's hints) and sent to OpenShorts as preset_clips; the fallbacks; a scan keeping and
narrowing the moments Gemini found real; auto-clip and 🎬 carrying the picks along."""
import asyncio
import dataclasses
import shutil
from types import SimpleNamespace

import httpx
import pytest

from app import chat, director, media
from app.chat import ChatMessage
from app.director import Pick
from app.gemini import QuotaExhausted
from app.openshorts import OpenShorts
from app.pipeline import Pipeline
from app.platforms import LatestVod
from app.scoring import Moment, StreamSignals
from app.scout import Scout
from app.store import Store
from test_pipeline import FakeN8n, FakeOpenShorts

VOD = "https://www.twitch.tv/videos/555"


def run(coro):
    return asyncio.run(coro)


def pick(start, end, score=80, what="Kevin throws the monitor", window="w1"):
    return Pick(start, end, score, "chaos", what, "it breaks", "kevin did it again", "Kevin destroys the monitor",
                "rip #lirik", "rip", window)


@pytest.fixture
def env(settings, sample_video, monkeypatch):
    cfg = dataclasses.replace(settings, gemini_api_key="k", twitch_downloader="tdcli")
    fake_os = FakeOpenShorts(cfg.openshorts_output_dir, sample_video)
    store = Store(cfg.db_path)
    pipeline = Pipeline(cfg, store, OpenShorts(cfg.openshorts_url, httpx.AsyncClient(
        transport=httpx.MockTransport(fake_os.handler))), httpx.AsyncClient(transport=httpx.MockTransport(FakeN8n().handler)))

    async def fake_download(url, start, end, out_dir, cookies=None, **kw):
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{kw.get('name', 'source')}.mp4"
        shutil.copyfile(sample_video, dst)
        return dst

    async def fake_chat(vod_id, work_dir, binary="", start=None, end=None):
        assert (vod_id, binary, start, end) == ("555", "tdcli", 3720.0, 4320.0)
        return [ChatMessage(3721.0, "a", "KEKW"), ChatMessage(3721.5, "b", "KEKW"),
                ChatMessage(3722.0, "c", "he threw the monitor"), ChatMessage(4000.0, "d", "lol")]

    monkeypatch.setattr(media, "download_section", fake_download)
    monkeypatch.setattr(chat, "fetch_twitch_chat", fake_chat)
    watch = store.add_watch(111, "twitch", "lirik", None)
    store.update_watch(watch.id, notes="Kevin is his roommate")
    store.save_vods(watch.id, [LatestVod("555", VOD, 9000.0, False, "Kevin vs the monitor")])
    yield SimpleNamespace(cfg=cfg, store=store, pipeline=pipeline, fake_os=fake_os, watch=watch)
    store.close()


def fake_direct(seen, picks=None, error=None):
    async def direct(settings, windows, ctx, client=None):
        seen.update(windows=windows, ctx=ctx, proxies=[w.path.is_file() for w in windows])
        if error:
            raise error
        return picks or []
    return direct


def test_range_job_cuts_what_gemini_picked(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(director, "direct", fake_direct(seen, [pick(10.0, 40.0)]))
    job = env.store.create_job(111, VOD, 3720.0, 4320.0, 2, note="the monitor smash")
    run(env.pipeline.start_job(job))

    ctx, w = seen["ctx"], seen["windows"][0]
    assert seen["proxies"] == [True]                                    # it watched a real proxy
    assert (ctx.streamer, ctx.notes, ctx.note, ctx.title) == (
        "lirik (Twitch)", "Kevin is his roommate", "the monitor smash", "Kevin vs the monitor")
    assert ctx.want == 2 and ctx.hints == [] and (ctx.min_len, ctx.max_len) == (15, 60)
    assert w.start == 0 and w.chat == [(1.0, "KEKW", 2), (2.0, "he threw the monitor", 1)]   # section clock, in it only
    body = env.fake_os.process_bodies[0]
    assert body["preset_clips"] == [{
        "start": 10.0, "end": 40.0, "predicted_score": 80, "category": "chaos", "what_happened": "Kevin throws the monitor",
        "video_title_for_youtube_short": "Kevin destroys the monitor", "viral_hook_text": "kevin did it again",
        "video_description_for_tiktok": "rip #lirik", "video_description_for_instagram": "rip"}]
    saved = env.store.get_job(job.id).preset_picks()
    assert (saved[0]["start"], saved[0]["end"]) == (3730.0, 3760.0)    # kept in VOD seconds
    assert env.store.get_job(job.id).status == "processing"


def test_a_scans_picks_are_hints_for_the_exact_cut(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(director, "direct", fake_direct(seen, [pick(9.0, 41.0)]))
    job = env.store.create_job(111, VOD, 3720.0, 4320.0, 1, preset=[pick(3732.0, 3762.0).to_dict()])
    run(env.pipeline.start_job(job))
    hint = seen["ctx"].hints[0]
    assert (hint.start, hint.end, seen["ctx"].want) == (12.0, 42.0, None)
    assert env.fake_os.process_bodies[0]["preset_clips"][0]["start"] == 9.0


@pytest.mark.parametrize("error,picks", [(QuotaExhausted("Gemini's free quota for m is used up for today"), None),
                                         (None, [])])
def test_openshorts_picks_when_the_director_cant(env, monkeypatch, error, picks):
    monkeypatch.setattr(director, "direct", fake_direct({}, picks, error))
    job = env.store.create_job(111, VOD, 3720.0, 4320.0, 2)
    run(env.pipeline.start_job(job))
    body = env.fake_os.process_bodies[0]
    assert "preset_clips" not in body and body["target_clips"] == 2
    told = [e["payload"]["text"] for e in env.store.outbox()]
    assert any("free quota" in t for t in told) == bool(error)


def test_no_key_no_director(env, monkeypatch):
    monkeypatch.setattr(director, "direct", fake_direct({}, error=AssertionError("not called")))
    env.pipeline.settings = dataclasses.replace(env.cfg, gemini_api_key="")
    run(env.pipeline.start_job(env.store.create_job(111, VOD, 3720.0, 4320.0, 2)))
    assert "preset_clips" not in env.fake_os.process_bodies[0]


def test_scan_keeps_the_moments_gemini_found_real(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(director, "direct", fake_direct(seen, [
        pick(1010.0, 1040.0, 60, "he reads a donation", "w1"),
        pick(3020.0, 3050.0, 90, "Kevin throws the monitor", "w3"), pick(3060.0, 3080.0, 70, "chat loses it", "w3")]))
    scan = env.store.create_scan(111, VOD, env.watch.id)
    sig = StreamSignals(9000.0, messages=[ChatMessage(1005.0, "a", "KEKW"), ChatMessage(3030.0, "b", "NOOO")])
    moments = [Moment(1000, 900, 1080, 5.0, clip_titles=["donation"]), Moment(2000, 1900, 2080, 4.0),
               Moment(3030, 2950, 3100, 3.0, loud=True)]
    scout = Scout(env.cfg, env.store, SimpleNamespace(wake=lambda: None))
    kept = run(scout.direct_scan(env.store.get_scan(scan.id), "Kevin vs the monitor", sig, moments, env.cfg.data_dir / "w"))

    assert [m.peak for m in kept] == [3030, 1000]                         # best first; the empty one dropped
    assert (kept[0].start, kept[0].end) == (3000.0, 3100.0)              # narrowed around its clips
    assert [p["what_happened"] for p in kept[0].picks] == ["Kevin throws the monitor", "chat loses it"]
    ctx = seen["ctx"]
    assert (ctx.streamer, ctx.notes, ctx.want) == ("lirik (Twitch)", "Kevin is his roommate", None)
    w1 = seen["windows"][0]
    assert (w1.start, w1.end, w1.clip_titles, w1.chat) == (900, 1080, ["donation"], [(1005.0, "KEKW", 1)])
    assert "loud" in seen["windows"][2].signals

    # Stored with the moment; 🎬 and auto-clip hand the picks to the job.
    rows = [env.store.add_moment(scan.id, i, m.peak, m.start, m.end, m.score, info=m.info()) for i, m in enumerate(kept, 1)]
    assert rows[0].details["what_happened"] == "Kevin throws the monitor" and rows[0].details["director_score"] == 90
    env.store.update_watch(env.watch.id, auto_clip=1)
    [job] = scout.auto_clip(env.store.get_scan(scan.id), rows)
    assert job.target_clips == 2 and [p["start"] for p in job.preset_picks()] == [3020.0, 3060.0]
    result, _ = scout.choose(111, f"sc:{rows[1].id}:2")                 # wider: Gemini picks two around it
    wide = env.store.get_job(env.store.moments_for_scan(scan.id)[1].job_id)
    assert result == "Queued" and wide.note == "he reads a donation" and wide.preset is None and wide.target_clips == 2
