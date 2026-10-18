"""Reading OpenShorts' log of a failed job (the lines are modelled on OpenShorts' own prints)."""
from app.diagnose import failure_hint, failure_reason, retry_delay

TAIL = [
    "Traceback (most recent call last):",
    '  File "/app/main.py", line 2524, in <module>',
    "    raise RuntimeError(",
    "RuntimeError: Clip detection failed — the AI model did not return usable clips for this video.",
    "Process failed with exit code 1",
]


def test_gemini_quota():
    lines = ["🤖  Analyzing with Gemini (2-pass: score → detail)...",
             "🤖  Model: gemini-3.1-flash-lite | language: en",
             "❌ Gemini Error: 429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
             "'You exceeded your current quota, please check your plan and billing details.'}}"] + TAIL
    reason = failure_reason(lines)
    assert reason.startswith("❌ Gemini Error: 429 RESOURCE_EXHAUSTED")
    assert reason.endswith("RuntimeError: Clip detection failed — the AI model did not return usable clips for this video.")
    assert "raise RuntimeError(" not in reason and "exit code" not in reason
    assert "quota" in failure_hint(lines)


def test_bad_key():
    lines = ["❌ Gemini Error: 400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key."] + TAIL
    assert "refused the API key" in failure_hint(lines)


def test_nothing_clip_worthy():
    lines = ["🤖  Analyzing with Gemini (2-pass: score → detail)...", "⚠️ 2-pass returned no clips."] + TAIL
    assert failure_reason(lines).startswith("⚠️ 2-pass returned no clips.")
    assert "Wider" in failure_hint(lines)


def test_little_speech():
    lines = ["🔇 Only 3 word(s) of speech in 135s — switching to visual analysis.",
             "🎥  Silent video — analyzing with Gemini vision (no transcript)...",
             "⚠️ Vision pass returned no usable clips."] + TAIL
    reason = failure_reason(lines)
    assert "🔇 Only 3 word(s)" in reason and "Vision pass returned no usable clips" in reason
    assert "nothing it rated clip-worthy" in failure_hint(lines)


def test_network_error_from_the_real_log():
    # Tail of the log from job d9f1fb76 on the server (2026-10-18).
    lines = ["[132.94s -> 133.76s]  I swear on everything I love",
             "🤖  Analyzing with Gemini (2-pass: score → detail)...",
             "🤖  Model: gemini-3.1-flash-lite | language: en",
             "Built 2 scoring window(s).",
             "❌ Gemini Error: [Errno 101] Network is unreachable"] + TAIL
    assert failure_reason(lines).startswith("❌ Gemini Error: [Errno 101] Network is unreachable")
    assert "couldn't reach Google" in failure_hint(lines)
    assert retry_delay(lines) == 120


def test_retry_delays():
    assert retry_delay(["❌ Gemini Error: 429 RESOURCE_EXHAUSTED"] + TAIL) == 300
    assert retry_delay(["❌ Gemini Error: 503 UNAVAILABLE. The model is overloaded."] + TAIL) == 120
    assert retry_delay(["⚠️ 2-pass returned no clips."] + TAIL) is None
    assert retry_delay(["❌ Gemini Error: 400 INVALID_ARGUMENT. API key not valid."] + TAIL) is None


def test_transcript_words_dont_trigger_hints():
    lines = ["[10.0s -> 12.0s]  bro the server timed out, 503 again, network is unreachable lol",
             "⚠️ 2-pass returned no clips."] + TAIL
    assert "nothing it rated clip-worthy" in failure_hint(lines)
    assert retry_delay(lines) is None


def test_unknown_failure_falls_back_to_last_lines():
    lines = ["downloading", "ffmpeg exited with 234", "raise RuntimeError(", "Process failed with exit code 1"]
    assert failure_reason(lines) == "downloading | ffmpeg exited with 234"
    assert failure_hint(lines) is None
    assert failure_reason([]) == "no log output"
