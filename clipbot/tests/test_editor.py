"""The clip editor: the edit model and its time maps, the ffmpeg graph for cuts and zooms, real
re-renders with cuts / zooms / edited caption words, OpenShorts re-cuts (extend, reframe), and the API."""
import asyncio
import dataclasses
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import editing, render, templates
from app.main import create_app
from app.media import probe_duration
from app.openshorts import OpenShorts
from app.pipeline import Pipeline
from app.store import Store

OS_JOB = "0b5c7e2a-1f3d-4c6b-9a8e-123456789abc"
WORDS = [{"w": w, "s": round(i * 0.25, 2), "e": round(i * 0.25 + 0.2, 2)}
         for i, w in enumerate("one two three four five six seven eight nine ten eleven twelve".split())]
PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


# ---- the edit model -------------------------------------------------------------------------------
def test_normalize_edit():
    e = editing.normalize({"segments": [[2, 2.1], [1.5, 2.5], [0, 1], [0.9, 1.2], [2.8, 99], ["x"]],
                           "zooms": [{"s": 0.5, "e": 0.55}, {"s": 1, "e": 2, "scale": 9}, "nope"]}, 3.0)
    assert e == {"segments": [[0.0, 1.2], [1.5, 2.5]], "zooms": [{"s": 1.0, "e": 2.0, "scale": 2.0}]}
    assert editing.normalize(None, 3.0) == {"segments": [[0.0, 3.0]], "zooms": []}
    assert editing.normalize({"segments": [[0, 0.1]]}, 3.0)["segments"] == [[0.0, 3.0]]   # nothing usable: all
    assert not editing.is_cut(editing.full(3.0), 3.0) and editing.is_cut(e, 3.0)
    assert editing.output_duration(e) == 2.2


def test_time_maps():
    segs = [[0.0, 1.0], [2.0, 3.0]]
    assert [editing.map_time(t, segs) for t in (0.5, 1.5, 2.0, 2.5, 3.0, 3.5)] == [0.5, None, 1.0, 1.5, 2.0, None]
    assert editing.map_range(0.5, 2.5, segs) == [(0.5, 1.0), (1.0, 1.5)]
    words = [{"w": "a", "s": 0.2, "e": 0.4}, {"w": "b", "s": 0.9, "e": 1.4}, {"w": "c", "s": 1.5, "e": 1.8},
             {"w": "d", "s": 2.1, "e": 2.4}]
    assert editing.map_words(words, segs) == [{"w": "a", "s": 0.2, "e": 0.4}, {"w": "b", "s": 0.9, "e": 1.0},
                                              {"w": "d", "s": 1.1, "e": 1.4}]
    assert editing.map_points([{"t": 0.5, "sound": "x"}, {"t": 1.5}, {"t": 2.5}], segs) == [
        {"t": 0.5, "sound": "x"}, {"t": 1.5}]
    edit = {"segments": segs, "zooms": [{"s": 0.8, "e": 2.2, "scale": 1.2}]}
    assert editing.zoom_ranges(edit) == [(0.8, 1.0, 1.2), (1.0, 1.2, 1.2)]


def test_shift_after_extending():
    whole, sfx = editing.shift(None, [{"sound": "x", "t": 1.0}], by=2.0, old_duration=10, new_duration=14)
    assert whole["segments"] == [[0.0, 14.0]] and sfx == [{"sound": "x", "t": 3.0}]
    cut = {"segments": [[0, 3], [5, 10]], "zooms": [{"s": 1, "e": 2, "scale": 1.2}]}
    moved, _ = editing.shift(cut, [], by=2.0, old_duration=10, new_duration=14)
    assert moved == {"segments": [[2.0, 5.0], [7.0, 12.0]], "zooms": [{"s": 3.0, "e": 4.0, "scale": 1.2}]}


# ---- ffmpeg -------------------------------------------------------------------------------------
def test_cut_and_zoom_command(tmp_path):
    plan = render.Plan(duration=2.0, segments=[[0.0, 1.0], [2.0, 3.0]], zooms=[(0.2, 0.6, 1.3)], size=(540, 960))
    cmd = render.command(tmp_path / "in.mp4", tmp_path / "out.mp4", plan)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]trim=start=0.000:end=1.000,setpts=PTS-STARTPTS[v0]" in graph
    assert "[0:a]atrim=start=2.000:end=3.000,asetpts=PTS-STARTPTS[a1]" in graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcut][acut]" in graph
    assert "[vcut]scale=w='2*trunc(iw*(1+if(between(t,0.200,0.600),0.300*min(1,(t-0.200)/0.12),0))/2)'" in graph
    assert "crop=540:960" in graph and "[acut]volume=1.000[voice]" in graph
    assert cmd[cmd.index("-map") + 1] == "[v]" and cmd[cmd.index("-c:v") + 1] == "libx264"
    silent = render.command(tmp_path / "in.mp4", tmp_path / "out.mp4",
                            render.Plan(duration=2.0, segments=[[0.0, 1.0], [2.0, 3.0]], has_audio=False))
    assert "concat=n=2:v=1:a=0[vcut]" in silent[silent.index("-filter_complex") + 1]


