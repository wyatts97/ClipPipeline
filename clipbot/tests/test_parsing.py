import pytest

from app.parsing import format_timestamp, parse_message, parse_timestamp


@pytest.mark.parametrize("value,seconds", [
    ("1:02:03", 3723), ("62:03", 3723), ("0:45", 45), ("1h2m3s", 3723),
    ("90m", 5400), ("45s", 45), ("2h", 7200), ("1h30m", 5400),
])
def test_parse_timestamp(value, seconds):
    assert parse_timestamp(value) == seconds


def test_format_timestamp():
    assert format_timestamp(3723) == "1:02:03"
    assert format_timestamp(75) == "1:15"


@pytest.mark.parametrize("text", ["", "hello", "/start", "/help", "/help@ClipBot"])
def test_help(text):
    assert parse_message(text).kind == "help"


def test_status_with_bot_suffix():
    assert parse_message("/status@ClipBot").kind == "status"


def test_plain_url():
    cmd = parse_message("https://www.twitch.tv/videos/123456789")
    assert cmd.kind == "clip"
    assert cmd.url == "https://www.twitch.tv/videos/123456789"
    assert cmd.start is None and cmd.end is None and cmd.target_clips is None


def test_url_with_range_and_clips():
    cmd = parse_message("https://youtu.be/abc123, 1:02:00 - 1:12:30 clips=5")
    assert cmd.kind == "clip"
    assert cmd.url == "https://youtu.be/abc123"
    assert (cmd.start, cmd.end) == (3720, 4350)
    assert cmd.target_clips == 5


def test_range_before_url_and_hms_style():
    cmd = parse_message("1h2m-1h12m https://kick.com/video/xyz")
    assert (cmd.start, cmd.end) == (3720, 4320)


def test_timestamp_inside_url_is_not_a_range():
    cmd = parse_message("https://www.twitch.tv/videos/1?t=1h2m3s")
    assert cmd.kind == "clip" and cmd.start is None


@pytest.mark.parametrize("text,needle", [
    ("https://youtu.be/x 10:00-5:00", "ends before"),
    ("https://youtu.be/x 10:00-10:30", "at least"),
    ("https://youtu.be/x 0:00-3:00:00", "limit is 90"),
    ("https://youtu.be/x clips=40", "between 1 and 15"),
])
def test_invalid(text, needle):
    cmd = parse_message(text)
    assert cmd.kind == "invalid"
    assert needle in cmd.error
