"""Templates: caption ASS, where sound effects land, Gemini punchlines, the ffmpeg command, real renders
(captions + music + effects), fonts, the dashboard API and the Telegram side."""
import asyncio
import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import captions, fonts, render, templates
from app.main import create_app, handle_message
from app.openshorts import OpenShorts
from app.parsing import parse_message
from app.pipeline import Pipeline
from app.publisher import caption_for
from app.store import Store

WORDS = [{"w": w, "s": round(i * 0.4, 2), "e": round(i * 0.4 + 0.35, 2)}
         for i, w in enumerate("bro he really did that no way WHAT".split())]
BUNDLED = Path(__file__).resolve().parents[1] / "fonts"
PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def tone(path: Path, seconds: float, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    ff("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", str(path))
    return path


# ---- captions ---------------------------------------------------------------------------
def test_default_style_matches_openshorts():
    doc = captions.build_ass(WORDS, captions.PRESETS["default"])
    style = next(line for line in doc.splitlines() if line.startswith("Style: Default"))
    assert style.startswith("Style: Default,Anton,37,&H00FFFFFF") and ",2,10,10,43,1" in style
    events = [line for line in doc.splitlines() if line.startswith("Dialogue")]
    assert len(events) == len(WORDS)
    assert "{\\c&H00E5FF&\\fscx90\\fscy90\\t(0,110,\\fscx108\\fscy108)}BRO{\\r}" in events[0]


def test_reveal_highlight_and_seam():
    doc = captions.build_ass(WORDS, captions.PRESETS["hormozi"], split_ranges=[(1.0, 2.0)])
    first = next(line for line in doc.splitlines() if line.startswith("Dialogue"))
    assert "{\\alpha&HFF&}HE{\\r}" in first                      # words not said yet are invisible
    assert any("{\\an5}" in line for line in doc.splitlines() if ",0:00:01.20," in line)
    pill = captions.build_ass(WORDS, {**captions.PRESETS["lime"], "highlight_color": "#A3FF12"})
    active = next(line for line in pill.splitlines() if line.startswith("Style: Active"))
    assert active.startswith("Style: Active,Montserrat ExtraBold,37,&H00000000")   # black text on light lime
    assert captions.build_ass([], captions.PRESETS["default"]) is None


def test_words_per_line_and_garbage_styles():
    one = captions.build_ass(WORDS, {**captions.DEFAULT_STYLE, "max_chars": 1})
    assert all(line.count("{\\r}") == 1 for line in one.splitlines() if line.startswith("Dialogue"))
    style = captions.normalize_style({"font_name": "Evil;Font\\n", "font_size": 900, "font_color": "red",
                                      "effect": "explode", "position": "left", "max_chars": -4})
    assert (style["font_name"], style["font_size"], style["font_color"], style["effect"], style["position"],
            style["max_chars"]) == ("EvilFontn", 120, "#FFFFFF", "pop", "bottom", 1)


def test_openshorts_words_and_layout():
    words = captions.words_from_openshorts([{"text": " hey ", "startMs": 120, "endMs": 400},
                                            {"text": "", "startMs": 400, "endMs": 500},
                                            {"text": "x", "startMs": 900, "endMs": 800}])
    assert words == [{"w": "hey", "s": 0.12, "e": 0.4}]
    layout = {"ranges": [{"start": 0, "end": 3, "layout": "speaker"}, {"start": 3, "end": 9.5, "layout": "split"}]}
    assert captions.split_ranges_from_layout(layout) == [(3.0, 9.5)]
    assert captions.split_ranges_from_layout(None) == []


# ---- templates ---------------------------------------------------------------------------
def test_normalize_template():
    t = templates.normalize({"name": "  ", "music": {"sound": "../sneaky-snitch.mp3", "volume": 9},
                             "sfx": [{"sound": "vine-boom.wav", "at": "punchline"}, {"sound": "x", "at": "never"},
                                     {"at": "start"}]})
    assert t["name"] == "Template" and t["music"] == {"sound": "sneaky-snitch", "volume": 1.0, "duck": True, "start": 0}
    assert t["sfx"] == [{"sound": "vine-boom", "at": "punchline", "words": [], "volume": 1.0, "max": 2}]
    assert templates.sounds_used(t) == ["sneaky-snitch", "vine-boom"]
    assert {k for k in templates.BUILT_IN} == {"standard", "hormozi", "meme", "sneaky", "hype"}
    assert all(templates.normalize(v)["name"] for v in templates.BUILT_IN.values())


def test_where_effects_land():
    t = templates.normalize({"sfx": [
        {"sound": "boom", "at": "word", "words": ["what", "no"], "max": 3},
        {"sound": "boom", "at": "start"}, {"sound": "ding", "at": "end"},
        {"sound": "boom", "at": "peak"}, {"sound": "boom", "at": "punchline", "max": 1}]})
    events = templates.place_effects(t, WORDS, duration=4.0, peak=1.0, punchlines=[0.9, 3.95],
                                     manual=[{"sound": "airhorn", "t": 0.1}])
    # start, end, peak and the first punchline are each within 1.5 s of an effect placed before them
    assert [(e["sound"], e["t"]) for e in events] == [
        ("airhorn", 0.1),                 # placed by hand: always kept
        ("boom", 2.35),                   # "no" ("WHAT" at 3.15 is too close)
        ("boom", 3.95),                   # the second punchline
    ]
    assert templates.peak_in_clip(130.0, 120.0) == 6.0 and templates.peak_in_clip(None, 5) is None


def test_effect_spacing_and_limits():
    t = templates.normalize({"sfx": [{"sound": "boom", "at": "word", "words": ["what", "no", "way"], "max": 5}]})
    events = templates.place_effects(t, WORDS, duration=4.0)
    assert [e["t"] for e in events] == [2.35]                # "way" and "WHAT" are too close to "no"
    assert templates.place_effects(t, WORDS, duration=2.0) == []   # all past the end
    assert templates.place_effects(templates.normalize({}), WORDS, 4.0,
                                   manual=[{"sound": "a", "t": 1}, {"sound": "b", "t": 1.2}, {"sound": "c", "t": 4.0}]) == [
        {"sound": "a", "t": 1.0, "volume": 1.0}, {"sound": "b", "t": 1.2, "volume": 1.0},
        {"sound": "c", "t": 3.7, "volume": 1.0}]                   # placed at the very end: pulled in


def test_punchlines_from_gemini(settings):
    seen = {}

    def gemini(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"text": json.dumps({"times": [2.71, 1.2, 1.2, "x"]})}]}}]})

    cfg = dataclasses.replace(settings, gemini_api_key="g-key")
    client = httpx.AsyncClient(transport=httpx.MockTransport(gemini))
    assert run(templates.find_punchlines(cfg, WORDS, 2, client)) == [1.2, 2.71]
    assert seen["key"] == "g-key" and "0.75 he" in seen["body"]["contents"][0]["parts"][0]["text"]
    assert seen["body"]["generationConfig"]["responseMimeType"] == "application/json"
    broken = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert run(templates.find_punchlines(cfg, WORDS, 2, broken)) == []
    assert run(templates.find_punchlines(settings, WORDS, 2)) == []        # no key: no call