# ---- real renders -----------------------------------------------------------------------------------
@pytest.fixture
def clip_env(settings, sample_video):
    store = Store(settings.db_path)
    store.seed_templates({k: templates.normalize(v) for k, v in templates.BUILT_IN.items()}, "standard")
    pipeline = Pipeline(settings, store, openshorts=None)
    job = store.create_job(111, "https://www.twitch.tv/videos/9", 100.0, 235.0, 1)
    store.update_job(job.id, status="done", openshorts_job=OS_JOB)
    shutil.copyfile(sample_video, settings.folder("clean") / f"{job.id}_0.mp4")
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", None, {"start": 10.0}, 3.0, str(master), None)
    store.update_clip(clip.id, words=json.dumps(WORDS), template="standard")
    yield settings, store, pipeline, store.get_clip(clip.id)
    run(pipeline.notify_client.aclose())
    store.close()


def test_rerender_with_cuts_zooms_and_words(clip_env):
    settings, store, pipeline, clip = clip_env
    words = [dict(w) for w in WORDS]
    words[0]["w"] = "ONE!"
    run(pipeline.rerender(clip.id, edit={"segments": [[0, 1], [2, 3]], "zooms": [{"s": 0.2, "e": 0.8}]},
                          words=words))
    clip = store.get_clip(clip.id)
    assert clip.render_error is None and clip.render_state is None
    assert json.loads(clip.edit)["segments"] == [[0.0, 1.0], [2.0, 3.0]]
    assert clip.word_list()[0]["w"] == "ONE!" and clip.meta["words_original"][0]["w"] == "one"
    assert clip.duration == 2.0 and abs(run(probe_duration(Path(clip.path))) - 2.0) < 0.15
    run(pipeline.rerender(clip.id))                                   # nothing given: the edit stays
    assert json.loads(store.get_clip(clip.id).edit)["segments"] == [[0.0, 1.0], [2.0, 3.0]]


class FakeOpenShorts:
    """The clip editor endpoints: edl, rerender, the new clip file, its transcript and layout."""

    def __init__(self, sample: Path, source_kept: bool = True):
        self.sample, self.source_kept, self.recuts, self.gone = sample, source_kept, [], False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if self.gone:
            return httpx.Response(404, json={"detail": "Job not found"})
        if path == f"/api/clip/{OS_JOB}/0/edl":
            return httpx.Response(200, json={"segments": [{"start": 600.0, "end": 603.0}], "framing": "auto",
                                             "canonical_range": {"start": 600.0, "end": 603.0},
                                             "source": {"available": self.source_kept, "duration": 900.0}})
        if path == "/api/clip/rerender":
            body = json.loads(request.content)
            self.recuts.append(body)
            if not self.source_kept:
                return httpx.Response(409, json={"detail": "The source video is no longer on the server"})
            return httpx.Response(200, json={"success": True, "new_video_url": f"/videos/{OS_JOB}/recut.mp4",
                                             "start": body["segments"][0]["start"], "end": body["segments"][-1]["end"],
                                             "render_path": "source"})
        if path == f"/videos/{OS_JOB}/recut.mp4":
            return httpx.Response(200, content=self.sample.read_bytes())
        if path == f"/api/clip/{OS_JOB}/0/transcript":
            return httpx.Response(200, json={"captions": [{"text": "fresh", "startMs": 100, "endMs": 400}]})
        if path == f"/api/status/{OS_JOB}":
            return httpx.Response(200, json={"status": "completed", "result": {"clips": [
                {"layout_ranges": [{"start": 0.5, "end": 1.5, "layout": "split"}]}]}})
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"no route {path}"})


