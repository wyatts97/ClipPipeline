"""Streamers: VOD lists (Kick API, yt-dlp listings), adding with settings, refresh errors, backfill,
the auto-scan switch, auto-clipping a scan's top moments, and the dashboard API."""
import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_scout import FakePipeline, events, fake_sources

from app import platforms, scout as scout_mod
from app.main import create_app
from app.media import MediaError
from app.openshorts import OpenShorts
from app.platforms import Channel, LatestVod
from app.scout import Scout
from app.store import Store

CH = Channel("twitch", "lirik")
VODS = [LatestVod("300", "https://www.twitch.tv/videos/300", None, True, "Live now"),
        LatestVod("200", "https://www.twitch.tv/videos/200", 7200, False, "Yesterday", 1792300000.0),
        LatestVod("100", "https://www.twitch.tv/videos/100", 3600, False, "Last week", 1791800000.0)]
PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(settings, monkeypatch):
    store = Store(settings.db_path)
    listed = {"vods": list(VODS), "error": None}

    async def list_vods(channel, client=None, limit=8):
        if listed["error"]:
            raise MediaError(listed["error"])
        return listed["vods"]

    monkeypatch.setattr(scout_mod, "list_vods", list_vods)
    yield Scout(settings, store, FakePipeline()), store, listed
    store.close()


