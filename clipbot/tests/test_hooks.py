"""The hook line: how it's burned (styles, position, timing, emoji), the clip's own settings over the
template's, AUTO_HOOK, real re-renders with new text, Gemini hook ideas, and the API."""
import asyncio
import dataclasses
import json
import shutil
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import captions, editing, media, render, templates
from app.main import create_app
from app.openshorts import OpenShorts
from app.pipeline import Pipeline, Rendered
from app.store import Store

WORDS = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.35} for i, w in enumerate("he really did that no way".split())]
PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


def hook_lines(doc):
    return [line for line in doc.splitlines() if "Hook" in line]


# ---- burning -------------------------------------------------------------------------------------
def test_hook_styles_and_positions():
    hook = {"text": "POV: he didn't see it coming 😳", "style": {}, "start": 0, "end": 5}
    style_line, event = hook_lines(captions.build_ass(WORDS, captions.DEFAULT_STYLE, hook=hook))
    assert style_line.startswith("Style: Hook,Montserrat ExtraBold,25,&H00000000") and ",0,0,3,7,0,8,28,28,38,1" in style_line
    assert event.startswith("Dialogue: 2,0:00:00.00,0:00:05.00,Hook") and event.endswith("POV: he didn't see it coming")
    outline = captions.build_ass([], {**captions.DEFAULT_STYLE, "enabled": False}, hook={
        **hook, "text": "two\nlines", "position": "bottom",
        "style": {"style": "outline", "text_color": "#FFFFFF", "uppercase": True}})
    style_line, event = hook_lines(outline)
    assert ",0,0,1,2,0,2,28,28,88,1" in style_line and event.endswith("TWO\\NLINES")
    assert not [line for line in outline.splitlines() if line.startswith("Dialogue: 0")]   # captions off
    plain = hook_lines(captions.build_ass([], captions.DEFAULT_STYLE, hook={**hook, "style": {"style": "plain"}}))
    assert ",1,0,2,8,28,28,38,1" in plain[0]
    assert captions.build_ass([], captions.DEFAULT_STYLE, hook={**hook, "text": "😳🔥"}) is None   # only emoji
    assert captions.normalize_hook({"style": "neon", "font_size": 500, "duration": -3, "position": "left"}) == {
        **captions.DEFAULT_HOOK, "font_size": 90, "duration": 0.0}


def frame(width, height, boxes):
    """A gray mask with white boxes (top, bottom, left, right), like hook_mask_ass renders."""
    data = bytearray(width * height)
    for top, bottom, left, right in boxes:
        for y in range(top, bottom):
            data[y * width + left:y * width + right] = b"\xff" * (right - left)
    return bytes(data)


def drawing_points(path):
    nums = [int(n) for n in path.replace("m", " ").replace("l", " ").replace("b", " ").split()]
    return list(zip(nums[::2], nums[1::2]))


def test_rounded_box_shape():
    w, h = 384, 288                                     # 1 px = 1 ASS unit, \p4 = 8 per px
    pill = captions.hook_box_shape(frame(w, h, [(20, 60, 100, 300)]), w, h, radius=1000)
    assert pill.startswith("m ") and pill.count(" b ") == 4
    xs, ys = zip(*drawing_points(pill))
    assert (min(xs), max(xs), min(ys), max(ys)) == (800, 2400, 160, 480)
    assert {(960, 160), (2240, 160), (960, 480), (2240, 480)} <= set(drawing_points(pill))   # ends: h/2 round
    square = captions.hook_box_shape(frame(w, h, [(20, 60, 100, 300)]), w, h, radius=0)
    assert (800, 160) in drawing_points(square)
    # A narrow line over a wide one, with a half-covered sliver row on top: one outline, 8 corners.
    lines = frame(w, h, [(19, 20, 180, 200), (20, 60, 140, 260), (52, 100, 100, 300)])
    stepped = captions.hook_box_shape(lines, w, h, radius=12)
    assert stepped.count("m ") == 1 and stepped.count(" b ") == 8
    xs, ys = zip(*drawing_points(stepped))
    assert (min(xs), max(xs), min(ys), max(ys)) == (800, 2400, 160, 800)
    assert captions.hook_box_shape(frame(w, h, []), w, h, 10) is None
    two = captions.hook_box_shape(frame(w, h, [(20, 60, 100, 300), (150, 190, 100, 300)]), w, h, 5)
    assert two.count("m ") == 2                          # a blank line between: two boxes


