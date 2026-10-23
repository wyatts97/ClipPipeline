"""The sound library: what's known about each sound, the meme pack (Myinstants pages → MP3s, failures
reported, sounds already there kept), searching Myinstants and Freesound (CC0 only), importing, and
the dashboard's sound API."""
import asyncio
import dataclasses
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import sounds, templates
from app.main import create_app

MP3 = b"ID3" + b"\x00" * 64
PASSWORD = "correct horse"


def run(coro):
    return asyncio.run(coro)


def page(mp3):
    return f'<html><meta property="og:audio" content="https://www.myinstants.com{mp3}"/></html>'


SEARCH_HTML = """
<div class="instant"><button class="small-button" onclick="play('/media/sounds/huh-cat.mp3', 'loader-1', 'huh-cat-21280')"></button>
<a href="/en/instant/huh-cat-21280/" class="instant-link link-secondary">Huh Cat</a></div>
<div class="instant"><button class="small-button" onclick="play('/media/sounds/x_2.mp3', 'loader-2', 'what-s-up-9')"></button>
<a href="/en/instant/what-s-up-9/" class="instant-link link-secondary">What&#x27;s up</a></div>
<div class="instant"><button class="small-button" onclick="play('/media/sounds/huh-cat.mp3', 'loader-3', 'huh-cat-21280')"></button>
<a href="/en/instant/huh-cat-21280/" class="instant-link link-secondary">Huh Cat</a></div>"""


class Web:
    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if "/en/instant/vine-boom-sound-70972/" in url:
            return httpx.Response(200, text=page("/media/sounds/vine-boom.mp3"))
        if "/en/instant/" in url and "huh" not in url:
            return httpx.Response(404)
        if "/en/instant/huh-cat-21280/" in url:
            return httpx.Response(200, text="<html>no audio here</html>")
        if url.endswith(".mp3") or "freesound.org/data/previews" in url:
            return httpx.Response(200, content=MP3)
        if "/en/search/" in url:
            return httpx.Response(200, text=SEARCH_HTML)
        if "/apiv2/search/text/" in url:
            return httpx.Response(200, json={"results": [
                {"id": 123, "name": "Big boom", "duration": 1.2, "username": "sfxguy", "license": "CC0",
                 "previews": {"preview-hq-mp3": "https://cdn.freesound.org/data/previews/1/123-hq.mp3"}},
                {"id": 124, "name": "no preview", "previews": {}}]})
        return httpx.Response(404)


@pytest.fixture
def web():
    fake = Web()
    return fake, httpx.AsyncClient(transport=httpx.MockTransport(fake))


def test_meme_pack_installs_what_it_can(settings, web, monkeypatch):
    fake, client = web
    monkeypatch.setattr(sounds, "MEME_PACK", (("vine-boom", "impact", "vine-boom-sound-70972"),
                                              ("huh", "reaction", "huh-cat-21280"),
                                              ("gone", "funny", "gone-404"), ("bruh", "reaction", "bruh-1")))
    monkeypatch.setattr(sounds, "PACK_MUSIC", (("sneaky-snitch", "music", "https://incompetech.com/s.mp3",
                                                "CC BY 4.0", "Kevin MacLeod"),))
    (sounds.folder(settings) / "bruh.mp3").write_bytes(MP3)                    # already there: kept
    result = run(sounds.install_pack(settings, client))
    assert result["added"] == ["vine-boom", "sneaky-snitch"] and result["kept"] == ["bruh"]
    assert set(result["failed"]) == {"huh", "gone"} and "no sound" in result["failed"]["huh"]
    assert (sounds.folder(settings) / "vine-boom.mp3").read_bytes() == MP3
    assert sounds.describe(settings, "vine-boom") == {"category": "impact", "source": "myinstants", "license": None,
                                                     "author": None, "favorite": False}
    assert sounds.describe(settings, "sneaky-snitch")["license"] == "CC BY 4.0"
    assert sounds.describe(settings, "bruh")["source"] == "upload"             # nobody described it


def test_downloads_must_be_mp3s_of_sane_size(settings, monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>nope")))
    with pytest.raises(sounds.SoundError, match="not an MP3"):
        run(sounds._download(client, "https://x/y.mp3", settings.data_dir / "y.mp3"))
    monkeypatch.setattr(sounds, "MAX_BYTES", 10)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=MP3)))
    with pytest.raises(sounds.SoundError, match="12 MB"):
        run(sounds._download(client, "https://x/y.mp3", settings.data_dir / "y.mp3"))


