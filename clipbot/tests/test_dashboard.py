"""Dashboard API: login, overview, clips, jobs, scans, logs, outbox and the static files."""
import dataclasses
import logging
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dashboard import COOKIE, make_session
from app.main import create_app
from app.openshorts import OpenShorts

PASSWORD = "correct horse"


def openshorts_mock(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(404)


@pytest.fixture
def dash(settings, tmp_path):
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web,
                              n8n_url="http://127.0.0.1:9")  # nothing listens there
    client = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(openshorts_mock)))
    with TestClient(create_app(cfg, openshorts=client, run_background=False)) as c:
        c.cfg = cfg
        yield c


def login(c):
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200


def seed_clip(c, sample_video, status_job="done"):
    store = c.app.state.store
    job = store.create_job(111, "https://www.twitch.tv/videos/555", 60.0, 200.0, 2)
    store.update_job(job.id, status=status_job)
    pending = c.cfg.folder("pending")
    master = pending / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "Big play", "No way 😳", {"video_description_for_tiktok": "desc"},
                          3.0, str(master), None)
    return job, clip


def test_login_flow(settings, dash):
    assert dash.get("/api/session").json() == {"configured": True, "authenticated": False}
    assert dash.get("/api/overview").status_code == 401
    assert dash.post("/api/login", json={"password": "nope"}).status_code == 401
    login(dash)
    assert dash.get("/api/session").json()["authenticated"] is True
    assert dash.get("/api/overview").status_code == 200
    dash.post("/api/logout")
    assert dash.get("/api/overview").status_code == 401


def test_forged_or_expired_cookie_is_refused(dash):
    dash.cookies.set(COOKIE, "9999999999.deadbeef")
    assert dash.get("/api/overview").status_code == 401
    dash.cookies.set(COOKIE, make_session(dash.cfg, now=0))  # expired long ago
    assert dash.get("/api/overview").status_code == 401
    dash.cookies.set(COOKIE, make_session(dash.cfg))
    assert dash.get("/api/overview").status_code == 200


def test_dashboard_off_without_password(settings, tmp_path):
    cfg = dataclasses.replace(settings, web_dir=tmp_path)
    with TestClient(create_app(cfg, run_background=False)) as c:
        assert c.get("/api/session").json()["configured"] is False
        assert c.get("/api/overview").status_code == 503
        assert c.post("/api/login", json={"password": ""}).status_code == 503


def test_overview(dash, sample_video):
    login(dash)
    job, _ = seed_clip(dash, sample_video)
    failed = dash.app.state.store.create_job(111, "https://youtu.be/x", None, None, 3)
    dash.app.state.store.update_job(failed.id, status="failed", error="OpenShorts refused the job")
    data = dash.get("/api/overview").json()
    assert data["pending_clips"] == 1 and data["pending_preview"][0]["title"] == "Big play"
    assert data["health"]["openshorts"] is True and data["health"]["n8n"] is False
    assert any(p["kind"] == "job" and p["id"] == failed.id for p in data["problems"])
    assert {a["id"] for a in data["activity"]} >= {job.id, failed.id}


def test_clip_edit_approve_and_files(dash, sample_video):
    login(dash)
    job, clip = seed_clip(dash, sample_video)
    items = dash.get("/api/clips").json()["items"]
    assert [i["id"] for i in items] == [clip.id] and items[0]["thumb_url"] is None

    edited = dash.patch(f"/api/clips/{clip.id}", json={"title": "Better title", "hook": "Wait for it"}).json()
    assert edited["title"] == "Better title" and edited["hook"] == "Wait for it"
    assert edited["description_tiktok"] == "desc"

    video = dash.get(f"/api/clips/{clip.id}/video")
    assert video.status_code == 200 and video.content[:12] == sample_video.read_bytes()[:12]
    ranged = dash.get(f"/api/clips/{clip.id}/video", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.content) == 100

    thumbs = dash.cfg.folder("thumbs")
    (thumbs / f"{job.id}_0.jpg").write_bytes(b"\xff\xd8jpeg")
    assert dash.get("/api/clips").json()["items"][0]["thumb_url"] == f"/api/clips/{clip.id}/thumb"
    assert dash.get(f"/api/clips/{clip.id}/thumb").content == b"\xff\xd8jpeg"

    result = dash.post(f"/api/clips/{clip.id}/decision", json={"decision": "approve"}).json()
    assert result["result"] == "Approved" and result["clip"]["status"] == "approved"
    sidecar = next(dash.cfg.folder("approved").glob("*.json"))
    assert '"title": "Better title"' in sidecar.read_text(encoding="utf-8")
    dash.patch(f"/api/clips/{clip.id}", json={"title": "Final title"})
    assert '"title": "Final title"' in sidecar.read_text(encoding="utf-8")
    assert dash.post(f"/api/clips/{clip.id}/decision", json={"decision": "reject"}).json()["result"] == \
        "Already approved"
    assert dash.get("/api/clips", params={"status": "approved"}).json()["items"][0]["id"] == clip.id


def test_bulk_reject(dash, sample_video):
    login(dash)
    _, a = seed_clip(dash, sample_video)
    _, b = seed_clip(dash, sample_video)
    out = dash.post("/api/clips/decisions", json={"ids": [a.id, b.id], "decision": "reject"}).json()
    assert [i["result"] for i in out["items"]] == ["Rejected", "Rejected"]
    assert dash.get("/api/clips").json()["items"] == []


