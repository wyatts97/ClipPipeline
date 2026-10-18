import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory) -> Path:
    """A 3-second vertical test clip with audio, made by ffmpeg."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=540x960:rate=30:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True)
    return path


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        openshorts_url="http://openshorts:8000",
        openshorts_output_dir=tmp_path / "os-output",
        notify_url="http://n8n:5678/webhook/clipbot-events",
        notify_token="test-token",
        self_url="http://clipbot:8080",
        allowed_chat_ids=frozenset({111}),
        target_clips=3,
        clip_min_seconds=15,
        clip_max_seconds=60,
        layouts="auto",
        auto_hook=True,
        captions=True,
        poll_seconds=1,
        job_timeout_minutes=360,
        max_range_minutes=90,
        preview_max_mb=45,
        preview_height=640,
    )
