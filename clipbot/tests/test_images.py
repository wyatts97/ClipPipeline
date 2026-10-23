"""The editor's pictures: uploads (PNG, JPEG, GIF, WebP, checked by their bytes), the bundled stickers,
importing from the web (public addresses only, redirects checked, 15 MB at most), frame grabs, and the API."""
import asyncio
import dataclasses
import json
import shutil
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from app import images
from app.main import create_app

PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def pictures(tmp_path_factory):
    d = tmp_path_factory.mktemp("pics")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x32", "-frames:v", "1",
                    str(d / "red.png")], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=40x40:r=5:d=1", str(d / "spin.gif")],
                   check=True)
    return {"png": (d / "red.png").read_bytes(), "gif": (d / "spin.gif").read_bytes()}


def test_kinds_and_names():
    assert images.kind(b"\x89PNG\r\n\x1a\nxx") == ".png" and images.kind(b"GIF89a..") == ".gif"
    assert images.kind(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp" and images.kind(b"<svg") is None
    assert images.image_name("../my face?.PNG") == "my face" and images.image_name("sticker:skull") == "stickerskull"
    assert {"skull", "mind-blown", "fire", "moai"} <= set(images.stickers())


def test_library(settings, pictures):
    name = run(images.save_upload(settings, "Red Box.png", pictures["png"]))
    assert name == "Red Box" and images.find_image(settings, "red box").name == "Red Box.png"
    got = images.items(settings)
    assert [(i["name"], i["width"], i["height"], i["animated"], i["source"]) for i in got] == [("Red Box", 64, 32, False, "upload")]
    gif = run(images.save_upload(settings, "spin.gif", pictures["gif"]))
    assert images.items(settings)[1]["animated"] is True and gif == "spin"
    with pytest.raises(images.ImageError, match="PNG, JPEG"):
        run(images.save_upload(settings, "evil.png", b"<script>"))
    assert images.find_image(settings, "sticker:skull").parent == images.STICKERS_DIR
    assert images.find_image(settings, "sticker:../../app/main") is None and images.find_image(settings, "nope") is None


def test_import_from_the_web(settings, pictures, monkeypatch):
    with pytest.raises(images.ImageError, match="private"):
        images._public("127.0.0.1")
    checked = []
    monkeypatch.setattr(images, "_public", lambda host: checked.append(host))

    def web(request: httpx.Request) -> httpx.Response:
        if request.url.host == "short.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/pics/cat.png"})
        if request.url.path == "/pics/cat.png":
            return httpx.Response(200, content=pictures["png"])
        if request.url.path == "/huge.png":
            return httpx.Response(200, content=pictures["png"] + b"\0" * images.MAX_BYTES)
        return httpx.Response(200, text="<html>not a picture</html>")

    client = httpx.AsyncClient(transport=httpx.MockTransport(web))
    name = run(images.import_url(settings, "https://short.example/x", client=client))
    assert name == "cat" and checked == ["short.example", "cdn.example"]          # each hop checked
    assert images.load_meta(settings)["cat"]["page"] == "https://cdn.example/pics/cat.png"
    for url, why in (("https://cdn.example/huge.png", "15 MB"), ("https://cdn.example/page", "PNG, JPEG"),
                     ("ftp://cdn.example/x.png", "web address"), ("file:///etc/passwd", "web address")):
        with pytest.raises(images.ImageError, match=why):
            run(images.import_url(settings, url, client=client))


@pytest.fixture
def dash(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    with TestClient(create_app(cfg, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_picture_api(dash, pictures, sample_video):
    assert dash.put("/api/images/face.png", content=b"nope").status_code == 422
    up = dash.put("/api/images/face.png", content=pictures["png"]).json()
    assert (up["name"], up["width"], up["url"]) == ("face", 64, "/api/images/face/file")
    assert dash.get(up["url"]).content == pictures["png"]
    listed = dash.get("/api/images").json()
    assert [i["name"] for i in listed["items"]] == ["face"]
    skull = next(s for s in listed["stickers"] if s["name"] == "skull")
    assert skull["ref"] == "sticker:skull" and dash.get(skull["url"]).content[:4] == b"\x89PNG"
    assert dash.get("/api/stickers/..%2F..%2Fapp%2Fmain.png").status_code == 404

    # A frame grab: the box in the middle of the clip's frame at 1 s.
    store, settings = dash.app.state.store, dash.app.state.settings
    job = store.create_job(111, "https://youtu.be/x", None, None, 1)
    master = settings.folder("pending") / f"{job.id}_0.mp4"
    shutil.copyfile(sample_video, master)
    clip = store.add_clip(job.id, 0, "T", None, {}, 3.0, str(master), None)
    grab = dash.post(f"/api/clips/{clip.id}/frame", json={"t": 1, "crop": {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}}).json()
    full = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", str(sample_video)],
                          capture_output=True, text=True).stdout.split()[0]
    assert grab["name"] == "frame 1.0s" and grab["source"] == "frame" and abs(grab["width"] - int(full) / 2) <= 1
    assert dash.post(f"/api/clips/{clip.id}/frame", json={"t": 99}).status_code == 422          # past the end

    assert dash.delete("/api/images/face").json() == {"ok": True}
    assert "face" not in json.loads((settings.data_dir / "images" / "images.json").read_text())
    assert dash.delete("/api/images/face").status_code == 404
