"""Publishing through Postiz: slots, captions, the create-post body, scheduling, reading results back,
retries, and the Telegram buttons. Postiz is faked with httpx.MockTransport (shapes from Postiz v2.25)."""
import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.pipeline import Pipeline
from app.postiz import Postiz, as_html, parse_iso
from app.publisher import (Publisher, build_post, clean_caption, next_free_slot, parse_slots,
                           youtube_title)
from app.store import Store

KEY = "postiz-test-key"
UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


class FakePostiz:
    def __init__(self):
        self.channels = [
            {"id": "yt1", "name": "My YT", "identifier": "youtube", "disabled": False},
            {"id": "tt1", "name": "my_tt", "identifier": "tiktok", "disabled": False},
            {"id": "ig1", "name": "my.ig", "identifier": "instagram-standalone", "disabled": False},
            {"id": "x1", "name": "on X", "identifier": "x", "disabled": False},          # not a video platform
            {"id": "yt2", "name": "Old YT", "identifier": "youtube", "disabled": True},
        ]
        self.uploads, self.created, self.deleted = 0, [], []
        self.posts: dict[str, dict] = {}       # what GET /posts returns
        self.down = 0                          # answer 503 this many times
        self.refuse = None                     # answer 400 with this message
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == KEY
        path = request.url.path.removeprefix("/api/public/v1")
        if self.down:
            self.down -= 1
            return httpx.Response(503, json={"msg": "Service Unavailable"})
        if path == "/is-connected":
            return httpx.Response(200, json={"connected": True})
        if path == "/integrations":
            return httpx.Response(200, json=self.channels)
        if path == "/upload":
            self.uploads += 1
            assert b'filename="' in request.content
            return httpx.Response(201, json={"id": f"m{self.uploads}", "name": "x.mp4",
                                             "path": f"https://postiz.example.com/uploads/m{self.uploads}.mp4"})
        if path == "/posts" and request.method == "POST":
            if self.refuse:
                return httpx.Response(400, json={"msg": self.refuse})
            body = json.loads(request.content)
            self.created.append(body)
            out = []
            for post in body["posts"]:
                self.n += 1
                pid = f"p{self.n}"
                self.posts[pid] = {"id": pid, "state": "QUEUE", "publishDate": body["date"], "releaseURL": None,
                                   "integration": {"id": post["integration"]["id"]}}
                out.append({"postId": pid, "integration": post["integration"]["id"]})
            return httpx.Response(200, json=out)
        if path == "/posts" and request.method == "GET":
            assert request.url.params["startDate"] and request.url.params["endDate"]
            return httpx.Response(200, json={"posts": list(self.posts.values())})
        if path.startswith("/posts/") and request.method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            self.deleted.append(pid)
            group = self.created_group(pid)
            for other in group:
                self.posts.pop(other, None)
            return httpx.Response(200, json={"error": False})
        return httpx.Response(404, json={"msg": f"no route {path}"})

    def created_group(self, pid):
        n = int(pid[1:])
        done = 0
        for body in self.created:
            ids = [f"p{done + i + 1}" for i in range(len(body["posts"]))]
            done += len(body["posts"])
            if n <= done:
                return ids
        return [pid]