def test_rounded_box_in_the_ass():
    look = captions.normalize_hook({"box_radius": 60, "box_color": "#FFE500"})
    assert look["box_radius"] == 60 and captions.normalize_hook({"box_radius": 400})["box_radius"] == 100
    assert captions.hook_radius(look, 1920) == pytest.approx(0.6 * (25 + 2 * 7) / 2 * 1920 / 288)
    hook = {"text": "Wait for it", "style": look, "start": 1, "end": 4, "position": "top"}
    mask = hook_lines(captions.hook_mask_ass(hook))
    assert ",&HFF000000,&HFF000000,&H00FFFFFF,&H00FFFFFF," in mask[0] and ",3,7,0,8," in mask[0]   # box alone
    assert mask[1].startswith("Dialogue: 2,0:00:00.00,0:00:10.00,Hook") and "fad" not in mask[1]
    doc = captions.build_ass([], captions.DEFAULT_STYLE, hook={**hook, "shape": "m 0 0 l 8 0 8 8"})
    style_line, box, text = hook_lines(doc)
    assert ",0,0,1,0,0,8,28,28,38,1" in style_line                       # text alone, no square box
    assert box.startswith("Dialogue: 1,0:00:01.00,0:00:04.00,Hook") and box.endswith(
        "{\\an7\\pos(0,0)\\bord0\\shad0\\1c&H00E5FF&\\fad(120,150)\\p4}m 0 0 l 8 0 8 8{\\p0}")
    assert text.startswith("Dialogue: 2,0:00:01.00,0:00:04.00,Hook") and text.endswith("{\\fad(120,150)}Wait for it")


def test_rounded_box_render(settings, sample_video, monkeypatch, tmp_path):
    store = Store(settings.db_path)
    try:
        pipeline = Pipeline(settings, store, openshorts=None)
        look = captions.normalize_hook({"box_radius": 100})
        hook = {"text": "Wait for the ending", "style": look, "start": 0, "end": 3, "position": "top"}
        done = Rendered("standard", {})
        plan = render.Plan(duration=3.0)
        shape = run(pipeline.rounded_hook_box(hook, sample_video, tmp_path, plan, done))   # real libass
        assert shape.startswith("m ") and " b " in shape and not done.notes

        async def broken(*args):
            raise media.MediaError("ffmpeg failed: no")

        monkeypatch.setattr(render, "ass_mask", broken)
        assert run(pipeline.rounded_hook_box(hook, sample_video, tmp_path, plan, done)) is None
        assert done.notes == ["the hook's box has square corners this time (ffmpeg failed: no)"]
        run(pipeline.notify_client.aclose())
    finally:
        store.close()


def test_hook_window():
    plain = editing.normalize(None, 10.0)
    assert editing.hook_window(plain, 5.0, 10.0) == (0.0, 5.0)
    assert editing.hook_window(plain, 0.0, 10.0) == (0.0, 10.0)            # 0 = whole clip
    assert editing.hook_window(plain, 12.0, 10.0) == (0.0, 10.0)
    cut = editing.normalize({"segments": [[0, 2], [4, 10]], "hook": {"s": 1, "e": 5}}, 10.0)
    assert editing.hook_window(cut, 5.0, 8.0) == (1.0, 3.0)               # 1-2 then 4-5, back to back
    whole = editing.normalize({"hook": {"s": 2, "e": None, "position": "middle", "enabled": True}}, 10.0)
    assert whole["hook"] == {"enabled": True, "s": 2.0, "e": None, "position": "middle"}
    assert editing.hook_window(whole, 5.0, 10.0) == (2.0, 10.0)
    assert editing.normalize({"hook": {"s": 50, "e": 1}}, 10.0)["hook"] == {"s": 9.7, "e": 10.0}
    moved, _ = editing.shift(whole, [], 1.5, 10.0, 11.5)
    assert moved["hook"]["s"] == 3.5 and moved["hook"]["e"] is None


