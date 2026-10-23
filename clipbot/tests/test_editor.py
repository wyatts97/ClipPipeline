"""The clip editor: the edit model and its time maps, the ffmpeg graph for cuts and zooms, real
re-renders with cuts / zooms / edited caption words, OpenShorts re-cuts (extend, reframe), and the API."""
import asyncio
import dataclasses
import json
import shutil
import subprocess
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
    styled = editing.normalize({"zooms": [{"s": 0, "e": 1, "style": "shake", "x": 0.2, "y": 0.5},
                                          {"s": 1, "e": 2, "style": "wobble", "x": -3, "y": "?"}]}, 3.0)
    assert styled["zooms"] == [{"s": 0.0, "e": 1.0, "scale": 1.15, "style": "shake", "x": 0.2},   # centre and
                               {"s": 1.0, "e": 2.0, "scale": 1.15, "x": 0.0}]                    # punch: left out
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
    edit = {"segments": segs, "zooms": [{"s": 0.8, "e": 2.2, "scale": 1.2, "style": "push"}]}
    assert editing.zoom_ranges(edit) == [{"s": 0.8, "e": 1.0, "scale": 1.2, "style": "push"},
                                         {"s": 1.0, "e": 1.2, "scale": 1.2, "style": "push"}]


def test_timeline_freezes_and_transitions():
    edit = editing.normalize({"segments": [[0, 2], [3, 5], [6, 8]],
                              "freezes": [{"t": 1.0, "d": 1.5, "style": "gray"}, {"t": 8, "d": 1, "style": "nope"},
                                          {"t": 2.5, "d": 1}],                       # in a cut part: dropped
                              "transitions": [{"after": 2.02, "type": "fade", "d": 0.5}, {"after": 5, "type": "flash"},
                                              {"after": 7, "type": "fade"}]}, 8.0)   # not on a cut: dropped
    assert edit["freezes"] == [{"t": 1.0, "d": 1.5, "style": "gray"}, {"t": 7.967, "d": 1.0}]   # the last frame
    assert edit["transitions"] == [{"after": 2.0, "type": "fade", "d": 0.5}, {"after": 5.0, "type": "flash"}]
    tl = editing.timeline(edit)
    # 0-3.5: part one with its 1.5 s freeze; part two starts 0.5 s early (the fade); the flash changes nothing.
    assert [(p["o"], p["len"], p["x"]) for p in tl] == [(0.0, 3.5, 0.0), (3.0, 2.0, 0.5), (5.0, 3.0, 0.0)]
    assert editing.output_duration(edit) == 8.0 and editing.is_cut(edit, 8.0) and editing.reshapes(edit)
    assert [editing.map_time(t, edit) for t in (0.5, 1.0, 1.5, 2.5, 3.0, 7.0)] == [0.5, 1.0, 3.0, None, 3.0, 6.0]
    assert editing.map_range(0.5, 1.5, edit) == [(0.5, 3.0)]                       # stretched through the freeze
    assert editing.map_words([{"w": "held", "s": 0.8, "e": 1.2}], edit) == [{"w": "held", "s": 0.8, "e": 2.7}]
    assert editing.accents(edit) == {"zooms": [], "gray": [(1.0, 2.5)], "flash": [5.0], "glitch": [], "scratch": []}
    # A transition can't eat more than 45% of either side; parts too short for one lose it.
    tight = editing.normalize({"segments": [[0, 1], [2, 4], [5, 5.3]],
                               "transitions": [{"after": 1, "type": "wipe", "d": 1}, {"after": 4, "type": "fade"}]}, 6.0)
    assert tight["transitions"] == [{"after": 1.0, "type": "wipe", "d": 0.45}, {"after": 4.0, "type": "fade", "d": 0.135}]
    cuts_only = editing.normalize({"segments": [[0, 1], [2, 3]], "transitions": [{"after": 1, "type": "shake"}]}, 3.0)
    assert not editing.reshapes(cuts_only) and editing.accents(cuts_only)["zooms"][0]["style"] == "shake"


def test_shift_after_extending():
    whole, sfx = editing.shift(None, [{"sound": "x", "t": 1.0}], by=2.0, old_duration=10, new_duration=14)
    assert whole["segments"] == [[0.0, 14.0]] and sfx == [{"sound": "x", "t": 3.0}]
    cut = {"segments": [[0, 3], [5, 10]], "zooms": [{"s": 1, "e": 2, "scale": 1.2}]}
    moved, _ = editing.shift(cut, [], by=2.0, old_duration=10, new_duration=14)
    assert moved == {"segments": [[2.0, 5.0], [7.0, 12.0]], "zooms": [{"s": 3.0, "e": 4.0, "scale": 1.2}]}


