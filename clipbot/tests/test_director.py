"""The Director: what Gemini is told, how its clips come back (clock, clamping, lengths, repeats),
batching under the token budget, the File API round trip, and the free tier's 429s."""
import asyncio
import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from app import director, gemini
from app.director import Context, Pick, Window
from app.store import Store


def run(coro):
    return asyncio.run(coro)


def window(tmp_path, wid="w1", start=100.0, end=280.0, **kw):
    path = tmp_path / f"{wid}.mp4"
    path.write_bytes(b"\x00" * 64)
    return Window(wid, path, start, end, **kw)


def answer(clips):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"clips": clips})}]}}]}


def clip(video="w1", start=10, end=40, score=80, **kw):
    return {"video": video, "start": start, "end": end, "score": score, "category": "chaos",
            "what_happened": "Kevin throws the monitor", "why": "it actually breaks", "hook": "kevin did it again",
            "title": "Kevin destroys the monitor", "tiktok": "rip monitor #xqc", "instagram": "rip #xqc", **kw}


# ---- prompt -----------------------------------------------------------------------------------
def test_prompt_carries_the_context(tmp_path):
    w = window(tmp_path, chat=[(110.0, "KEKW", 14), (112.0, "he threw it", 1)], clip_titles=["KEVIN NOOO"],
               signals="chat 8× its normal, loud")
    ctx = Context(streamer="xqc (Twitch)", title="GTA day 3", notes="Kevin = his roommate", note="the monitor smash",
                  examples=[{"hook": "kevin did it again", "title": "Kevin breaks it", "ai_hook": "POV: chaos 😳"}],
                  skipped=["he reads donations"], want=3, min_len=12, max_len=75)
    prompt = director.build_prompt([w], ctx)
    for needle in ('stream title: "GTA day 3"', "look for: the monitor smash", "Between 12 and 75 seconds",
                   "Return the 3 best clips", 'hook "kevin did it again"', 'AI wrote hook "POV: chaos 😳"',
                   "- he reads donations", "Kevin = his roommate", "Never write \"POV:\""):
        assert needle in prompt, needle
    text = director.window_text(w)
    assert '"KEVIN NOOO"' in text and "10 KEKW ×14" in text and "12 he threw it" in text   # the video's own clock
    assert "Return every real moment" in director.build_prompt([w], Context())
    hinted = director.build_prompt([w], Context(hints=[Pick(130, 160, 80, "chaos", "Kevin throws it", "", "", "", "", "")]))
    assert "video w1, around 30-60 s: Kevin throws it" in hinted and "one clip per moment" in hinted


def test_batches_stay_under_the_budget(tmp_path):
    ws = [window(tmp_path, f"w{i}", 0, 200) for i in range(6)]          # 20k tokens each
    assert [len(b) for b in director.batches(ws, 60_000)] == [3, 3]
    assert [len(b) for b in director.batches(ws, 1_000_000)] == [4, 2]  # at most 4 videos a request
    assert [len(b) for b in director.batches([window(tmp_path, "big", 0, 3000)], 60_000)] == [1]


def test_picks_come_back_on_the_windows_clock(tmp_path):
    a, b = window(tmp_path, "w1", 100, 280), window(tmp_path, "w2", 1000, 1060)
    ctx = Context(min_len=15, max_len=60)
    picks = director.parse_picks({"clips": [
        clip("w1", 10, 40), clip("w1", 170, 200),           # runs past the end: clamped, then grown back
        clip("w2", 5, 9), clip("w2", 0, 59.9, score=150),   # too short → 15 s; score capped
        clip("w1", 0, 120), clip("nope", 1, 20), clip("w1", "x", 3),
    ]}, [a, b], ctx)
    assert [(p.window, p.start, p.end) for p in picks] == [
        ("w1", 110.0, 140.0), ("w1", 265.0, 280.0), ("w2", 1005.0, 1020.0), ("w2", 1000.0, 1059.9), ("w1", 100.0, 160.0)]
    assert picks[3].score == 100 and picks[0].category == "chaos"


def test_the_same_moment_twice_keeps_the_better(tmp_path):
    p = lambda s, e, score: Pick(s, e, score, "funny", "", "", "", "", "", "")
    kept = director.dedupe([p(10, 40, 60), p(15, 45, 90), p(41, 70, 50), p(100, 130, 70)])
    assert [(k.start, k.score) for k in kept] == [(15, 90), (100, 70), (41, 50)]