def test_hook_plan_rules(settings):
    store = Store(settings.db_path)
    try:
        pipeline = Pipeline(settings, store, openshorts=None)
        template = templates.normalize(templates.BUILT_IN["meme"])
        plain = editing.full(10.0)
        plan = pipeline.hook_plan(template, plain, "He did WHAT 😳", 10.0)
        assert plan["text"] == "He did WHAT" and (plan["start"], plan["end"]) == (0.0, 5.0)
        assert plan["style"]["style"] == "outline" and plan["position"] == "top"
        assert pipeline.hook_plan(template, plain, "", 10.0) is None
        assert pipeline.hook_plan(template, {**plain, "hook": {"enabled": False}}, "x", 10.0) is None
        moved = pipeline.hook_plan(template, {**plain, "hook": {"position": "bottom", "s": 1.0, "e": 3.0}}, "x", 10.0)
        assert (moved["start"], moved["end"], moved["position"]) == (1.0, 3.0, "bottom")
        off = dataclasses.replace(settings, auto_hook=False)
        pipeline.settings = off
        assert pipeline.hook_plan(template, plain, "x", 10.0) is None              # AUTO_HOOK=false...
        assert pipeline.hook_plan(template, {**plain, "hook": {"enabled": True}}, "x", 10.0)  # ...unless asked
        run(pipeline.notify_client.aclose())
    finally:
        store.close()


def test_rerender_with_new_hook(settings, sample_video):
    store = Store(settings.db_path)
    store.seed_templates({k: templates.normalize(v) for k, v in templates.BUILT_IN.items()}, "standard")
    pipeline = Pipeline(settings, store, openshorts=None)
    job = store.create_job(111, "https://youtu.be/x", None, None, 1)
    store.update_job(job.id, status="done")
    shutil.copyfile(sample_video, settings.folder("clean") / f"{job.id}_0.mp4")
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", "Old hook 😳", {"viral_hook_text": "Old hook 😳", "hook_rendered": "Old hook"},
                          3.0, str(master), None)
    legacy = store.add_clip(job.id, 1, "T", "Burned by OpenShorts", {}, 3.0, str(master), None)
    shutil.copyfile(sample_video, settings.folder("clean") / f"{job.id}_1.mp4")
    try:
        run(pipeline.rerender(clip.id, hook_text="Wait for the end 👀"))
        clip = store.get_clip(clip.id)
        assert clip.render_error is None and clip.hook == "Wait for the end 👀"
        assert clip.meta["viral_hook_text"] == "Wait for the end 👀" and clip.meta["hook_rendered"] == "Wait for the end"
        assert Path(clip.path).read_bytes() != sample_video.read_bytes()
        run(pipeline.rerender(clip.id, edit={"hook": {"enabled": False}}))
        assert store.get_clip(clip.id).meta["hook_rendered"] == ""
        run(pipeline.rerender(legacy.id))                        # its clean copy has OpenShorts' hook already
        legacy = store.get_clip(legacy.id)
        assert legacy.render_error is None and "hook_rendered" not in legacy.meta
        run(pipeline.notify_client.aclose())
    finally:
        store.close()


def test_old_built_ins_get_their_hook(settings):
    store = Store(settings.db_path)
    try:
        old = {k: {key: v for key, v in templates.normalize(t).items() if key != "hook"} for k, t in templates.BUILT_IN.items()}
        store.seed_templates(old, "standard")
        store.save_template("mine", {k: v for k, v in old["meme"].items()})
        store.seed_templates({k: templates.normalize(v) for k, v in templates.BUILT_IN.items()}, "standard")
        assert store.get_template("meme")["hook"]["style"] == "outline"
        assert store.get_template("hype")["hook"]["box_color"] == "#A3FF12"
        assert "hook" not in store.get_template("mine")                  # yours: the default look
        assert templates.normalize(store.get_template("mine"))["hook"] == captions.DEFAULT_HOOK
    finally:
        store.close()