# ---- listing --------------------------------------------------------------------------------
def test_kick_listing():
    def kick(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/channels/xqc/videos"
        return httpx.Response(200, json=[
            {"video": {"uuid": "11111111-2222-3333-4444-555555555555"}, "duration": 3_600_000, "is_live": False,
             "session_title": "Reacting", "created_at": "2026-10-18 20:00:00"},
            {"video": {}, "duration": 0},
            {"video": {"uuid": "66666666-2222-3333-4444-555555555555"}, "duration": 0, "is_live": True}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(kick))
    vods = run(platforms.list_vods(Channel("kick", "xqc"), client))
    assert [(v.vod_id[:8], v.duration, v.live, v.title) for v in vods] == [
        ("11111111", 3600.0, False, "Reacting"), ("66666666", None, True, None)]
    assert vods[0].url == "https://kick.com/xqc/videos/11111111-2222-3333-4444-555555555555"
    assert vods[0].published == 1792353600.0


def test_ytdlp_listing(monkeypatch):
    seen = {}

    async def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        return json.dumps({"entries": [
            {"id": "upcoming1", "live_status": "is_upcoming"},
            {"id": "abcdefghijk", "title": "Stream VOD", "duration": 9000, "live_status": "was_live",
             "upload_date": "20261017"},
            {"id": "zyxwvutsrqp", "title": "Older", "duration": 5000, "timestamp": 1791000000}]})

    monkeypatch.setattr(platforms, "run", fake_run)
    vods = run(platforms.list_vods(Channel("youtube", "@someone"), limit=5))
    assert seen["cmd"][-1] == "https://www.youtube.com/@someone/streams" and "1:7" in seen["cmd"]
    assert [(v.vod_id, v.title, v.live) for v in vods] == [("abcdefghijk", "Stream VOD", False),
                                                           ("zyxwvutsrqp", "Older", False)]
    assert vods[0].published == 1792195200.0 and vods[1].published == 1791000000.0
    assert run(platforms.latest_vod(Channel("youtube", "@someone"))).vod_id == "abcdefghijk"
    assert platforms.channel_url(Channel("youtube", "@someone")) == "https://www.youtube.com/@someone"


# ---- streamers ------------------------------------------------------------------------------
def test_add_streamer_keeps_settings_and_vods(env):
    scout, store, _ = env
    watch, latest = run(scout.add_streamer(111, CH, auto_scan=True, auto_clip=2, template="meme"))
    assert (watch.auto_scan, watch.auto_clip, watch.template, latest.vod_id) == (1, 2, "meme", "300")
    assert [v.vod_id for v in store.vods_for_watch(watch.id)] == ["300", "200", "100"]   # live one first
    assert watch.pending_vod_id == "300" and watch.last_vod_id is None      # live: scanned when it ends
    assert watch.checked and watch.check_error is None
    with pytest.raises(ValueError):
        run(scout.add_streamer(111, CH))


def test_refresh_keeps_the_error(env):
    scout, store, listed = env
    watch, _ = run(scout.add_streamer(111, CH))
    listed["error"] = "HTTP Error 404"
    with pytest.raises(MediaError):
        run(scout.refresh(watch))
    assert store.get_watch(watch.id).check_error == "HTTP Error 404"
    listed["error"] = None
    listed["vods"] = [LatestVod("400", "https://www.twitch.tv/videos/400", 100, False, "New")] + VODS
    run(scout.refresh(watch))
    assert store.get_watch(watch.id).check_error is None
    assert "400" in [v.vod_id for v in store.vods_for_watch(watch.id)]


def test_backfill_skips_live_and_scanned(env):
    scout, store, _ = env
    watch, _ = run(scout.add_streamer(111, CH))
    done = store.create_scan(111, "https://www.twitch.tv/videos/200")
    store.update_scan(done.id, status="done")
    scans = scout.backfill(watch, 3)
    assert [s.source_url for s in scans] == ["https://www.twitch.tv/videos/100"]   # 300 live, 200 scanned
    assert scans[0].watch_id == watch.id
    failed = store.create_scan(111, "https://www.twitch.tv/videos/200")
    store.update_scan(done.id, status="failed")
    store.update_scan(failed.id, status="failed")
    assert [s.source_url for s in scout.backfill(watch, 3)] == ["https://www.twitch.tv/videos/200"]


def test_auto_scan_off_only_remembers(env):
    scout, store, _ = env
    watch, _ = run(scout.add_streamer(111, CH, auto_scan=False))
    w = store.get_watch(watch.id)
    ended = LatestVod("300", "https://www.twitch.tv/videos/300", 9000, False)
    scout.step_watch(w, CH, ended)                                    # length known: not stable yet
    assert scout.step_watch(store.get_watch(w.id), CH, ended) is None  # stable, but auto-scan is off
    assert store.get_watch(w.id).last_vod_id == "300" and store.scans_with_status("queued") == []


def test_finished_stream_scans_with_its_streamer(env):
    scout, store, _ = env
    watch, _ = run(scout.add_streamer(111, CH))
    ended = LatestVod("300", "https://www.twitch.tv/videos/300", 9000, False)
    scout.step_watch(store.get_watch(watch.id), CH, ended)
    scan = scout.step_watch(store.get_watch(watch.id), CH, ended)
    assert scan and scan.watch_id == watch.id


def test_auto_clip_after_scan(env, monkeypatch):
    scout, store, _ = env
    fake_sources(monkeypatch)
    watch, _ = run(scout.add_streamer(111, CH, auto_clip=1, template="hype"))
    scan = scout.queue_scan(111, "https://www.twitch.tv/videos/555", watch.id)
    run(scout.run_scan(scan))
    (job,) = store.list_jobs()
    moments = store.moments_for_scan(scan.id)
    assert len(moments) >= 2 and job.template == "hype"
    assert moments[0].job_id == job.id and moments[1].job_id is None      # only the top one
    assert (job.range_start, job.range_end) == (moments[0].start, moments[0].end)
    assert any("🤖 Auto-clipping the top 1 moment(s) for twitch/lirik: #1 → job" in e.get("text", "")
               for e in events(store))
    assert scout.pipeline.woken >= 1


def test_no_auto_clip_without_the_setting(env, monkeypatch):
    scout, store, _ = env
    fake_sources(monkeypatch)
    watch, _ = run(scout.add_streamer(111, CH))
    run(scout.run_scan(scout.queue_scan(111, "https://www.twitch.tv/videos/555", watch.id)))
    assert store.list_jobs() == []


# ---- dashboard API --------------------------------------------------------------------------
@pytest.fixture
def dash(settings, tmp_path, monkeypatch):
    listed = {"vods": list(VODS)}

    async def list_vods(channel, client=None, limit=8):
        return listed["vods"]

    monkeypatch.setattr(scout_mod, "list_vods", list_vods)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    client = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with TestClient(create_app(cfg, openshorts=client, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_streamer_api(dash):
    assert dash.post("/api/streamers", json={"url": "lirik"}).status_code == 422
    assert dash.post("/api/streamers", json={"url": "twitch.tv/lirik", "template": "nope"}).status_code == 422
    r = dash.post("/api/streamers", json={"url": "https://twitch.tv/lirik", "auto_clip": 3, "template": "meme",
                                          "backfill": 5})
    assert r.status_code == 201
    s = r.json()
    assert (s["label"], s["url"], s["auto_clip"], s["template_name"]) == (
        "twitch/lirik", "https://www.twitch.tv/lirik", 3, "Meme")
    by_id = {v["vod_id"]: v for v in s["vods"]}
    assert by_id["300"]["live"] and by_id["300"]["scan"] is None
    assert by_id["200"]["scan"]["status"] == "queued" and by_id["100"]["scan"]["status"] == "queued"   # backfill
    assert dash.post("/api/streamers", json={"url": "twitch.tv/lirik"}).status_code == 409

    sid = s["id"]
    edited = dash.patch(f"/api/streamers/{sid}", json={"auto_scan": False, "auto_clip": 50, "template": ""}).json()
    assert (edited["auto_scan"], edited["auto_clip"], edited["template"]) == (False, 10, None)
    assert edited["notes"] == ""
    notes = dash.patch(f"/api/streamers/{sid}", json={"notes": "  Kevin = roommate, breaks stuff  "}).json()
    assert notes["notes"] == "Kevin = roommate, breaks stuff" and notes["auto_scan"] is False
    assert dash.patch(f"/api/streamers/{sid}", json={"notes": ""}).json()["notes"] == ""
    assert dash.post(f"/api/streamers/{sid}/scan", json={"vod_id": "300"}).status_code == 409     # live
    assert dash.post(f"/api/streamers/{sid}/scan", json={"vod_id": "nope"}).status_code == 404
    scan = dash.post(f"/api/streamers/{sid}/scan", json={"vod_id": "100"}).json()
    assert dash.app.state.store.get_scan(scan["id"]).watch_id == sid
    assert dash.post(f"/api/streamers/{sid}/refresh").json()["checked"]
    assert dash.post(f"/api/streamers/{sid}/backfill", json={"count": 2}).json()["queued"] == []   # all scanned
    assert len(dash.get("/api/streamers").json()["items"]) == 1
    assert dash.delete(f"/api/streamers/{sid}").json() == {"ok": True}
    assert dash.get("/api/streamers").json()["items"] == []
