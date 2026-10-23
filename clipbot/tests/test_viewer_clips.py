"""Viewer clips first: scans that rank the clips viewers made by their views (overlapping clips are one
moment), fill up with chat and loudness, 🎬 that clips the viewer's own cut, the cut as made when the
Director can't help, Telegram's /scan … viewers, and the dashboard's mode, Scan again and Clip top N."""
import asyncio
import dataclasses

from fastapi.testclient import TestClient

from app import clips_api, scoring
from app.clips_api import ClipMarker
from app.main import create_app
from app.parsing import parse_message
from app.pipeline import Pipeline
from app.scout import Scout
from app.store import Store
from test_scout import VOD, FakePipeline, events, fake_sources

PASSWORD = "correct horse"
CLIPS = [ClipMarker(1500, 30, 900, "he smashed the monitor"), ClipMarker(1510, 40, 3000, "MONITOR GONE"),
         ClipMarker(2400, 6, 50, "lol"), ClipMarker(3000, 25, 400, "the raid")]


def test_viewer_moments():
    moments = scoring.viewer_moments(CLIPS, 3600, top=10, min_len=15)
    # The two monitor clips are one moment (3,900 views), then the raid (400), then the 6-s "lol".
    assert [(m.clips, m.clip_views) for m in moments] == [(2, 3900), (1, 400), (1, 50)]
    first = moments[0]
    assert first.source == "viewers" and first.clip_titles == ["MONITOR GONE", "he smashed the monitor"]
    pick = first.picks[0]                                         # its most viewed clip, cut as made
    assert (pick["start"], pick["end"], pick["category"]) == (1510, 1550, scoring.VIEWER_CLIP)
    assert pick["what_happened"] == pick["hook"] == "MONITOR GONE" and (first.start, first.end) == (1500, 1558)
    short = moments[2].picks[0]
    assert (short["start"], short["end"]) == (2395.5, 2410.5)       # grown to 15 s around its middle
    assert first.info()["source"] == "viewers" and "source" not in scoring.Moment(1, 0, 2, 1).info()
    assert scoring.viewer_moments(CLIPS, 3600, top=1)[0].clip_views == 3900


def scout_with_clips(settings, monkeypatch, clips=CLIPS):
    store = Store(settings.db_path)
    scout = Scout(dataclasses.replace(settings, twitch_client_id="tid", twitch_client_secret="ts", scan_top=4,
                                      director=False), store, FakePipeline())
    fake_sources(monkeypatch)

    async def markers(client, vod_id, client_id, client_secret, max_pages=5):
        return list(clips)

    monkeypatch.setattr(clips_api, "twitch_clip_markers", markers)
    return scout, store


def test_scan_with_viewer_clips_first(settings, monkeypatch):
    scout, store = scout_with_clips(settings, monkeypatch)
    scan = scout.queue_scan(111, VOD, mode="viewers")
    asyncio.run(scout.run_scan(scan))
    rows = store.moments_for_scan(scan.id)
    assert [r.details.get("source") for r in rows] == ["viewers", "viewers", "viewers", None]   # chat fills the last
    assert not any(rows[3].start < v.end and v.start < rows[3].end for v in rows[:3])           # not the same moment
    out = events(store)
    assert "viewer clips first: 3 moment(s) from viewer clips" in out[0]["text"]
    first = next(e for e in out if e["event"] == "choice")
    assert first["text"].startswith("#1 ✂️ “MONITOR GONE”  at 25:10") and "2 viewer clips · 3.9k views" in first["text"]

    toast, reply = scout.choose(111, f"sc:{rows[0].id}:1")
    job = store.recent_jobs(111)[0]
    assert (job.range_start, job.range_end, job.target_clips) == (1500, 1558, 1)
    assert job.preset_picks()[0]["category"] == scoring.VIEWER_CLIP

    # Without the Director, the job cuts the viewer's clip as made, on the downloaded section's clock.
    pipeline = Pipeline(settings, store, openshorts=None)
    cut = pipeline.viewer_presets(job)
    assert [(c["start"], c["end"], c["viral_hook_text"]) for c in cut] == [(10, 50, "MONITOR GONE")]
    assert pipeline.viewer_presets(store.create_job(111, VOD, 0, 60, 1)) is None
    asyncio.run(pipeline.notify_client.aclose())
    store.close()


def test_no_viewer_clips_falls_back(settings, monkeypatch):
    scout, store = scout_with_clips(settings, monkeypatch, clips=[])
    scan = scout.queue_scan(111, VOD, mode="viewers")
    asyncio.run(scout.run_scan(scan))
    assert all(r.details.get("source") is None for r in store.moments_for_scan(scan.id))
    assert "there were none, so chat and loudness picked" in events(store)[0]["text"]
    store.close()


def test_a_streamers_setting_and_telegram(settings):
    store = Store(settings.db_path)
    scout = Scout(settings, store, FakePipeline())
    watch = store.add_watch(111, "twitch", "lirik", None)
    store.update_watch(watch.id, mode="viewers")
    assert scout.queue_scan(111, VOD, watch.id).mode == "viewers"                  # its own setting
    assert scout.queue_scan(111, VOD, watch.id, "signals").mode is None            # unless told otherwise
    assert scout.queue_scan(111, VOD).mode is None
    store.close()
    assert parse_message(f"/scan {VOD} viewers").arg == "viewers"
    assert parse_message(f"/scan viewer clips {VOD}").arg == "viewers"
    assert parse_message(f"/scan {VOD}").arg is None


def test_dashboard(settings, tmp_path, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    with TestClient(create_app(cfg, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        assert c.post("/api/scans", json={"url": VOD, "mode": "loudest"}).status_code == 422
        scan = c.post("/api/scans", json={"url": VOD, "mode": "viewers"}).json()
        assert scan["mode"] == "viewers"
        assert c.post(f"/api/scans/{scan['id']}/rescan").json()["mode"] == "viewers"            # as it was
        assert c.post(f"/api/scans/{scan['id']}/rescan", json={"mode": "signals"}).json()["mode"] == "signals"

        store = c.app.state.store
        for rank, m in enumerate(scoring.viewer_moments(CLIPS, 3600), 1):
            store.add_moment(scan["id"], rank, m.peak, m.start, m.end, m.score, info=m.info())
        top = c.post(f"/api/scans/{scan['id']}/clip-top", json={"count": 2}).json()["jobs"]
        assert [j["target_clips"] for j in top] == [1, 1] and len(store.recent_jobs(111, limit=10)) == 2
        assert len(c.post(f"/api/scans/{scan['id']}/clip-top", json={"count": 5}).json()["jobs"]) == 1   # the rest
        assert c.post(f"/api/scans/{scan['id']}/clip-top", json={"count": 5}).status_code == 409

        watch = store.add_watch(111, "twitch", "lirik", None)
        edited = c.patch(f"/api/streamers/{watch.id}", json={"mode": "viewers"}).json()
        assert edited["mode"] == "viewers" and store.get_watch(watch.id).mode == "viewers"
        assert c.patch(f"/api/streamers/{watch.id}", json={"mode": "signals"}).json()["mode"] == "signals"
