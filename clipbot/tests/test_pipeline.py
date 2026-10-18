"""Pipeline tests against a fake OpenShorts and a fake n8n (httpx MockTransport), real ffmpeg."""
import asyncio
import json
import shutil

import httpx
import pytest

from app import media
from app.openshorts import OpenShorts
from app.pipeline import Pipeline
from app.store import Store

OS_JOB = "os-job-1"


class FakeOpenShorts:
    def __init__(self, output_dir, sample, write_files=True):
        self.output_dir, self.sample, self.write_files = output_dir, sample, write_files
        self.process_bodies, self.uploaded_bytes = [], 0
        self.status_value = "completed"
        self.refuse = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if method == "POST" and path == "/api/process":
            if self.refuse:
                return httpx.Response(400, json={"detail": self.refuse})
            self.process_bodies.append(json.loads(request.content))
            if self.write_files:
                job_dir = self.output_dir / OS_JOB
                job_dir.mkdir(parents=True, exist_ok=True)
                for name in ("clip_0.mp4", "clip_1.mp4"):
                    shutil.copyfile(self.sample, job_dir / name)
            return httpx.Response(200, json={"job_id": OS_JOB, "status": "queued"})
        if method == "POST" and path == "/api/uploads":
            return httpx.Response(200, json={"upload_id": "up1", "upload_url": "http://public/x"})
        if method == "PUT" and path == "/api/uploads/up1":
            self.uploaded_bytes = len(request.content)
            return httpx.Response(200, json={"upload_id": "up1"})
        if method == "GET" and path == f"/api/status/{OS_JOB}":
            return httpx.Response(200, json={
                "status": self.status_value, "logs": ["step one", "boom"],
                "result": {"clips": [
                    {"video_url": f"/videos/{OS_JOB}/clip_0.mp4",
                     "video_title_for_youtube_short": "Funny moment",
                     "viral_hook_text": "He did WHAT 😳", "start": 12.0, "end": 40.0},
                    {"video_url": f"/videos/{OS_JOB}/clip_1.mp4", "title": "Second one"},
                ]} if self.status_value == "completed" else None})
        if method == "GET" and path.startswith(f"/videos/{OS_JOB}/"):
            return httpx.Response(200, content=self.sample.read_bytes())
        return httpx.Response(404, json={"detail": "not found"})


class FakeN8n:
    def __init__(self):
        self.events, self.tokens, self.fail_next = [], [], 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503)
        self.events.append(json.loads(request.content))
        self.tokens.append(request.headers.get("X-Clipbot-Token"))
        return httpx.Response(200, json={"message": "Workflow was started"})


@pytest.fixture
def env(settings, sample_video):
    fake_os = FakeOpenShorts(settings.openshorts_output_dir, sample_video)
    fake_n8n = FakeN8n()
    store = Store(settings.db_path)
    client = OpenShorts(settings.openshorts_url,
                        httpx.AsyncClient(transport=httpx.MockTransport(fake_os.handler)))
    pipeline = Pipeline(settings, store, client,
                        httpx.AsyncClient(transport=httpx.MockTransport(fake_n8n.handler)))
    yield pipeline, store, fake_os, fake_n8n
    store.close()


def run(coro):
    return asyncio.run(coro)


def test_url_job_end_to_end(env, settings):
    pipeline, store, fake_os, fake_n8n = env
    job = store.create_job(111, "https://youtu.be/abc", None, None, 3)

    run(pipeline.start_job(job))
    job = store.get_job(job.id)
    assert job.status == "processing" and job.openshorts_job == OS_JOB
    body = fake_os.process_bodies[0]
    assert body["acknowledged"] is True and body["url"] == "https://youtu.be/abc"
    assert body["layouts"] == ["auto"] and body["target_clips"] == 3 and body["auto_hook"] is True
    assert "captions" not in body  # OpenShorts burns captions unless told captions=false

    run(pipeline.poll_job(job))
    assert store.get_job(job.id).status == "done"
    clips = store.clips_for_job(job.id)
    assert [c.title for c in clips] == ["Funny moment", "Second one"]
    for clip in clips:
        assert (settings.data_dir / "pending").joinpath(f"{job.id}_{clip.idx}.mp4").is_file()
        preview = settings.data_dir / "previews" / f"{job.id}_{clip.idx}.mp4"
        assert preview.is_file() and preview.stat().st_size < settings.preview_max_mb * 1024 * 1024
        assert 2.5 < clip.duration < 3.5

    run(pipeline.flush_outbox())
    kinds = [e["event"] for e in fake_n8n.events]
    assert kinds == ["message", "clip_ready", "clip_ready"]
    assert "2 clip(s)" in fake_n8n.events[0]["text"]
    first = fake_n8n.events[1]
    assert first["chat_id"] == 111 and first["approve_data"] == f"ok:{clips[0].id}"
    assert first["preview_url"] == f"http://clipbot:8080/previews/{clips[0].id}"
    assert "Funny moment" in first["caption"] and "🪝 He did WHAT" in first["caption"]
    assert set(fake_n8n.tokens) == {"test-token"}
    assert store.due_events() == []