# ---- the ffmpeg command ----------------------------------------------------------------------
def test_render_command(tmp_path):
    src, dst = tmp_path / "in.mp4", tmp_path / "out.mp4"
    assert render.command(src, dst, render.Plan(duration=5)) is None
    audio_only = render.command(src, dst, render.Plan(duration=5, music=render.Music(tmp_path / "m.mp3", duck=False)))
    assert audio_only[audio_only.index("-c:v") + 1] == "copy" and "sidechaincompress" not in " ".join(audio_only)
    full = render.command(src, dst, render.Plan(
        duration=5, ass=Path("C:/x/captions.ass"), fonts_dir=Path("C:/x/fonts"),
        music=render.Music(tmp_path / "m.mp3"), effects=[render.Effect(tmp_path / "b.wav", 1.25, 0.5)]))
    graph = full[full.index("-filter_complex") + 1]
    assert "[0:v]ass=filename='C\\:/x/captions.ass':fontsdir='C\\:/x/fonts'[v]" in graph
    assert "sidechaincompress" in graph and "adelay=delays=1250:all=1" in graph and "amix=inputs=3" in graph
    assert full[full.index("-c:v") + 1] == "libx264"
    silent = render.command(src, dst, render.Plan(duration=5, effects=[render.Effect(tmp_path / "b.wav", 1)],
                                                  has_audio=False))
    assert "anullsrc" in silent[silent.index("-filter_complex") + 1]


