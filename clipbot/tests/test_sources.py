"""Chat/loudness parsers, link detection and the viewer-clip APIs (mocked HTTP)."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app import clips_api
from app.chat import parse_twitch_downloader, parse_youtube_live_chat
from app.platforms import Channel, Vod, detect_channel, detect_vod, timestamp_link
from app.signals import SILENCE_DB, parse_astats


def test_parse_twitch_downloader(tmp_path):
    path = tmp_path / "chat.json"
    path.write_text(json.dumps({"comments": [
        {"content_offset_seconds": 12.5, "commenter": {"_id": "1", "name": "a"}, "message": {"body": "KEKW"}},
        {"content_offset_seconds": 30, "commenter": {"name": "b"}, "message": {"body": "hi"}},
    ]}), encoding="utf-8")
    msgs = parse_twitch_downloader(path)
    assert [(m.t, m.author, m.text) for m in msgs] == [(12.5, "1", "KEKW"), (30.0, "b", "hi")]


def yt_line(ms, runs, author="UC1", paid=False):
    kind = "liveChatPaidMessageRenderer" if paid else "liveChatTextMessageRenderer"
    return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms), "actions": [
        {"addChatItemAction": {"item": {kind: {"message": {"runs": runs},
                                               "authorExternalChannelId": author}}}}]}})


def test_parse_youtube_live_chat(tmp_path):
    path = tmp_path / "chat.live_chat.json"
    path.write_text("\n".join([
        yt_line(1500, [{"text": "no way "}, {"emoji": {"emojiId": "😂", "shortcuts": [":joy:"]}}]),
        yt_line(2000, [{"emoji": {"emojiId": "UCx/abc", "shortcuts": [":_lud:"], "isCustomEmoji": True}}], "UC2"),
        yt_line(2500, [{"text": "take my money"}], "UC3", paid=True),
        json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "3000", "actions": [
            {"addLiveChatTickerItemAction": {}}]}}),
        "not json",
    ]), encoding="utf-8")
    msgs = parse_youtube_live_chat(path)
    assert [(m.t, m.author, m.text) for m in msgs] == [
        (1.5, "UC1", "no way 😂"), (2.0, "UC2", ":_lud:"), (2.5, "UC3", "take my money")]


def test_parse_astats():
    out = "\n".join([
        "frame:0    pts:0       pts_time:0", "lavfi.astats.Overall.RMS_level=-30.5",
        "frame:1    pts:8000    pts_time:1", "lavfi.astats.Overall.RMS_level=-inf",
        "frame:2    pts:16000   pts_time:3", "lavfi.astats.Overall.RMS_level=-12.25",
    ])
    assert parse_astats(out) == [-30.5, SILENCE_DB, SILENCE_DB, -12.25]
    assert parse_astats("") == []


@pytest.mark.parametrize("url,platform,vod_id", [
    ("https://www.twitch.tv/videos/2900742360", "twitch", "2900742360"),
    ("https://twitch.tv/videos/123?t=1h2m", "twitch", "123"),
    ("https://kick.com/xqc/videos/4bf8def5-b4cd-4084-b875-c47f781215e1", "kick",
     "4bf8def5-b4cd-4084-b875-c47f781215e1"),
    ("https://www.youtube.com/watch?v=zn1dXa6W7t4&t=5", "youtube", "zn1dXa6W7t4"),
    ("https://youtu.be/zn1dXa6W7t4", "youtube", "zn1dXa6W7t4"),
    ("https://www.youtube.com/live/zn1dXa6W7t4", "youtube", "zn1dXa6W7t4"),
    ("https://example.com/video.mp4", "other", None),
])
def test_detect_vod(url, platform, vod_id):
    vod = detect_vod(url)
    assert (vod.platform, vod.vod_id) == (platform, vod_id)


@pytest.mark.parametrize("text,expected", [
    ("https://www.twitch.tv/lirik", Channel("twitch", "lirik")),
    ("twitch.tv/Lirik/", Channel("twitch", "lirik")),
    ("https://kick.com/xqc", Channel("kick", "xqc")),
    ("https://www.youtube.com/@ludwig/streams", Channel("youtube", "@ludwig")),
    ("https://youtube.com/channel/UCrPseYLGpNygVi34QpGNqpA", Channel("youtube", "channel/UCrPseYLGpNygVi34QpGNqpA")),
    ("twitch:lirik", Channel("twitch", "lirik")),
    ("yt ludwig", Channel("youtube", "@ludwig")),
    ("https://www.twitch.tv/videos/123", None),
    ("https://kick.com/video/abc", None),
    ("lirik", None),
])
def test_detect_channel(text, expected):
    assert detect_channel(text) == expected


def test_timestamp_links():
    assert timestamp_link(Vod("twitch", "123", "x"), 3725) == "https://www.twitch.tv/videos/123?t=1h2m5s"
    assert timestamp_link(Vod("youtube", "abcdefghijk", "x"), 90.7) == "https://youtu.be/abcdefghijk?t=90"
    assert timestamp_link(Vod("kick", "u", "https://kick.com/xqc/videos/u?foo=1"), 60) == \
        "https://kick.com/xqc/videos/u?t=60"
    assert timestamp_link(Vod("other", None, "https://e.com/v.mp4"), 60) == "https://e.com/v.mp4"


def test_kick_clip_markers_pages_and_filters():
    start = datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc)
    pages = {
        None: {"clips": [
            {"livestream_id": "999", "created_at": "2026-10-16T01:00:00Z", "duration": 30, "view_count": 5},
            {"livestream_id": "777", "created_at": "2026-10-15T21:00:30.5Z", "duration": 30, "view_count": 120},
        ], "nextCursor": {"view": 0, "id": "clip_2"}},
        '{"view":0,"id":"clip_2"}': {"clips": [
            {"livestream_id": "777", "created_at": "2026-10-15T20:10:00Z", "duration": 20, "views": 7},
            {"livestream_id": "666", "created_at": "2026-10-15T19:00:00Z", "duration": 20, "views": 1},
        ], "nextCursor": {"view": 0, "id": "clip_3"}},
    }
    seen = []

    def handler(request):
        cursor = request.url.params.get("cursor")
        seen.append(cursor)
        return httpx.Response(200, json=pages[cursor])

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await clips_api.kick_clip_markers(client, "xqc", "777", start)

    markers = asyncio.run(go())
    assert seen == [None, '{"view":0,"id":"clip_2"}']  # stopped once clips predate the stream
    assert [(m.t, m.duration, m.views) for m in markers] == [(3600.5, 30.0, 120), (580.0, 20.0, 7)]


def test_twitch_clip_markers():
    def handler(request):
        path = request.url.path
        if path == "/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok" and request.headers["Client-Id"] == "cid"
        if path == "/helix/videos":
            return httpx.Response(200, json={"data": [
                {"user_id": "42", "created_at": "2026-10-15T20:00:00Z", "duration": "3h2m1s"}]})
        if path == "/helix/clips":
            assert request.url.params["ended_at"] == "2026-10-16T00:02:01Z"
            return httpx.Response(200, json={"data": [
                {"video_id": "555", "vod_offset": 1234, "duration": 28.5, "view_count": 900},
                {"video_id": "555", "vod_offset": None, "duration": 30, "view_count": 50},
                {"video_id": "999", "vod_offset": 10, "duration": 30, "view_count": 50},
            ], "pagination": {}})
        return httpx.Response(404)

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await clips_api.twitch_clip_markers(client, "555", "cid", "secret")

    assert [(m.t, m.duration, m.views) for m in asyncio.run(go())] == [(1234.0, 28.5, 900)]