def test_search_and_import(settings, web):
    fake, client = web
    found = run(sounds.search(settings, "huh", "myinstants", client))
    assert [(r["id"], r["title"]) for r in found] == [("huh-cat-21280", "Huh Cat"), ("what-s-up-9", "What's up")]
    assert found[0]["preview_url"] == "https://www.myinstants.com/media/sounds/huh-cat.mp3"
    with pytest.raises(sounds.SoundError, match="FREESOUND_API_KEY"):
        run(sounds.search(settings, "boom", "freesound", client))
    cfg = dataclasses.replace(settings, freesound_api_key="fs")
    fs = run(sounds.search(cfg, "boom", "freesound", client))
    assert [(r["id"], r["license"], r["author"]) for r in fs] == [("123", "CC0", "sfxguy")]
    query = httpx.URL(fake.requests[-1]).params
    assert query["filter"] == 'license:"Creative Commons 0"' and query["token"] == "fs"

    (sounds.folder(settings) / "huh-cat.mp3").write_bytes(MP3)
    name = run(sounds.import_sound(settings, found[0], category="reaction", client=client))
    assert name == "huh-cat-2"                                                 # named from the title, kept apart
    assert sounds.describe(settings, name)["category"] == "reaction"
    boom = run(sounds.import_sound(cfg, fs[0], name="big boom", category="nope", client=client))
    assert sounds.describe(settings, boom) == {"category": "other", "source": "freesound", "license": "CC0",
                                              "author": "sfxguy", "favorite": False}
    with pytest.raises(sounds.SoundError, match="not a Myinstants"):
        run(sounds.import_sound(settings, {"source": "myinstants", "preview_url": "https://evil.example/x.mp3"},
                                client=client))


@pytest.fixture
def dash(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    cfg = dataclasses.replace(settings, dashboard_password=PASSWORD, web_dir=web, n8n_url="http://127.0.0.1:9")
    with TestClient(create_app(cfg, run_background=False)) as c:
        assert c.post("/api/login", json={"password": PASSWORD}).status_code == 200
        yield c


def test_sound_api(dash, sample_video, monkeypatch):
    wav = sample_video.read_bytes()
    assert dash.put("/api/sounds/Vine Boom.mp4", content=wav).status_code == 422          # not an audio name
    up = dash.put("/api/sounds/boom.mp3", content=wav).json()
    assert (up["name"], up["category"], up["source"]) == ("boom", "other", "upload")
    listed = dash.get("/api/sounds").json()
    assert listed["categories"][0] == "impact" and listed["freesound"] is False
    assert listed["pack"] == {"running": False, "result": None, "size": len(sounds.MEME_PACK) + 1}
    edited = dash.patch("/api/sounds/boom", json={"category": "impact", "favorite": True}).json()
    assert (edited["category"], edited["favorite"]) == ("impact", True)
    assert dash.patch("/api/sounds/boom", json={"category": "loud"}).status_code == 422
    assert dash.patch("/api/sounds/nope", json={"favorite": True}).status_code == 404
    assert dash.get("/api/sounds/search", params={"q": "x", "source": "freesound"}).status_code == 503

    async def fake_pack(settings, client=None):
        (sounds.folder(settings) / "vine-boom.mp3").write_bytes(wav)
        return {"added": ["vine-boom"], "kept": [], "failed": {}}

    monkeypatch.setattr(sounds, "install_pack", fake_pack)
    assert dash.post("/api/sounds/packs/meme").status_code == 202
    for task in list(dash.app.state.render_tasks):
        dash.portal.call(asyncio.wait_for, asyncio.shield(task), 30)
    pack = dash.get("/api/sounds").json()["pack"]
    assert pack["running"] is False and pack["result"]["added"] == ["vine-boom"]
    assert templates.find_sound(dash.app.state.settings, "vine-boom")

    assert dash.delete("/api/sounds/boom").json() == {"ok": True}
    assert "boom" not in json.loads((sounds.folder(dash.app.state.settings) / "sounds.json").read_text())