# ---- fonts --------------------------------------------------------------------------------
def test_fonts(settings):
    families = [f["family"] for f in fonts.list_fonts(settings)]
    assert {"Anton", "Montserrat ExtraBold", "Luckiest Guy", "Bangers", "Bebas Neue", "Poppins ExtraBold"} <= set(families)
    user = settings.folder("fonts")
    shutil.copyfile(BUNDLED / "Anton-Regular.ttf", user / "anton-copy.ttf")      # same family: listed once
    (user / "broken.ttf").write_bytes(b"not a font")
    assert [f["family"] for f in fonts.list_fonts(settings)].count("Anton") == 1
    merged = fonts.render_fonts_dir(settings)
    assert (merged / "anton-copy.ttf").is_file() and (merged / "Bangers-Regular.ttf").is_file()
    (user / "anton-copy.ttf").unlink()
    assert not (fonts.render_fonts_dir(settings) / "anton-copy.ttf").exists()
    assert fonts.font_file(settings, "../../etc/passwd") is None


# ---- real renders ------------------------------------------------------------------------------
@pytest.fixture
def clip_env(settings, sample_video):
    store = Store(settings.db_path)
    store.seed_templates({k: templates.normalize(v) for k, v in templates.BUILT_IN.items()}, "standard")
    pipeline = Pipeline(settings, store, openshorts=None)
    job = store.create_job(111, "https://www.twitch.tv/videos/9", 100.0, 235.0, 1, template="meme")
    store.update_job(job.id, status="done")
    clean = settings.folder("clean") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, clean)
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", None, {"start": 10.0, "punchlines": [1.0]}, 3.0, str(master), None)
    store.update_clip(clip.id, words=json.dumps(WORDS[:6]))
    yield settings, store, pipeline, store.get_clip(clip.id)
    run(pipeline.notify_client.aclose())
    store.close()


def test_rerender_with_template_and_hand_placed_effects(clip_env):
    settings, store, pipeline, clip = clip_env
    tone(settings.data_dir / "sounds" / "vine-boom.wav", 0.6, 80)
    tone(settings.data_dir / "sounds" / "airhorn.mp3", 0.5, 900)
    before = Path(clip.path).read_bytes()
    run(pipeline.rerender(clip.id, "meme", [{"sound": "airhorn", "t": 2.0}]))
    clip = store.get_clip(clip.id)
    assert clip.render_state is None and clip.render_error is None, clip.render_error
    assert clip.template == "meme" and clip.manual_sfx() == [{"sound": "airhorn", "t": 2.0}]
    assert Path(clip.path).read_bytes() != before
    assert (settings.data_dir / "thumbs" / f"{clip.job_id}_0.jpg").is_file()
    assert not list((settings.data_dir / "tmp").glob("render-*"))


def test_missing_sounds_are_reported_not_fatal(clip_env):
    settings, store, pipeline, clip = clip_env
    run(pipeline.rerender(clip.id, "sneaky"))
    clip = store.get_clip(clip.id)
    assert clip.render_state is None and "sneaky-snitch" in clip.render_error
    assert clip.meta["credit"].startswith('Music: "Sneaky Snitch" Kevin MacLeod')
    assert caption_for("tiktok", clip).endswith("creativecommons.org/licenses/by/4.0/)")


def test_peak_lands_where_chat_exploded(clip_env, monkeypatch):
    settings, store, pipeline, clip = clip_env
    scan = store.create_scan(111, "https://www.twitch.tv/videos/9")
    moment = store.add_moment(scan.id, 1, peak=115.5, start=100.0, end=235.0, score=3.0)
    store.claim_moment(moment.id, clip.job_id)
    tone(settings.data_dir / "sounds" / "vine-boom.wav", 0.4, 80)
    placed = {}
    real = templates.place_effects

    def spy(template, words, duration, peak=None, punchlines=None, manual=None):
        placed["peak"] = peak
        return real(template, words, duration, peak, punchlines, manual)

    monkeypatch.setattr(templates, "place_effects", spy)
    run(pipeline.rerender(clip.id, "hype"))
    assert placed["peak"] == pytest.approx(1.5)              # 115.5 - 4 s chat lag - (100 + 10)
    assert store.get_clip(clip.id).render_error is None