# ---- Gemini ideas ------------------------------------------------------------------------------------
def test_hook_ideas(settings):
    seen = {}

    def gemini(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps({"hooks": [
            '"He really did that 😳"', "POV: chat called it", "he really did that", "", "Wait for it"]})}]}}]})

    cfg = dataclasses.replace(settings, gemini_api_key="g")
    client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
    hooks = run(templates.suggest_hooks(cfg, WORDS, "Big play", "Old hook", client=client))
    assert hooks == ["He really did that", "POV: chat called it", "Wait for it"]
    prompt = seen["body"]["contents"][0]["parts"][0]["text"]
    assert "Clip title: Big play" in prompt and "needs replacing: Old hook" in prompt and "he really did" in prompt
    broken = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert run(templates.suggest_hooks(cfg, WORDS, client=broken)) is None
    assert run(templates.suggest_hooks(settings, WORDS)) is None


# ---- API -----------------------------------------------------------------------------------------
@pytest.fixture
def dash(settings, tmp_path, sample_video, monkeypatch):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    client = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with TestClient(create_app(cfg, openshorts=client, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        store, settings = c.app.state.store, c.app.state.settings
        job = store.create_job(111, "https://youtu.be/x", None, None, 1)
        store.update_job(job.id, status="done")
        shutil.copyfile(sample_video, settings.folder("clean") / f"{job.id}_0.mp4")
        master = settings.folder("pending") / f"{job.id}_0.mp4"
        shutil.copyfile(sample_video, master)
        c.clip = store.add_clip(job.id, 0, "T", "Old hook", {"hook_rendered": "Old hook"}, 3.0, str(master), None)
        c.legacy = store.add_clip(job.id, 1, "T", "Burned", {}, 3.0, str(master), None)
        yield c


def test_hook_api(dash, monkeypatch):
    clip = dash.clip
    assert dash.post(f"/api/clips/{clip.id}/hook-suggestions").status_code == 503      # no Gemini key
    dash.app.state.settings = dataclasses.replace(dash.app.state.settings, gemini_api_key="g")

    async def ideas(settings, words, title=None, current=None, n=5, client=None, **context):
        assert current == "Old hook"
        return ["One", "Two"]

    monkeypatch.setattr(templates, "suggest_hooks", ideas)
    assert dash.post(f"/api/clips/{clip.id}/hook-suggestions").json() == {"suggestions": ["One", "Two"]}

    t = dash.get("/api/templates").json()
    assert t["hook_styles"] == ["box", "outline", "plain"] and t["auto_hook"] is True
    assert t["hook_default"]["style"] == "box"
    meme = next(x for x in t["items"] if x["id"] == "meme")
    assert meme["hook"]["style"] == "outline" and meme["hook"]["uppercase"] is True
    meme["hook"]["duration"] = 2.5
    assert dash.put("/api/templates/meme", json=meme).json()["hook"]["duration"] == 2.5

    assert dash.post(f"/api/clips/{dash.legacy.id}/edit", json={"hook_text": "x"}).status_code == 409
    r = dash.post(f"/api/clips/{clip.id}/edit", json={"hook_text": "Brand new hook", "edit": {"hook": {"position": "middle"}}})
    assert r.status_code == 202
    for task in list(dash.app.state.render_tasks):
        dash.portal.call(asyncio.wait_for, asyncio.shield(task), 120)
    out = dash.get(f"/api/clips/{clip.id}/editor").json()
    assert out["clip"]["hook"] == "Brand new hook" and out["clip"]["hook_rendered"] == "Brand new hook"
    assert out["edit"]["hook"] == {"position": "middle"} and out["clip"]["hook_on"] is True

    r = dash.post(f"/api/clips/{clip.id}/edit", json={"edit": {"hook": {"enabled": False}}})
    for task in list(dash.app.state.render_tasks):
        dash.portal.call(asyncio.wait_for, asyncio.shield(task), 120)
    out = dash.get(f"/api/clips/{clip.id}/editor").json()["clip"]
    assert out["hook_on"] is False and out["hook_rendered"] == "" and out["hook"] == "Brand new hook"
