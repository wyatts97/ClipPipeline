"""Scan flow, 🎬 buttons and the watcher, with the network parts monkeypatched."""
import asyncio
import dataclasses

import pytest

from app import chat, scout as scout_mod, signals
from app.chat import ChatMessage
from app.media import MediaError
from app.platforms import Channel, LatestVod
from app.scout import Scout
from app.store import Store

VOD = "https://www.twitch.tv/videos/555"


class FakePipeline:
    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1


@pytest.fixture
def env(settings):
    store = Store(settings.db_path)
    pipeline = FakePipeline()
    yield Scout(settings, store, pipeline), store, pipeline
    store.close()


def stream_chat():
    msgs = [ChatMessage(t + 0.5, f"u{t % 300}", "ok") for t in range(0, 3600, 3)]
    msgs += [ChatMessage(1800 + i * 0.25, f"fan{i}", "KEKW") for i in range(60)]
    msgs += [ChatMessage(2700 + i * 0.25, f"w{i}", "LUL") for i in range(40)]
    return msgs


def fake_sources(monkeypatch, info=None, chat_error=None, audio=True):
    calls = {}

    async def probe(url, cookies=None):
        calls["probe"] = url
        return info or {"duration": 3600, "title": "Big stream", "url": "https://cdn/audio.m3u8",
                        "protocol": "m3u8_native", "http_headers": {"User-Agent": "x"}}

    async def twitch_chat(vod_id, work_dir, binary="TwitchDownloaderCLI"):
        calls["chat"] = vod_id
        if chat_error:
            raise MediaError(chat_error)
        return stream_chat()

    async def loudness(url, headers, duration):
        calls["audio"] = (url, headers)
        if not audio:
            raise MediaError("ffmpeg produced no loudness data")
        return [-30.0] * 3600

    async def download_audio(url, out_dir, cookies=None):
        calls["download"] = url
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "audio.webm"
        path.write_bytes(b"x")
        return path

    monkeypatch.setattr(signals, "probe", probe)
    monkeypatch.setattr(signals, "download_audio", download_audio)
    monkeypatch.setattr(chat, "fetch_twitch_chat", twitch_chat)
    monkeypatch.setattr(signals, "loudness", loudness)
    return calls


def events(store):
    return [payload for _, payload, _ in store.due_events(limit=100)]


def test_scan_sends_moments_with_buttons(env, monkeypatch):
    scout, store, _ = env
    calls = fake_sources(monkeypatch)
    scan = scout.queue_scan(111, VOD)
    asyncio.run(scout.run_scan(scan))

    assert calls["chat"] == "555" and calls["audio"] == ("https://cdn/audio.m3u8", {"User-Agent": "x"})
    assert store.get_scan(scan.id).status == "done"
    out = events(store)
    assert out[0]["event"] == "message" and "Big stream" in out[0]["text"]
    assert "chat 1,300 msgs" in out[0]["text"] and "audio" in out[0]["text"]
    choices = [e for e in out if e["event"] == "choice"]
    assert choices and choices[0]["chat_id"] == 111
    first = choices[0]
    assert first["text"].startswith("#1 🔥 30:")  # the KEKW spike at 30:00
    assert "KEKW ×" in first["text"] and "https://www.twitch.tv/videos/555?t=0h28m" in first["text"]
    moment = store.moments_for_scan(scan.id)[0]
    assert [b["data"] for b in first["buttons"]] == [f"sc:{moment.id}:1", f"sc:{moment.id}:2"]


def test_one_failing_signal_is_only_noted(env, monkeypatch):
    scout, store, _ = env
    fake_sources(monkeypatch, chat_error="sub-only VOD")
    scan = scout.queue_scan(111, VOD)
    asyncio.run(scout.run_scan(scan))
    assert store.get_scan(scan.id).status == "done"
    assert "chat unavailable (sub-only VOD)" in events(store)[0]["text"]


def test_non_hls_audio_is_downloaded_first(env, monkeypatch):
    scout, store, _ = env
    calls = fake_sources(monkeypatch, info={"duration": 3600, "title": "yt", "protocol": "https",
                                            "url": "https://googlevideo/x", "http_headers": {}})
    scan = scout.queue_scan(111, "https://youtu.be/abcdefghijk")
    asyncio.run(scout.run_scan(scan))
    assert calls["download"] == "https://youtu.be/abcdefghijk"
    assert calls["audio"][0].endswith("audio.webm") and calls["audio"][1] is None
    assert store.get_scan(scan.id).status == "done"


def test_scan_of_a_live_stream_fails_politely(env, monkeypatch):
    scout, store, _ = env
    fake_sources(monkeypatch, info={"duration": None, "live_status": "is_live", "url": "u"})
    scan = scout.queue_scan(111, VOD)
    asyncio.run(scout.run_scan(scan))
    assert store.get_scan(scan.id).status == "failed"
    assert "hasn't finished yet" in events(store)[0]["text"]


