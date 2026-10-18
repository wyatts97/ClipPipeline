import dataclasses

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import HELP, create_app
from app.openshorts import OpenShorts

AUTH = {"X-Clipbot-Token": "test-token"}


def make_client(settings):
    dead_end = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    app = create_app(settings, openshorts=OpenShorts(settings.openshorts_url, dead_end),
                     run_background=False)
    return TestClient(app)


@pytest.fixture
def client(settings):
    with make_client(settings) as c:
        yield c


def test_token_required(client):
    assert client.post("/messages", json={"chat_id": 111, "text": "/help"}).status_code == 401
    assert client.post("/messages", json={"chat_id": 111, "text": "/help"},
                       headers={"X-Clipbot-Token": "wrong"}).status_code == 401
    assert client.get("/health").json() == {"ok": True}


def test_setup_mode_tells_you_your_chat_id(settings):
    open_settings = dataclasses.replace(settings, allowed_chat_ids=frozenset())
    with make_client(open_settings) as c:
        reply = c.post("/messages", json={"chat_id": 42, "text": "/start"}, headers=AUTH).json()["reply"]
    assert "ALLOWED_CHAT_IDS=42" in reply


def test_strangers_are_ignored(client):
    assert client.post("/messages", json={"chat_id": 999, "text": "https://youtu.be/x"},
                       headers=AUTH).json() == {"reply": None}
    assert client.post("/decisions", json={"chat_id": 999, "data": "ok:abc"},
                       headers=AUTH).json() == {"toast": "Not allowed", "reply": None}


def test_help_status_and_queue(client):
    assert client.post("/messages", json={"chat_id": 111, "text": "/start"},
                       headers=AUTH).json()["reply"] == HELP
    assert "No jobs yet" in client.post("/messages", json={"chat_id": 111, "text": "/status"},
                                        headers=AUTH).json()["reply"]

    reply = client.post("/messages", json={"chat_id": 111,
                                           "text": "https://www.twitch.tv/videos/1 1:00:00-1:10:00 clips=4"},
                        headers=AUTH).json()["reply"]
    assert "queued: 4 clip(s) from 1:00:00–1:10:00 (10:00)" in reply

    status = client.post("/messages", json={"chat_id": 111, "text": "/status"}, headers=AUTH).json()["reply"]
    assert "queued" in status and "twitch.tv/videos/1" in status

    bad = client.post("/messages", json={"chat_id": 111, "text": "https://youtu.be/x 5:00-1:00"},
                      headers=AUTH).json()["reply"]
    assert bad.startswith("⚠️")


def test_scan_and_watch_commands(client):
    def say(text):
        return client.post("/messages", json={"chat_id": 111, "text": text}, headers=AUTH).json()["reply"]

    assert say("/scan").startswith("⚠️ Usage: /scan")
    assert say("/scan https://www.twitch.tv/videos/555").startswith("🔎 Scan ")
    assert "• scan " in say("/status")
    assert say("/watch").startswith("⚠️ Usage: /watch")
    assert say("/watching").startswith("Not watching")
    assert say("/unwatch twitch:nobody").startswith("I wasn't watching")
    assert client.post("/decisions", json={"chat_id": 111, "data": "sc:nope:1"},
                       headers=AUTH).json() == {"toast": "Unknown moment", "reply": None}


def test_unknown_preview_is_404(client):
    assert client.get("/previews/nope", headers=AUTH).status_code == 404