def test_learned_from_decisions(settings, tmp_path):
    store = Store(settings.db_path)
    try:
        job = store.create_job(111, "https://twitch.tv/videos/1", 0, 600, 3)
        def made(status, streamer, **meta):
            c = store.add_clip(job.id, 0, meta.pop("title", "T"), meta.pop("hook", "h"),
                               {"streamer": streamer, **meta}, 30.0, "/x.mp4", None)
            store.decide_clip(c.id, status, "/x.mp4")
        made("approved", "other", hook="theirs", title="Other")
        made("approved", "xqc", hook="mine", title="Mine", ai_hook="POV: wow")
        made("rejected", "xqc", what_happened="he reads donations")
        made("rejected", "xqc")
        examples, skipped = director.learned(store, "xqc")
        assert [e["hook"] for e in examples] == ["mine", "theirs"] and examples[0]["ai_hook"] == "POV: wow"
        assert skipped == ["he reads donations"]
    finally:
        store.close()


# ---- the round trip ---------------------------------------------------------------------------
class FakeGemini:
    """Upload (resumable, then PROCESSING → ACTIVE), generateContent, delete."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        self.deleted = []
        self.polled = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert request.headers.get("x-goog-api-key") == "k" or "upload-session" in url
        if url.endswith("/upload/v1beta/files"):
            return httpx.Response(200, headers={"x-goog-upload-url": "https://g.test/upload-session/1"})
        if "upload-session" in url:
            return httpx.Response(200, json={"file": {"name": f"files/f{len(self.requests)}", "uri": "https://g.test/f",
                                                      "mimeType": "video/mp4", "state": "PROCESSING"}})
        if request.method == "GET":
            self.polled += 1
            return httpx.Response(200, json={"name": url.rsplit("/v1beta/", 1)[1], "uri": "https://g.test/f",
                                             "mimeType": "video/mp4", "state": "ACTIVE"})
        if request.method == "DELETE":
            self.deleted.append(url.rsplit("/v1beta/", 1)[1])
            return httpx.Response(200, json={})
        self.requests.append(json.loads(request.content))
        return self.answers.pop(0)


def test_direct_watches_and_cleans_up(settings, tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(gemini.asyncio, "sleep", lambda *_: real_sleep(0))
    cfg = dataclasses.replace(settings, gemini_api_key="k", gemini_api_url="https://g.test",
                              director_model="gemini-test", director_batch_tokens=30_000)
    fake = FakeGemini([httpx.Response(200, json=answer([clip("w1", 10, 40, 70)])),
                       httpx.Response(200, json=answer([clip("w2", 0, 30, 95), clip("w2", 2, 31, 60)]))])
    ws = [window(tmp_path, "w1", 100, 280), window(tmp_path, "w2", 500, 700)]
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    picks = run(director.direct(cfg, ws, Context(), client=client))
    assert [(p.window, p.start, p.score) for p in picks] == [("w2", 500.0, 95), ("w1", 110.0, 70)]
    assert len(fake.requests) == 2 and fake.polled == 2 and len(fake.deleted) == 2       # one window each
    body = fake.requests[0]
    parts = body["contents"][0]["parts"]
    assert "edit a clip channel" in parts[0]["text"] and parts[1]["text"].startswith("Video w1")
    assert parts[2] == {"fileData": {"mimeType": "video/mp4", "fileUri": "https://g.test/f"}}
    assert body["generationConfig"]["mediaResolution"] == "MEDIA_RESOLUTION_LOW"
    assert body["generationConfig"]["responseSchema"]["properties"]["clips"]["type"] == "ARRAY"


def test_free_tier_429s(settings, tmp_path, monkeypatch):
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(gemini.asyncio, "sleep", sleep)
    cfg = dataclasses.replace(settings, gemini_api_key="k", gemini_api_url="https://g.test", director_model="m2")
    minute = {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}]}}
    daily = {"error": {"code": 429, "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}
    fake = FakeGemini([httpx.Response(429, json=minute), httpx.Response(200, json=answer([clip()]))])
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    assert len(run(director.direct(cfg, [window(tmp_path)], Context(), client=client))) == 1
    assert 7.0 in waits

    fake = FakeGemini([httpx.Response(429, json=daily)])
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    with pytest.raises(gemini.QuotaExhausted, match="for today"):
        run(director.direct(cfg, [window(tmp_path)], Context(), client=client))
    assert fake.deleted                                        # the upload is cleaned up anyway

    with pytest.raises(gemini.GeminiError, match="GEMINI_API_KEY"):
        run(director.direct(settings, [window(tmp_path)], Context()))


def test_budget_waits_for_the_minute(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(gemini.time, "monotonic", lambda: clock[0])

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(gemini.asyncio, "sleep", sleep)
    budget = gemini._Budget()

    async def go():
        await budget.take(150_000, 200_000)
        await budget.take(40_000, 200_000)          # fits
        await budget.take(100_000, 200_000)         # waits for the first to age out
        return clock[0]

    assert run(go()) == 60.0
