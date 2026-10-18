import random
import time

import pytest

from app.chat import ChatMessage
from app.clips_api import ClipMarker
from app.scoring import StreamSignals, find_moments, is_greeting, is_hype


@pytest.mark.parametrize("text", ["KEKW", "cohhLUL", "???", "LMAOOOOO", "hahahaha", "😂😂", "clip it",
                                  "OMEGALUL OMEGALUL", "W", "no way lol"])
def test_hype(text):
    assert is_hype(text)


@pytest.mark.parametrize("text", ["nice weather today", "what game is this", "hello", "", "ok"])
def test_not_hype(text):
    assert not is_hype(text)


def test_extra_hype_words():
    assert is_hype("streamerJAM", frozenset({"streamerjam"}))


@pytest.mark.parametrize("text,expected", [("hi", True), ("hey hey", True), ("o7", True),
                                           ("bye chat", True), ("gn everyone o7", True),
                                           ("hey what game is this", False), ("KEKW", False)])
def test_greeting(text, expected):
    assert is_greeting(text) is expected


def background_chat(duration, rate_per_5s=2, seed=1):
    rng = random.Random(seed)
    words = ["what", "is this", "nice", "ok", "gaming", "true", "real", "this game", "he is cooking"]
    return [ChatMessage(t + rng.random() * 5, f"user{rng.randrange(400)}", rng.choice(words))
            for t in range(0, int(duration), 5) for _ in range(rate_per_5s)]


def spike(at, users, word, seconds=15, prefix="fan"):
    return [ChatMessage(at + i * seconds / users, f"{prefix}{i}", word) for i in range(users)]


def test_chat_spike_becomes_the_top_moment():
    msgs = background_chat(3600) + spike(1800, 60, "KEKW") + spike(2700, 25, "W", prefix="w")
    moments = find_moments(StreamSignals(3600, messages=msgs), top=5, before=90, after=45)
    first = moments[0]
    assert 1795 <= first.peak <= 1830
    assert first.start == pytest.approx(first.peak - 90) and first.end == pytest.approx(first.peak + 45)
    assert first.chat_ratio >= 5
    assert first.top_tokens[0][0] == "KEKW"
    assert any(2695 <= m.peak <= 2725 for m in moments[1:])


def test_moments_never_overlap():
    msgs = background_chat(3600) + spike(1800, 60, "KEKW") + spike(1860, 60, "LUL", prefix="b")
    moments = find_moments(StreamSignals(3600, messages=msgs), top=5, before=90, after=45)
    peaks = sorted(m.peak for m in moments)
    assert all(b - a >= 135 for a, b in zip(peaks, peaks[1:]))


def test_hello_flood_at_the_start_is_ignored():
    msgs = background_chat(3600) + spike(40, 80, "hello") + spike(60, 80, "KEKW", prefix="x")
    moments = find_moments(StreamSignals(3600, messages=msgs), top=3)
    assert all(m.peak >= 180 for m in moments)


def test_goodbye_flood_at_the_end_is_ignored():
    msgs = background_chat(3600) + spike(3550, 80, "bye chat") + spike(3560, 80, "KEKW", prefix="x")
    moments = find_moments(StreamSignals(3600, messages=msgs), top=3)
    assert all(m.peak <= 3480 for m in moments)


def test_audio_only():
    loud = [-40.0] * 3600
    for s in range(1200, 1210):
        loud[s] = -8.0
    moments = find_moments(StreamSignals(3600, loudness_db=loud), top=3)
    assert 1195 <= moments[0].peak <= 1215 and moments[0].loud
    assert moments[0].chat_ratio is None


def test_viewer_clip_marks_a_quiet_stream():
    moments = find_moments(StreamSignals(3600, loudness_db=[-30.0] * 3600,
                                         markers=[ClipMarker(900, 30, 5000)]), top=3)
    assert 895 <= moments[0].peak <= 935
    assert moments[0].clips == 1 and moments[0].clip_views == 5000


def test_youtube_most_replayed():
    heat = [{"start_time": s, "end_time": s + 30, "value": 1.0 if s == 600 else 0.1} for s in range(0, 3600, 30)]
    moments = find_moments(StreamSignals(3600, heatmap=heat), top=3)
    assert 595 <= moments[0].peak <= 635 and moments[0].replayed


def test_short_video_keeps_windows_inside_it():
    msgs = background_chat(400) + spike(380, 40, "KEKW")
    moments = find_moments(StreamSignals(400, messages=msgs), top=1)
    assert moments and moments[0].end <= 400 and moments[0].end - moments[0].start >= 60


def test_six_hour_stream_is_fast():
    msgs = background_chat(6 * 3600, rate_per_5s=4) + spike(10000, 100, "OMEGALUL")
    started = time.time()
    moments = find_moments(StreamSignals(6 * 3600, messages=msgs, loudness_db=[-30.0] * (6 * 3600)))
    assert time.time() - started < 10
    assert 9995 <= moments[0].peak <= 10030