# ---- dashboard -------------------------------------------------------------------------------------
@pytest.fixture
def dash(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    client = OpenShorts(cfg.openshorts_url, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with TestClient(create_app(cfg, openshorts=client, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_template_api(dash):
    data = dash.get("/api/templates").json()
    assert [t["id"] for t in data["items"]] == ["standard", "hormozi", "meme", "sneaky", "hype"]
    assert data["default"] == "standard" and "oneword" in data["presets"] and "punchline" in data["triggers"]
    assert {"family": "Anton", "file": "Anton-Regular.ttf", "bundled": True} in data["fonts"]
    meme = next(t for t in data["items"] if t["id"] == "meme")
    assert meme["missing_sounds"] == ["vine-boom"]

    made = dash.post("/api/templates", json={"name": "Meme", "captions": {"font_name": "Bangers"}}).json()
    assert made["id"] == "meme-2" and made["captions"]["font_name"] == "Bangers"
    made["sfx"] = [{"sound": "vine-boom", "at": "start"}]
    assert dash.put("/api/templates/meme-2", json=made).json()["sfx"][0]["at"] == "start"
    assert dash.post("/api/templates/meme-2/default").json() == {"default": "meme-2"}
    assert dash.delete("/api/templates/meme-2").status_code == 409            # it's the default
    assert dash.delete("/api/templates/hype").json() == {"ok": True}
    assert dash.put("/api/templates/nope", json=made).status_code == 404


def test_sound_and_font_uploads(dash, tmp_path):
    wav = tone(tmp_path / "boom.wav", 0.5)
    r = dash.put("/api/sounds/Vine Boom!.wav", content=wav.read_bytes())
    assert r.status_code == 201 and r.json()["name"] == "Vine Boom" and 0.4 < r.json()["duration"] < 0.6
    assert dash.put("/api/sounds/notes.txt", content=b"x").status_code == 422
    assert dash.put("/api/sounds/fake.mp3", content=b"not audio").status_code == 422
    assert [s["name"] for s in dash.get("/api/sounds").json()["items"]] == ["Vine Boom"]
    assert dash.get("/api/sounds/Vine Boom/file").headers["content-type"] == "audio/wav"
    assert dash.delete("/api/sounds/Vine Boom").json() == {"ok": True}

    font = (BUNDLED / "Bangers-Regular.ttf").read_bytes()
    assert dash.put("/api/fonts/MyFont.ttf", content=font).json() == {"family": "Bangers", "file": "MyFont.ttf",
                                                                        "bundled": False}
    assert dash.put("/api/fonts/bad.ttf", content=b"nope").status_code == 422
    assert dash.get("/api/fonts/Anton-Regular.ttf/file").headers["content-type"] == "font/ttf"
    assert dash.delete("/api/fonts/Anton-Regular.ttf").status_code == 404    # bundled ones stay
    assert dash.delete("/api/fonts/MyFont.ttf").json() == {"ok": True}


def test_render_endpoint(dash, sample_video):
    store, settings = dash.app.state.store, dash.app.state.settings
    job = store.create_job(111, "https://youtu.be/x", None, None, 1)
    store.update_job(job.id, status="done")
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", None, {}, 3.0, str(master), None)
    assert dash.post(f"/api/clips/{clip.id}/render", json={}).status_code == 409   # no copy without captions
    shutil.copyfile(sample_video, settings.folder("clean") / f"{job.id}_0.mp4")
    store.update_clip(clip.id, words=json.dumps(WORDS))
    assert dash.post(f"/api/clips/{clip.id}/render", json={"template": "nope"}).status_code == 422
    r = dash.post(f"/api/clips/{clip.id}/render", json={"template": "hormozi", "sfx": [{"sound": "x.wav", "t": 1}]})
    assert r.status_code == 202 and r.json()["render_state"] == "rendering"
    for task in list(dash.app.state.render_tasks):
        dash.portal.call(asyncio.wait_for, asyncio.shield(task), 120)
    out = dash.get("/api/clips?status=pending").json()["items"][0]
    assert out["template"] == "hormozi" and out["template_name"] == "Hormozi" and out["sfx"] == [
        {"sound": "x", "t": 1.0, "volume": 1.0}]
    assert out["render_state"] is None and "x" in out["render_error"] and out["can_render"] is True
    assert out["word_count"] == len(WORDS) and "?v=" in out["video_url"]


# ---- Telegram -----------------------------------------------------------------------------------
def test_template_in_messages(dash):
    assert parse_message("https://youtu.be/x 1:00-3:00 t=Meme clips=2").template == "meme"
    assert parse_message("https://youtu.be/x template=hype").template == "hype"
    assert parse_message("/templates").kind == "templates"
    app, settings = dash.app, dash.app.state.settings
    reply = dash.portal.call(handle_message, app, settings, 111, "https://youtu.be/x t=nope")
    assert reply.startswith("⚠️ There's no template called nope")
    reply = dash.portal.call(handle_message, app, settings, 111, "https://youtu.be/x t=Sneaky")
    assert "in the Sneaky template" in reply
    assert app.state.store.list_jobs()[0].template == "sneaky"
    text = dash.portal.call(handle_message, app, settings, 111, "/templates")
    assert "• standard (default): Anton" in text and "missing sound(s): sneaky-snitch" in text