@pytest.fixture
def env(settings, sample_video):
    cfg = dataclasses.replace(settings, postiz_api_key=KEY, postiz_url="http://postiz:5000/api",
                              postiz_public_url="https://postiz.example.com", timezone="UTC")
    fake = FakePostiz()
    store = Store(cfg.db_path)
    client = Postiz(cfg.postiz_url, KEY, httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    publisher = Publisher(cfg, store, client)
    yield cfg, store, publisher, fake, sample_video
    store.close()


def approved_clip(cfg, store, sample_video, title="He did WHAT", meta=None):
    job = store.create_job(111, "https://www.twitch.tv/videos/9", 0.0, 300.0, 1)
    store.update_job(job.id, status="done")
    path = cfg.folder("approved") / f"{job.id}_0.mp4"
    path.write_bytes(sample_video.read_bytes())
    clip = store.add_clip(job.id, 0, title, "No way 😳", meta if meta is not None else {
        "video_title_for_youtube_short": "He did WHAT on stream",
        "video_description_for_tiktok": "bro really did that #fyp",
        "video_description_for_instagram": "Wait for it 👀 Comment CLIP and I'll send you the full stream!"},
        3.0, str(path), None)
    store.decide_clip(clip.id, "approved", str(path))
    return store.get_clip(clip.id)


# ---- pure helpers ------------------------------------------------------------------
def test_parse_slots():
    assert parse_slots("9:30, 17:00 21 25:00 nonsense 17:00") == [(9, 30), (17, 0), (21, 0)]
    assert parse_slots("") == [(12, 0), (17, 0), (21, 0)]


def test_next_free_slot():
    slots = [(12, 0), (17, 0), (21, 0)]
    now = datetime(2026, 10, 18, 11, 55, tzinfo=UTC).timestamp()
    at = lambda h, d=18: datetime(2026, 10, d, h, 0, tzinfo=UTC).timestamp()  # noqa: E731
    assert next_free_slot(slots, UTC, now, set()) == at(17)          # 12:00 is only 5 minutes away
    assert next_free_slot(slots, UTC, now, {int(at(17) // 60)}) == at(21)
    late = datetime(2026, 10, 18, 22, 0, tzinfo=UTC).timestamp()
    assert next_free_slot(slots, UTC, late, set()) == at(12, 19)


def test_slots_follow_local_time_across_dst():
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 10, 24, 13, 0, tzinfo=berlin).timestamp()   # clocks go back on the 25th
    taken = {int(datetime(2026, 10, 24, h, 0, tzinfo=berlin).timestamp() // 60) for h in (17, 21)}
    ts = next_free_slot([(12, 0), (17, 0), (21, 0)], berlin, now, taken)
    local = datetime.fromtimestamp(ts, berlin)
    assert (local.day, local.hour, local.utcoffset().total_seconds()) == (25, 12, 3600)


def test_captions():
    assert clean_caption("Wait for it 👀 Comment CLIP and I'll send you the full stream!") == "Wait for it 👀"
    assert clean_caption("DM me to get it now. Great play.") == "Great play."
    assert as_html("line 1\n\nline <2>") == "<p>line 1</p><p></p><p>line &lt;2&gt;</p>"
    assert parse_iso("2026-10-18T17:00:00.000Z") == datetime(2026, 10, 18, 17, tzinfo=UTC).timestamp()


def test_youtube_title_limits(env):
    cfg, store, _, _, video = env
    long = approved_clip(cfg, store, video, meta={"video_title_for_youtube_short": "x" * 300})
    assert len(youtube_title(long)) == 100
    short = approved_clip(cfg, store, video, title="A", meta={})
    assert youtube_title(short) == "A clip"


def test_build_post(env):
    cfg, store, _, _, video = env
    clip = approved_clip(cfg, store, video)
    channels = [{"id": "yt1", "identifier": "youtube", "platform": "youtube"},
                {"id": "tt1", "identifier": "tiktok", "platform": "tiktok"},
                {"id": "ig1", "identifier": "instagram-standalone", "platform": "instagram"}]
    when = datetime(2026, 10, 18, 17, tzinfo=UTC).timestamp()
    body = build_post(cfg, clip, channels, {"id": "m1", "path": "https://p/u.mp4"}, when)
    assert (body["type"], body["date"], body["shortLink"], body["tags"]) == ("schedule", "2026-10-18T17:00:00.000Z", False, [])
    yt, tt, ig = body["posts"]
    assert yt["settings"] == {"__type": "youtube", "title": "He did WHAT on stream", "type": "public",
                              "selfDeclaredMadeForKids": "no", "tags": []}
    assert yt["value"][0]["content"] == "<p>No way 😳</p><p>#shorts</p>"
    assert yt["value"][0]["image"] == [{"id": "m1", "path": "https://p/u.mp4"}]
    assert tt["settings"]["__type"] == "tiktok" and tt["settings"]["content_posting_method"] == "UPLOAD"
    assert tt["value"][0]["content"] == "<p>bro really did that #fyp</p>"
    assert ig["settings"] == {"__type": "instagram-standalone", "post_type": "post", "collaborators": []}
    assert ig["value"][0]["content"] == "<p>Wait for it 👀</p>"          # the podcast call to action is gone
    assert build_post(cfg, clip, channels[:1], {"id": "m1", "path": "x"}, None)["type"] == "now"


# ---- scheduling ----------------------------------------------------------------------
def test_approved_clip_is_scheduled_on_every_video_channel(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    assert "next free slot (12:00 / 17:00 / 21:00)" in publisher.on_approved(clip, 111)
    run(publisher.process_queue())

    (body,) = fake.created
    assert [p["integration"]["id"] for p in body["posts"]] == ["yt1", "tt1", "ig1"]   # not X, not the disabled one
    when = parse_iso(body["date"])
    assert datetime.fromtimestamp(when, UTC).hour in (12, 17, 21) and when > time.time() + 600
    posts = store.posts_for_clip(clip.id)
    assert {p.platform for p in posts} == {"youtube", "tiktok", "instagram"}
    assert {p.status for p in posts} == {"scheduled"} and {p.publish_at for p in posts} == {when}
    assert store.get_publish(clip.id).status == "scheduled"
    (event,) = [e["payload"] for e in store.outbox()]
    assert event["event"] == "choice" and "is scheduled for" in event["text"]
    assert "TikTok (to your inbox)" in event["text"]
    assert [b["data"] for b in event["buttons"]] == [f"pn:{clip.id}", f"pu:{clip.id}"]


def test_each_clip_gets_its_own_slot(env):
    cfg, store, publisher, fake, video = env
    for _ in range(3):
        publisher.on_approved(approved_clip(cfg, store, video), 111)
    run(publisher.process_queue())
    dates = [parse_iso(b["date"]) for b in fake.created]
    assert len(set(dates)) == 3 and dates == sorted(dates)


def test_slots_taken_in_postiz_by_hand_are_skipped(env):
    cfg, store, publisher, fake, video = env
    first = next_free_slot(publisher.slots, publisher.tz, time.time(), set())
    fake.posts["manual"] = {"id": "manual", "state": "QUEUE", "integration": {"id": "yt1"},
                            "publishDate": datetime.fromtimestamp(first, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    publisher.on_approved(approved_clip(cfg, store, video), 111)
    run(publisher.process_queue())
    assert parse_iso(fake.created[0]["date"]) > first


def test_postiz_down_is_retried_then_reported(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    fake.down = 1
    run(publisher.process_queue())
    req = store.get_publish(clip.id)
    assert req.status == "waiting" and req.attempts == 1 and req.next_try > time.time() + 30
    assert store.outbox() == []
    fake.down = 100
    for _ in range(3):
        store.update_publish(clip.id, next_try=0)
        run(publisher.process_queue())
    req = store.get_publish(clip.id)
    assert req.status == "failed" and "503" in req.error
    assert "wasn't scheduled" in store.outbox()[-1]["payload"]["text"]


def test_a_refusal_is_not_retried(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    fake.refuse = "post is too long, please fix it"
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    req = store.get_publish(clip.id)
    assert req.status == "failed" and "too long" in req.error and req.attempts == 0


def test_no_channels_connected(env):
    cfg, store, publisher, fake, video = env
    fake.channels = [c for c in fake.channels if c["identifier"] == "x"]
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    assert "No YouTube, TikTok or Instagram channel" in store.get_publish(clip.id).error
    assert fake.uploads == 0


def test_publishing_off_without_a_key(settings):
    store = Store(settings.db_path)
    try:
        publisher = Publisher(settings, store)
        assert not publisher.enabled
        assert publisher.on_approved(None, 111) is None
        assert store.publish_requests() == []
    finally:
        store.close()


# ---- reading results back ------------------------------------------------------------
def test_sync_reports_published_and_failed_posts(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    yt, tt, ig = sorted(fake.posts.values(), key=lambda p: p["integration"]["id"] != "yt1")[:3]
    by_channel = {p["integration"]["id"]: p for p in fake.posts.values()}
    by_channel["yt1"].update(state="PUBLISHED", releaseURL="https://youtube.com/shorts/abc")
    by_channel["tt1"].update(state="PUBLISHED", releaseURL="https://www.tiktok.com/messages?lang=en")
    by_channel["ig1"].update(state="ERROR")
    run(publisher.sync())

    states = {p.platform: (p.status, p.url) for p in store.posts_for_clip(clip.id)}
    assert states["youtube"] == ("published", "https://youtube.com/shorts/abc")
    assert states["tiktok"][0] == "published" and states["instagram"][0] == "failed"
    texts = [e["payload"]["text"] for e in store.outbox()[1:]]
    assert any("live on YouTube: https://youtube.com/shorts/abc" in t and "Content Rewards" in t for t in texts)
    assert any("in your TikTok inbox" in t for t in texts)
    assert any("to Instagram failed" in t and "https://postiz.example.com" in t for t in texts)
    run(publisher.sync())                                   # nothing left to report
    assert len(store.outbox()) == 4


def test_sync_follows_posts_moved_or_deleted_in_postiz(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    by_channel = {p["integration"]["id"]: p for p in fake.posts.values()}
    later = time.time() + 5 * 86400
    by_channel["yt1"]["publishDate"] = datetime.fromtimestamp(later, UTC).isoformat()
    fake.posts.pop(by_channel["tt1"]["id"])
    run(publisher.sync())
    states = {p.platform: p for p in store.posts_for_clip(clip.id)}
    assert abs(states["youtube"].publish_at - later) < 1 and states["youtube"].status == "scheduled"
    assert states["tiktok"].status == "cancelled"


# ---- buttons ---------------------------------------------------------------------------
def test_unschedule_button(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    assert run(publisher.button(222, f"pu:{clip.id}")) == ("Unknown clip", None)
    toast, reply = run(publisher.button(111, f"pu:{clip.id}"))
    assert toast == "Unscheduled" and "Cancelled 3" in reply
    assert len(fake.deleted) == 1 and fake.posts == {}
    assert {p.status for p in store.posts_for_clip(clip.id)} == {"cancelled"}
    assert run(publisher.button(111, f"pu:{clip.id}"))[0] == "Not scheduled"


def test_post_now_button_reuses_the_upload(env):
    cfg, store, publisher, fake, video = env
    clip = approved_clip(cfg, store, video)
    publisher.on_approved(clip, 111)
    run(publisher.process_queue())
    assert run(publisher.button(111, f"pn:{clip.id}"))[0] == "Posting now"
    run(publisher.process_queue())
    assert fake.uploads == 1 and fake.created[-1]["type"] == "now"
    assert fake.created[-1]["posts"][0]["value"][0]["image"][0]["id"] == "m1"
    live = [p for p in store.posts_for_clip(clip.id) if p.status == "scheduled"]
    assert len(live) == 3 and all(p.publish_at <= time.time() for p in live)
    assert "Posting clip" in store.outbox()[-1]["payload"]["text"]


# ---- ✅ Post -----------------------------------------------------------------------------
def test_approving_in_telegram_queues_the_clip(env):
    cfg, store, publisher, fake, video = env
    job = store.create_job(111, "https://www.twitch.tv/videos/9", 0.0, 300.0, 1)
    store.update_job(job.id, status="done")
    master = cfg.folder("pending") / f"{job.id}_0.mp4"
    master.write_bytes(video.read_bytes())
    clip = store.add_clip(job.id, 0, "Title", None, {}, 3.0, str(master), None)
    pipeline = Pipeline(cfg, store, openshorts=None)
    pipeline.publisher = publisher
    toast, reply = pipeline.decide(111, f"ok:{clip.id}")
    assert toast == "Approved" and "🗓 Scheduling it for the next free slot" in reply
    assert store.get_publish(clip.id).status == "waiting"
    pipeline.decide(111, f"no:{clip.id}")                  # already decided: nothing more queued
    assert len(store.publish_requests()) == 1
    run(pipeline.notify_client.aclose())
