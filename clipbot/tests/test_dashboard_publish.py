"""Dashboard API for the Storage and Publishing pages, and the Telegram routes for their buttons."""
import dataclasses
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient
from test_publisher import KEY, FakePostiz
from test_storage import OS_JOB, make_job

from app.main import create_app
from app.openshorts import OpenShorts
from app.postiz import Postiz
from app.publisher import Publisher

PASSWORD = "correct horse"
TOKEN = {"X-Clipbot-Token": "test-token"}


def openshorts_mock(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True}) if request.url.path == "/health" else httpx.Response(404)


def client_for(cfg, fake=None):
    openshorts = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(openshorts_mock)))

    def publisher_factory(settings, store):
        postiz = Postiz(settings.postiz_url, settings.postiz_api_key,
                        httpx.AsyncClient(transport=httpx.MockTransport(fake.handler if fake else openshorts_mock)))
        return Publisher(settings, store, postiz)

    app = create_app(cfg, openshorts=openshorts, run_background=False, publisher_factory=publisher_factory)
    return TestClient(app)


@pytest.fixture
def cfg(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")


def login(c):
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200


def approved(c, sample_video):
    store = c.app.state.store
    job = store.create_job(111, "https://www.twitch.tv/videos/9", 0.0, 300.0, 1)
    store.update_job(job.id, status="done")
    master = c.app.state.settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "Big play", "No way", {}, 3.0, str(master), None)
    return clip


# ---- storage ------------------------------------------------------------------------
def test_storage_page_and_deleting(cfg):
    with client_for(cfg) as c:
        login(c)
        store = c.app.state.store
        job = make_job(cfg, store)
        running = make_job(cfg, store, status="processing", os_job="1" * 8 + "-2222-4333-8444-555555555555")
        data = c.get("/api/storage").json()
        assert data["disk"]["total"] > 0 and data["openshorts"]["sources"] > 0
        rows = {row["id"]: row for row in data["jobs"]}
        assert rows[job.id]["files"]["freeable"] == 26 * 1024 * 1024 and not rows[job.id]["files"]["deleting"]
        assert c.post(f"/api/jobs/{running.id}/delete-files").status_code == 409
        assert c.get(f"/api/jobs/{job.id}").json()["files"]["openshorts"] == 26 * 1024 * 1024

        assert c.post(f"/api/jobs/{job.id}/delete-files").json() == {"freed": 26 * 1024 * 1024}
        assert c.get(f"/api/jobs/{job.id}").json()["files"]["deleting"] is True
        assert c.post("/api/storage/cleanup").json() == {"jobs": 0, "freed": 0}   # the running one stays
        assert c.get("/api/overview").json()["health"]["janitor_stuck"] is False


def test_telegram_cleanup_buttons_and_command(cfg):
    with client_for(cfg) as c:
        store = c.app.state.store
        job = make_job(cfg, store)
        r = c.post("/messages", json={"chat_id": 111, "text": "/cleanup"}, headers=TOKEN)
        assert r.json() == {"reply": None}
        assert store.outbox()[-1]["payload"]["buttons"][0]["data"] == "rm:all"
        r = c.post("/decisions", json={"chat_id": 111, "data": f"rm:{job.id}"}, headers=TOKEN)
        assert r.json()["toast"] == "Deleting"
        assert (cfg.data_dir / "purge" / OS_JOB).is_file()


# ---- publishing -----------------------------------------------------------------------
def test_publishing_off(cfg):
    with client_for(cfg) as c:
        login(c)
        data = c.get("/api/publishing").json()
        assert data["configured"] is False and data["slots"] == "12:00 / 17:00 / 21:00"
        assert c.post("/api/clips/nope/publish", json={}).status_code == 503
        assert c.get("/api/overview").json()["health"]["postiz"] is None


def test_publishing_page_and_actions(cfg, sample_video):
    fake = FakePostiz()
    cfg = dataclasses.replace(cfg, postiz_api_key=KEY, postiz_public_url="https://postiz.example.com")
    with client_for(cfg, fake) as c:
        login(c)
        clip = approved(c, sample_video)
        assert c.post(f"/api/clips/{clip.id}/publish", json={}).status_code == 409       # not approved yet
        c.post(f"/api/clips/{clip.id}/decision", json={"decision": "approve"})        # ✅ in the dashboard
        data = c.get("/api/publishing").json()
        assert data["configured"] and data["postiz_url"] == "https://postiz.example.com"
        assert [ch["platform"] for ch in data["channels"]] == ["youtube", "tiktok", "instagram", "youtube"]
        assert [q["clip"]["id"] for q in data["queue"]] == [clip.id] and data["unscheduled"] == []

        publisher = c.app.state.publisher
        c.portal.call(publisher.process_queue)
        data = c.get("/api/publishing").json()
        (group,) = data["upcoming"]
        assert group["clip"]["id"] == clip.id and group["status"] == "scheduled" and len(group["posts"]) == 3
        assert data["queue"] == [] and data["next_slot"] > group["publish_at"]
        assert c.get("/api/overview").json()["scheduled_posts"] == 3
        assert c.get("/api/overview").json()["health"]["postiz"] is True

        at = group["publish_at"] + 86400
        assert c.post(f"/api/clips/{clip.id}/publish", json={"at": at}).json() == {"queued": True}
        c.portal.call(publisher.process_queue)
        (group,) = c.get("/api/publishing").json()["upcoming"]
        assert abs(group["publish_at"] - at) < 1 and fake.uploads == 1

        assert c.post(f"/api/clips/{clip.id}/unschedule").json() == {"cancelled": 3}
        data = c.get("/api/publishing").json()
        assert data["upcoming"] == [] and [cl["id"] for cl in data["unscheduled"]] == [clip.id]
        assert data["recent"][0]["status"] == "cancelled"


def test_telegram_post_buttons(cfg, sample_video):
    fake = FakePostiz()
    cfg = dataclasses.replace(cfg, postiz_api_key=KEY)
    with client_for(cfg, fake) as c:
        clip = approved(c, sample_video)
        r = c.post("/decisions", json={"chat_id": 111, "data": f"ok:{clip.id}"}, headers=TOKEN)
        assert "Scheduling it" in r.json()["reply"]
        c.portal.call(c.app.state.publisher.process_queue)
        r = c.post("/decisions", json={"chat_id": 111, "data": f"pu:{clip.id}"}, headers=TOKEN)
        assert r.json()["toast"] == "Unscheduled"