# ---- ffmpeg -------------------------------------------------------------------------------------
def test_cut_and_zoom_command(tmp_path):
    plan = render.Plan(duration=2.0, segments=[[0.0, 1.0], [2.0, 3.0]], zooms=[{"s": 0.2, "e": 0.6, "scale": 1.3}],
                       size=(540, 960))
    cmd = render.command(tmp_path / "in.mp4", tmp_path / "out.mp4", plan)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]trim=start=0.000:end=1.000,setpts=PTS-STARTPTS[v0]" in graph
    assert "[0:a]atrim=start=2.000:end=3.000,asetpts=PTS-STARTPTS[a1]" in graph
    assert "[v0][a0][v1][a1]concat=n=2:v=1:a=1[vcut][acut]" in graph
    factor = "(1+if(between(t,0.200,0.600),0.300*min(1,(t-0.200)/0.12),0))"
    assert f"[vcut]scale=w='(2*trunc(540*{factor}/2))'" in graph
    assert f"crop=w=540:h=960:x='clip(0.5*(2*trunc(540*{factor}/2))-270,0," in graph   # centred, from the known size
    assert "[acut]volume=1.000[voice]" in graph
    assert cmd[cmd.index("-map") + 1] == "[vz]" and cmd[cmd.index("-c:v") + 1] == "libx264"
    silent = render.command(tmp_path / "in.mp4", tmp_path / "out.mp4",
                            render.Plan(duration=2.0, segments=[[0.0, 1.0], [2.0, 3.0]], has_audio=False))
    assert "concat=n=2:v=1:a=0[vcut]" in silent[silent.index("-filter_complex") + 1]