def test_decisions_move_files(env, settings):
    pipeline, store, _, _ = env
    job = store.create_job(111, "https://youtu.be/abc", None, None, 3)
    run(pipeline.start_job(job))
    run(pipeline.poll_job(store.get_job(job.id)))
    first, second = store.clips_for_job(job.id)

    toast, reply = pipeline.decide(111, f"ok:{first.id}")
    assert toast == "Approved" and reply.startswith("✅ Approved → approved/")
    approved = list((settings.data_dir / "approved").glob("*.mp4"))
    assert len(approved) == 1 and approved[0].name.endswith(f"_funny-moment_{first.id}.mp4")
    sidecar = json.loads(approved[0].with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["viral_hook_text"] == "He did WHAT 😳" and sidecar["source_url"] == "https://youtu.be/abc"
    assert not (settings.data_dir / "previews" / f"{job.id}_0.mp4").exists()

    assert pipeline.decide(111, f"ok:{first.id}") == ("Already approved", None)
    assert pipeline.decide(222, f"no:{second.id}") == ("Unknown clip", None)
    assert pipeline.decide(111, "garbage") == ("Unknown clip", None)

    toast, _ = pipeline.decide(111, f"no:{second.id}")
    assert toast == "Rejected"
    assert (settings.data_dir / "rejected" / f"{second.id}.mp4").is_file()


def test_range_job_uploads_only_the_section(env, settings, sample_video, monkeypatch):
    pipeline, store, fake_os, _ = env
    calls = {}

    async def fake_download(url, start, end, out_dir, cookies=None):
        calls.update(url=url, start=start, end=end)
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "source.mp4"
        shutil.copyfile(sample_video, dst)
        return dst

    monkeypatch.setattr(media, "download_section", fake_download)
    job = store.create_job(111, "https://www.twitch.tv/videos/1", 3720.0, 4320.0, 2)
    run(pipeline.start_job(job))

    assert calls == {"url": "https://www.twitch.tv/videos/1", "start": 3720.0, "end": 4320.0}
    assert fake_os.uploaded_bytes == sample_video.stat().st_size
    body = fake_os.process_bodies[0]
    assert body["upload_id"] == "up1" and "url" not in body and body["target_clips"] == 2
    assert not (settings.data_dir / "tmp" / job.id).exists()
    assert store.get_job(job.id).status == "processing"


def test_clips_fetched_over_http_when_not_on_shared_volume(env, settings, sample_video):
    pipeline, store, fake_os, _ = env
    fake_os.write_files = False
    job = store.create_job(111, "https://youtu.be/abc", None, None, 3)
    run(pipeline.start_job(job))
    run(pipeline.poll_job(store.get_job(job.id)))
    assert len(store.clips_for_job(job.id)) == 2


def test_openshorts_failure_is_reported(env):
    pipeline, store, fake_os, fake_n8n = env
    fake_os.status_value = "failed"
    job = store.create_job(111, "https://youtu.be/abc", None, None, 3)
    run(pipeline.start_job(job))
    run(pipeline.poll_job(store.get_job(job.id)))
    job = store.get_job(job.id)
    assert job.status == "failed" and "boom" in job.error
    run(pipeline.flush_outbox())
    assert fake_n8n.events[0]["text"].startswith(f"❌ Job {job.id} failed")


def test_openshorts_refusal_is_reported(env):
    pipeline, store, fake_os, _ = env
    fake_os.refuse = "You must confirm you own the content"
    job = store.create_job(111, "https://youtu.be/abc", None, None, 3)
    run(pipeline.start_job(job))
    job = store.get_job(job.id)
    assert job.status == "failed" and "You must confirm" in job.error


def test_outbox_waits_while_n8n_is_down(env):
    pipeline, store, _, fake_n8n = env
    store.enqueue_event({"event": "message", "chat_id": 111, "text": "first"})
    store.enqueue_event({"event": "message", "chat_id": 111, "text": "second"})
    fake_n8n.fail_next = 1

    run(pipeline.flush_outbox())
    assert fake_n8n.events == []  # first failed, second must not jump the queue
    assert store.due_events() == []  # first is now scheduled for a later retry