def test_jobs_create_cancel_retry(dash):
    login(dash)
    created = dash.post("/api/jobs", json={"url": "https://www.twitch.tv/videos/9", "start": "1:00:00",
                                           "end": "1:05:00", "clips": 2})
    assert created.status_code == 201
    job = created.json()
    assert job["range"] == {"start": 3600.0, "end": 3900.0} and job["status"] == "queued"
    assert dash.app.state.store.get_job(job["id"]).chat_id == 111  # notifications go to your chat

    for bad, needle in (({"url": "youtu.be/x"}, "https://"),
                        ({"url": "https://x.y/v", "start": "1:00", "end": "1:20"}, "at least 45"),
                        ({"url": "https://x.y/v", "start": "5:00"}, "both"),
                        ({"url": "https://x.y/v", "start": "abc", "end": "1:00"}, "isn't a time")):
        resp = dash.post("/api/jobs", json=bad)
        assert resp.status_code == 422 and needle in resp.json()["detail"], (bad, resp.json())

    assert dash.post(f"/api/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert dash.post(f"/api/jobs/{job['id']}/cancel").status_code == 409
    retry = dash.post(f"/api/jobs/{job['id']}/retry").json()
    assert retry["retry_of"] == job["id"] and retry["status"] == "queued"
    listed = {j["id"]: j for j in dash.get("/api/jobs").json()["items"]}
    assert listed[job["id"]]["retried_by"] == retry["id"]
    assert dash.post(f"/api/jobs/{retry['id']}/retry").status_code == 409
    detail = dash.get(f"/api/jobs/{job['id']}").json()
    assert any("cancelled" in line["message"] for line in detail["logs"])


def test_scan_detail_and_clip_from_timeline(dash):
    login(dash)
    store = dash.app.state.store
    scan = store.create_scan(111, "https://www.twitch.tv/videos/555")
    store.update_scan(scan.id, status="done", title="Big stream", duration=3600.0, signals='["chat 10 msgs"]')
    moment = store.add_moment(scan.id, 1, 1800, 1710, 1845, 6.2, info={"chat_ratio": 8.0, "loud": True})
    store.save_timeline(scan.id, {"step": 5, "duration": 3600, "series": {"score": [0, 1, 6.2]}, "markers": []})

    data = dash.get(f"/api/scans/{scan.id}").json()
    assert data["title"] == "Big stream" and data["timeline"]["series"]["score"][2] == 6.2
    m = data["moments"][0]
    assert m["details"]["chat_ratio"] == 8.0 and m["link"] == "https://www.twitch.tv/videos/555?t=0h28m30s"
    assert m["job"] is None

    job = dash.post(f"/api/scans/{scan.id}/clip", json={"start": 1700, "end": "30:00", "clips": 2,
                                                       "moment_id": moment.id}).json()
    assert job["range"] == {"start": 1700.0, "end": 1800.0} and job["target_clips"] == 2
    assert dash.get(f"/api/scans/{scan.id}").json()["moments"][0]["job"]["id"] == job["id"]
    assert dash.post(f"/api/scans/{scan.id}/clip", json={"start": 1700, "end": 1800,
                                                        "moment_id": moment.id}).status_code == 409
    assert dash.post(f"/api/scans/{scan.id}/clip", json={"start": 3590, "end": 3700}).status_code == 422
    assert dash.post(f"/api/scans/{scan.id}/rescan").json()["status"] == "queued"
    assert dash.post("/api/scans", json={"url": "https://kick.com/xqc/videos/x"}).status_code == 201
    assert len(dash.get("/api/scans").json()["items"]) == 3


def test_logs_capture_and_filter(dash):
    login(dash)
    logging.getLogger("clipbot.pipeline").warning("job %s failed: boom", "j1", extra={"job_id": "j1"})
    logging.getLogger("clipbot.scout").info("scan s1 done", extra={"scan_id": "s1"})
    try:
        raise ValueError("bad value")
    except ValueError:
        logging.getLogger("clipbot.scout").exception("scan crashed")

    everything = dash.get("/api/logs").json()
    assert {"pipeline", "scout"} <= set(everything["sources"])
    warnings = dash.get("/api/logs", params={"level": "warning"}).json()["items"]
    assert [i["message"] for i in warnings] == ["scan crashed", "job j1 failed: boom"]
    assert "ValueError: bad value" in warnings[0]["detail"]
    assert dash.get("/api/logs", params={"job_id": "j1"}).json()["items"][0]["level"] == "WARNING"
    assert dash.get("/api/logs", params={"q": "s1 done"}).json()["items"][0]["scan_id"] == "s1"


def test_outbox_retry_and_delete(dash):
    login(dash)
    store = dash.app.state.store
    store.enqueue_event({"event": "message", "chat_id": 111, "text": "hello"})
    store.enqueue_event({"event": "message", "chat_id": 0, "text": "nobody to send to"})  # dropped
    event_id = store.outbox()[0]["id"]
    store.event_failed(event_id, 40, 0, dead=True)
    items = dash.get("/api/outbox").json()["items"]
    assert len(items) == 1 and items[0]["failed"] is True and items[0]["text"] == "hello"
    assert dash.post(f"/api/outbox/{event_id}/retry").json() == {"ok": True}
    assert dash.get("/api/outbox").json()["items"][0]["failed"] is False
    assert dash.delete(f"/api/outbox/{event_id}").json() == {"ok": True}
    assert dash.get("/api/outbox").json()["items"] == []
    assert dash.get("/api/n8n/failures").json() == {"configured": False, "items": []}


def test_static_files_and_client_routes(dash):
    assert "id=root" in dash.get("/").text
    assert "id=root" in dash.get("/scans/abc").text  # client-side route
    js = dash.get("/assets/app.js")
    assert js.text == "console.log(1)" and "immutable" in js.headers["cache-control"]
    assert dash.get("/api/nope").status_code == 404
    assert "id=root" in dash.get("/..%2F..%2Fetc%2Fpasswd").text  # never escapes web/
    assert dash.get("/health").json() == {"ok": True}