# ---- zoom styles, rendered ------------------------------------------------------------------------
QUAD = {"red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255), "yellow": (255, 255, 0)}   # ffmpeg's colors


@pytest.fixture(scope="module")
def quad_video(tmp_path_factory):
    """4 s at 10 fps, 360x640: red top-left, green top-right, blue bottom-left, yellow bottom-right."""
    path = tmp_path_factory.mktemp("quad") / "quad.mp4"
    tiles = "".join(f"color={c}:s=180x320:d=4:r=10[{c}];" for c in QUAD)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-filter_complex",
                    f"{tiles}[red][green]hstack[top];[blue][yellow]hstack[bot];[top][bot]vstack[v]",
                    "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def colors_at(path: Path, t: float, points) -> list[str]:
    """The quadrant colour nearest each (x, y) share of the frame at time t."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    w, h = 360, 640
    out = []
    for fx, fy in points:
        i = (int(fy * (h - 1)) * w + int(fx * (w - 1))) * 3
        px = raw[i:i + 3]
        out.append(min(QUAD, key=lambda c: sum((a - b) ** 2 for a, b in zip(px, QUAD[c]))))
    return out


def render_zooms(src, dst, zooms):
    plan = render.Plan(duration=4.0, zooms=zooms, size=(360, 640), has_audio=False)
    subprocess.run(render.command(src, dst, plan), check=True)
    return dst


def test_zoom_styles_render(quad_video, tmp_path):
    corners = [(0.02, 0.02), (0.98, 0.02), (0.02, 0.98), (0.98, 0.98)]
    centred = render_zooms(quad_video, tmp_path / "c.mp4", [{"s": 0, "e": 4, "scale": 2.0}])
    assert colors_at(centred, 1.0, corners) == ["red", "green", "blue", "yellow"]     # the middle, not a corner
    aimed = render_zooms(quad_video, tmp_path / "a.mp4", [{"s": 0, "e": 4, "scale": 2.0, "x": 0.25, "y": 0.25}])
    assert set(colors_at(aimed, 1.0, corners)) == {"red"}                              # all top-left
    # Aimed at the top-left corner, (0.9, 0.9) shows red only when zoomed in past 1.8x.
    timed = render_zooms(quad_video, tmp_path / "t.mp4", [
        {"s": 0, "e": 2, "scale": 2.0, "style": "out", "x": 0, "y": 0},
        {"s": 2, "e": 4, "scale": 2.0, "style": "smooth", "x": 0, "y": 0}])
    probe = [(0.9, 0.9)]
    assert colors_at(timed, 0.1, probe) == ["red"] and colors_at(timed, 1.9, probe) == ["yellow"]   # out: close, then back
    assert colors_at(timed, 2.05, probe) == ["yellow"] and colors_at(timed, 3.0, probe) == ["red"]  # smooth: eases in

    shaken = render_zooms(quad_video, tmp_path / "s.mp4", [{"s": 0, "e": 1.5, "scale": 1.3, "style": "shake"}])

    def seam(t):         # the pixel where red turns green along the top row
        return colors_at(shaken, t, [(x / 359, 0.1) for x in range(360)]).index("green")
    moving = {seam(t) for t in (0.1, 0.2, 0.7, 0.8)}
    assert len(moving) >= 3 and abs(seam(2.0) - 180) <= 1                             # shakes, then settles


@pytest.fixture(scope="module")
def color_video(tmp_path_factory):
    """4 s at 30 fps, 360x640, with a tone: red, green, blue, then yellow, a second each."""
    path = tmp_path_factory.mktemp("colors") / "colors.mp4"
    tiles = "".join(f"color={c}:s=360x640:d=1:r=30[{c}];" for c in QUAD)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-filter_complex", f"{tiles}[red][green][blue][yellow]concat=n=4[v]",
                    "-f", "lavfi", "-i", "sine=f=440:d=4", "-map", "[v]", "-map", "0:a", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def rgb_at(path: Path, t: float) -> tuple[int, int, int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    i = (320 * 360 + 180) * 3
    return tuple(raw[i:i + 3])


def near(px, color, tol=60):
    return all(abs(a - b) <= tol for a, b in zip(px, QUAD.get(color, color)))


def render_edit(src: Path, dst: Path, raw: dict) -> Path:
    edit = editing.normalize(raw, 4.0)
    extra = editing.accents(edit)
    plan = render.Plan(duration=editing.output_duration(edit), segments=edit["segments"],
                       zooms=editing.zoom_ranges(edit) + extra["zooms"], size=(360, 640),
                       timeline=editing.timeline(edit) if editing.reshapes(edit) else None,
                       gray=extra["gray"], flashes=extra["flash"], glitches=extra["glitch"])
    subprocess.run(render.command(src, dst, plan), check=True)
    return dst


def test_freezes_and_transitions_render(color_video, tmp_path):
    # Hold the red frame at 0.5 s for a second (gray), then fade from green into blue at the cut.
    out = render_edit(color_video, tmp_path / "ft.mp4", {
        "segments": [[0, 2], [2.5, 4]], "freezes": [{"t": 0.5, "d": 1.0, "style": "gray"}],
        "transitions": [{"after": 2, "type": "fade", "d": 0.4}]})
    assert abs(run(probe_duration(out)) - 4.1) < 0.1                                 # 3.0 + 1.5 - 0.4
    assert near(rgb_at(out, 0.3), "red") and near(rgb_at(out, 2.4), "green")
    held = rgb_at(out, 1.2)
    assert max(held) - min(held) < 30 and 40 < held[0] < 140                         # red, frozen, in gray
    mixed = rgb_at(out, 2.8)                                                          # mid-fade: green and blue
    assert not near(mixed, "green") and not near(mixed, "blue") and mixed[1] > 40 and mixed[2] > 40
    assert near(rgb_at(out, 3.05), "blue") and near(rgb_at(out, 3.8), "yellow")
    has = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                          "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert has                                                                        # the sound came through

    # Accents keep the timing: a flash, a glitch, a shake and a zoom punch on plain cuts, and a slide.
    out = render_edit(color_video, tmp_path / "acc.mp4", {
        "segments": [[0, 0.9], [1, 1.9], [2, 2.9], [3, 4]],
        "transitions": [{"after": 0.9, "type": "flash"}, {"after": 1.9, "type": "glitch"},
                        {"after": 2.9, "type": "slideleft", "d": 0.3}]})
    assert abs(run(probe_duration(out)) - (3.7 - 0.3)) < 0.1
    assert sum(rgb_at(out, 0.9)) > sum(QUAD["green"]) + 200                         # washed toward white
    assert near(rgb_at(out, 0.5), "red") and near(rgb_at(out, 1.5), "green")


@pytest.fixture(scope="module")
def white_png(tmp_path_factory):
    d = tmp_path_factory.mktemp("pic")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=white:s=64x64", "-frames:v", "1",
                    str(d / "white.png")], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=white:s=64x64:r=5:d=1",
                    str(d / "white.gif")], check=True)
    return d / "white.png"


def px(path: Path, t: float, fx: float, fy: float) -> tuple[int, int, int]:
    """The colour at (fx, fy), shares of the 360x640 frame, at time t."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True).stdout
    i = (int(fy * 639) * 360 + int(fx * 359)) * 3
    return tuple(raw[i:i + 3])


def white(color, tol=40):
    return all(c >= 255 - tol for c in color)


def render_pictures(src: Path, dst: Path, pictures: list[dict]) -> Path:
    """pictures: overlay fields plus "path" (and "gif"); placed on the 4-s clip as the editor would."""
    edit = editing.normalize({"overlays": pictures}, 4.0)
    placed = [{**o, "path": raw["path"], "size": (64, 64), "gif": raw.get("gif", False)}
              for o, raw in zip(editing.overlay_ranges(edit), pictures)]
    plan = render.Plan(duration=4.0, size=(360, 640), has_audio=False, overlays=placed)
    subprocess.run(render.command(src, dst, plan), check=True)
    return dst


def test_overlay_model():
    edit = editing.normalize({"segments": [[0, 1], [2, 4]], "freezes": [{"t": 2.5, "d": 1}],
                              "overlays": [{"image": "sticker:skull", "s": 0.5, "e": 3, "w": 9, "enter": "boing"},
                                           {"image": "", "s": 0, "e": 1}, {"image": "x", "s": 1, "e": 1.05}]}, 4.0)
    assert edit["overlays"] == [{"image": "sticker:skull", "s": 0.5, "e": 3.0, "x": 0.5, "y": 0.5, "w": 1.0, "rot": 0.0,
                                 "opacity": 1.0, "enter": "none", "exit": "none", "motion": "none"}]
    # Up through the cut, and through the freeze: source 3.0 is 1 s of part one + 1 s of part two + the 1-s freeze.
    assert [(o["s"], o["e"]) for o in editing.overlay_ranges(edit)] == [(0.5, 3.0)]
    moved, _ = editing.shift(edit, [], by=1.0, old_duration=4.0, new_duration=5.0)
    assert (moved["overlays"][0]["s"], moved["overlays"][0]["e"]) == (1.5, 4.0)


def test_pictures_render(quad_video, white_png, tmp_path):
    # A white square, 20% of the width, centred at (0.25, 0.25) from 1 s to 2 s: on red before and after.
    plain = render_pictures(quad_video, tmp_path / "p.mp4", [
        {"image": "w", "path": white_png, "s": 1, "e": 2, "x": 0.25, "y": 0.25, "w": 0.2}])
    assert not white(px(plain, 0.5, 0.25, 0.25)) and white(px(plain, 1.5, 0.25, 0.25))
    assert not white(px(plain, 1.5, 0.25 + 0.15, 0.25)) and not white(px(plain, 2.5, 0.25, 0.25))

    # Pop: tiny as it starts, then full size (its edge, 0.08 of the width from the middle, turns white).
    pop = render_pictures(quad_video, tmp_path / "pop.mp4", [
        {"image": "w", "path": white_png, "s": 1, "e": 3, "x": 0.5, "y": 0.5, "w": 0.2, "enter": "pop", "exit": "fade"}])
    assert not white(px(pop, 1.04, 0.58, 0.5)) and white(px(pop, 1.6, 0.58, 0.5))
    # Slides in from the left: still off to the left just after it starts.
    slid = render_pictures(quad_video, tmp_path / "slide.mp4", [
        {"image": "w", "path": white_png, "s": 1, "e": 3, "x": 0.5, "y": 0.5, "w": 0.2, "enter": "slide-left"}])
    assert not white(px(slid, 1.05, 0.5, 0.5)) and white(px(slid, 1.6, 0.5, 0.5))
    # Half see-through over blue (bottom-left), turned 45° (a corner of the square's box stays blue), and a GIF.
    mixed = render_pictures(quad_video, tmp_path / "mix.mp4", [
        {"image": "w", "path": white_png, "s": 0, "e": 4, "x": 0.25, "y": 0.75, "w": 0.25, "opacity": 0.5},
        {"image": "w", "path": white_png, "s": 0, "e": 4, "x": 0.75, "y": 0.25, "w": 0.3, "rot": 45},
        {"image": "g", "path": white_png.with_suffix(".gif"), "gif": True, "s": 0, "e": 4, "x": 0.75, "y": 0.75, "w": 0.2}])
    r, g, b = px(mixed, 2.0, 0.25, 0.75)
    assert 90 < r < 170 and 90 < g < 170 and b > 200                                    # white over blue, halfway
    assert white(px(mixed, 2.0, 0.75, 0.25))                                           # the turned square's middle
    assert not white(px(mixed, 2.0, 0.75 + 0.14, 0.25 - 0.14 * 360 / 640))            # its box corner: off the diamond
    assert white(px(mixed, 3.5, 0.75, 0.75))                                           # the GIF, still looping


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
    run(pipeline.rerender(clip.id, edit={"segments": [[0, 1], [2, 3]], "zooms": [{"s": 0.2, "e": 0.8, "style": "shake", "x": 0.3}]},
                          words=words))
    clip = store.get_clip(clip.id)
    assert clip.render_error is None and clip.render_state is None
    assert json.loads(clip.edit)["segments"] == [[0.0, 1.0], [2.0, 3.0]]
    assert json.loads(clip.edit)["zooms"] == [{"s": 0.2, "e": 0.8, "scale": 1.15, "style": "shake", "x": 0.3}]
    assert clip.word_list()[0]["w"] == "ONE!" and clip.meta["words_original"][0]["w"] == "one"
    assert clip.duration == 2.0 and abs(run(probe_duration(Path(clip.path))) - 2.0) < 0.15
    run(pipeline.rerender(clip.id))                                   # nothing given: the edit stays
    assert json.loads(store.get_clip(clip.id).edit)["segments"] == [[0.0, 1.0], [2.0, 3.0]]


def test_rerender_with_pictures(clip_env):
    settings, store, pipeline, clip = clip_env
    run(pipeline.rerender(clip.id, edit={"overlays": [
        {"image": "sticker:skull", "s": 0.5, "e": 2.5, "x": 0.5, "y": 0.3, "w": 0.4, "rot": -10, "enter": "spin",
         "exit": "slide-right", "motion": "pulse"},
        {"image": "gone", "s": 0, "e": 1}]}))
    clip = store.get_clip(clip.id)
    assert clip.render_state is None and "the picture gone isn't in the library" in (clip.render_error or "")
    assert [o["image"] for o in json.loads(clip.edit)["overlays"]] == ["sticker:skull", "gone"]
    assert abs(run(probe_duration(Path(clip.path))) - clip.duration) < 0.15


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

    # A draft: the unsaved freeze and fade, small and quick, while the clip and its edit stay as they were.
    before = dash.app.state.store.get_clip(clip.id)
    assert dash.get(f"/api/clips/{clip.id}/draft").json()["state"] is None
    r = dash.post(f"/api/clips/{clip.id}/draft", json={"edit": {
        "segments": [[0, 1], [1.5, 3]], "freezes": [{"t": 0.5, "d": 1, "style": "scratch"}],
        "transitions": [{"after": 1, "type": "fade", "d": 0.3}]}})
    assert r.status_code == 202 and r.json()["state"] == "rendering"
    wait(dash)
    state = dash.get(f"/api/clips/{clip.id}/draft").json()
    assert state["state"] == "done", state
    draft = dash.app.state.settings.data_dir / "tmp" / "drafts" / f"{clip.id}.mp4"
    assert dash.get(state["url"]).content == draft.read_bytes()
    assert abs(run(probe_duration(draft)) - (1 + 1 + 1.5 - 0.3)) < 0.15
    size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height",
                           "-of", "csv=p=0", str(draft)], capture_output=True, text=True).stdout.strip()
    assert size == "854"
    after = dash.app.state.store.get_clip(clip.id)
    assert (after.edit, after.path, after.duration, after.render_state) == (before.edit, before.path, before.duration, None)
    assert Path(after.path).stat().st_mtime == Path(before.path).stat().st_mtime

    assert dash.post(f"/api/clips/{clip.id}/recut", json={}).status_code == 422
    assert dash.post(f"/api/clips/{clip.id}/recut", json={"framing": "sideways"}).status_code == 422
    assert dash.post(f"/api/clips/{clip.id}/recut", json={"extend_end": 2}).status_code == 202
    wait(dash)
    assert dash.app.state.store.get_clip(clip.id).render_state is None
    assert dash.fake.recuts[-1]["segments"] == [{"start": 600.0, "end": 605.0}]