def test_recut_extends_and_shifts(clip_env, sample_video):
    settings, store, pipeline, clip = clip_env
    fake = FakeOpenShorts(sample_video)
    pipeline.openshorts = OpenShorts("http://openshorts:8000", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    store.update_clip(clip.id, sfx=json.dumps([{"sound": "x", "t": 1.0}]), meta={**clip.meta, "moments": []})
    run(pipeline.recut(clip.id, extend_start=2.0, extend_end=1.0, framing="track"))
    (body,) = fake.recuts
    assert body["segments"] == [{"start": 598.0, "end": 604.0}] and body["framing"] == "track"
    assert body["reapply_captions"] is False
    clip = store.get_clip(clip.id)
    assert clip.render_state is None, clip.render_error
    assert clip.word_list() == [{"w": "fresh", "s": 0.1, "e": 0.4}] and clip.split_list() == [(0.5, 1.5)]
    assert clip.manual_sfx() == [] or clip.manual_sfx()[0]["t"] == 3.0     # moved by 2 s (dropped if past the end)
    assert clip.meta["start"] == 598.0 and "moments" not in clip.meta     # moments asked again on the new cut


def test_recut_reports_why_it_cannot(clip_env, sample_video):
    settings, store, pipeline, clip = clip_env
    fake = FakeOpenShorts(sample_video, source_kept=False)
    pipeline.openshorts = OpenShorts("http://openshorts:8000", httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    run(pipeline.recut(clip.id, extend_start=2.0))
    clip = store.get_clip(clip.id)
    assert clip.render_state == "failed" and "no longer on the server" in clip.render_error
    fake.gone = True
    run(pipeline.recut(clip.id, extend_start=2.0))
    assert "keeps them about a day" in store.get_clip(clip.id).render_error


# ---- API ------------------------------------------------------------------------------------
@pytest.fixture
def dash(settings, tmp_path, sample_video):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    fake = FakeOpenShorts(sample_video)
    client = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    with TestClient(create_app(cfg, openshorts=client, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        c.fake = fake
        yield c


def seed(dash, sample_video):
    store, settings = dash.app.state.store, dash.app.state.settings
    job = store.create_job(111, "https://youtu.be/x", None, None, 1)
    store.update_job(job.id, status="done", openshorts_job=OS_JOB)
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", None, {}, 3.0, str(master), None)
    store.update_clip(clip.id, words=json.dumps(WORDS))
    return store.get_clip(clip.id)


def wait(dash):
    for task in list(getattr(dash.app.state, "render_tasks", [])):
        dash.portal.call(asyncio.wait_for, asyncio.shield(task), 120)


def test_editor_api(dash, sample_video):
    clip = seed(dash, sample_video)
    assert dash.get(f"/api/clips/{clip.id}/editor").status_code == 409            # no copy without captions yet
    shutil.copyfile(sample_video, dash.app.state.settings.folder("clean") / f"{clip.job_id}_0.mp4")
    data = dash.get(f"/api/clips/{clip.id}/editor").json()
    assert 2.5 < data["source_duration"] < 3.5 and data["words"] == WORDS and data["words_original"] is None
    assert data["edit"]["segments"] == [[0.0, round(data["source_duration"], 3)]]
    assert data["openshorts"] == {"available": True, "source_kept": True, "framing": "auto", "reason": None}
    assert dash.get(data["source_url"]).content == sample_video.read_bytes()
    peaks = dash.get(data["peaks_url"]).json()
    assert peaks["per_second"] == 50 and 140 < len(peaks["peaks"]) < 160 and max(peaks["peaks"]) > 0
    assert dash.get(data["peaks_url"]).json() == peaks                          # cached

    assert dash.post(f"/api/clips/{clip.id}/edit", json={"template": "nope"}).status_code == 422
    r = dash.post(f"/api/clips/{clip.id}/edit", json={
        "edit": {"segments": [[0, 1], [2, 3]]}, "sfx": [{"sound": "boom.wav", "t": 0.5}],
        "words": [{"w": " One ", "s": 0, "e": 0.2}, {"w": "", "s": 0.25, "e": 0.45}]})
    assert r.status_code == 202 and r.json()["render_state"] == "rendering"
    assert dash.post(f"/api/clips/{clip.id}/edit", json={}).status_code == 409   # still rendering
    wait(dash)
    out = dash.get(f"/api/clips/{clip.id}/editor").json()
    assert out["words"] == [{"w": "One", "s": 0.0, "e": 0.2}] and out["sfx"] == [{"sound": "boom", "t": 0.5, "volume": 1.0}]
    assert out["clip"]["edited"] is True and out["clip"]["duration"] == 2.0

    assert dash.post(f"/api/clips/{clip.id}/recut", json={}).status_code == 422
    assert dash.post(f"/api/clips/{clip.id}/recut", json={"framing": "sideways"}).status_code == 422
    assert dash.post(f"/api/clips/{clip.id}/recut", json={"extend_end": 2}).status_code == 202
    wait(dash)
    assert dash.app.state.store.get_clip(clip.id).render_state is None
    assert dash.fake.recuts[-1]["segments"] == [{"start": 600.0, "end": 605.0}]