def test_scan_with_no_signal_fails(env, monkeypatch):
    scout, store, _ = env
    fake_sources(monkeypatch, info={"duration": 600, "title": "x", "url": "u"}, audio=False)
    scan = scout.queue_scan(111, "https://example.com/v.mp4")  # no chat on "other" sites
    asyncio.run(scout.run_scan(scan))
    assert store.get_scan(scan.id).status == "failed"
    assert "no signal could be read" in events(store)[0]["text"]


def test_choose_queues_a_range_job_once(env, monkeypatch):
    scout, store, pipeline = env
    fake_sources(monkeypatch)
    scan = scout.queue_scan(111, VOD)
    asyncio.run(scout.run_scan(scan))
    m1, m2 = store.moments_for_scan(scan.id)[:2]

    toast, reply = scout.choose(111, f"sc:{m1.id}:1")
    assert toast == "Queued" and "1 clip(s)" in reply and pipeline.woken == 1
    job = store.recent_jobs(111)[0]
    assert (job.source_url, job.range_start, job.range_end, job.target_clips) == (VOD, m1.start, m1.end, 1)
    assert scout.choose(111, f"sc:{m1.id}:1") == ("Already queued", None)
    assert scout.choose(222, f"sc:{m2.id}:1") == ("Unknown moment", None)

    toast, reply = scout.choose(111, f"sc:{m2.id}:2")
    wide = store.recent_jobs(111)[0]
    assert (wide.range_start, wide.range_end, wide.target_clips) == (max(0, m2.start - 60), m2.end + 60, 2)


# ---- watcher -------------------------------------------------------------------
def watch_with(store, chat_id=111, last=None, pending=None, pending_duration=None):
    w = store.add_watch(chat_id, "twitch", "lirik", last)
    store.update_watch(w.id, pending_vod_id=pending, pending_duration=pending_duration)
    return store.watches()[0]


CH = Channel("twitch", "lirik")


def test_watch_baseline_that_was_already_finished(env):
    scout, store, _ = env
    w = watch_with(store, last="100", pending="100", pending_duration=20000)
    assert scout.step_watch(w, CH, LatestVod("100", "u100", 20000, False)) is None
    w = store.watches()[0]
    assert (w.last_vod_id, w.pending_vod_id) == ("100", None)
    assert scout.step_watch(w, CH, LatestVod("100", "u100", 20000, False)) is None
    assert store.scans_with_status("queued") == []


def test_watch_baseline_that_was_live_gets_scanned_when_it_ends(env):
    scout, store, _ = env
    w = watch_with(store, last="100", pending="100", pending_duration=5000)
    scout.step_watch(w, CH, LatestVod("100", "u100", 6000, False))   # grew: it was live
    scout.step_watch(store.watches()[0], CH, LatestVod("100", "u100", 7000, False))  # still growing
    scan = scout.step_watch(store.watches()[0], CH, LatestVod("100", "u100", 7000, False))  # stable
    assert scan and scan.source_url == "u100"
    assert store.watches()[0].last_vod_id == "100"
    assert "finished a 1:56:40 stream" in events(store)[0]["text"]
    assert scout.step_watch(store.watches()[0], CH, LatestVod("100", "u100", 7000, False)) is None


def test_new_vod_waits_until_it_stops_growing(env):
    scout, store, _ = env
    w = watch_with(store, last="100")
    assert scout.step_watch(w, CH, LatestVod("200", "u200", 600, False)) is None      # first sighting
    assert scout.step_watch(store.watches()[0], CH, LatestVod("200", "u200", 1500, False)) is None
    assert scout.step_watch(store.watches()[0], CH, LatestVod("200", "u200", None, True)) is None  # live flag
    assert scout.step_watch(store.watches()[0], CH, LatestVod("200", "u200", 9000, False)) is None
    scan = scout.step_watch(store.watches()[0], CH, LatestVod("200", "u200", 9000, False))
    assert scan and scan.source_url == "u200" and scan.chat_id == 111


def test_add_watch_replies(env, monkeypatch):
    scout, store, _ = env

    async def vods(channel, client=None, limit=8):
        return [LatestVod("100", "https://www.twitch.tv/videos/100", 3600, False, "Big stream")]

    monkeypatch.setattr(scout_mod, "list_vods", vods)
    reply = asyncio.run(scout.add_watch(111, "https://twitch.tv/lirik"))
    assert reply.startswith("👀 Watching twitch/lirik") and "videos/100" in reply
    assert asyncio.run(scout.add_watch(111, "twitch.tv/lirik")) == "Already watching twitch/lirik."
    assert asyncio.run(scout.add_watch(111, "lirik")).startswith("⚠️")
    assert scout.list_watches(111) == "Watching:\n• twitch/lirik"
    assert scout.remove_watch(111, "twitch:lirik") == "Stopped watching twitch/lirik."
    assert scout.list_watches(111).startswith("Not watching")
