"""Video metadata (yt-dlp) and per-second audio loudness (ffmpeg), without saving the video."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .media import MediaError, run

SILENCE_DB = -100.0
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")
_RMS_RE = re.compile(r"RMS_level=(-?inf|-?[0-9.]+)")


async def probe(url: str, cookies: Optional[Path] = None) -> dict[str, Any]:
    """yt-dlp metadata, with the smallest stream that has audio selected as the format."""
    cmd = ["yt-dlp", "-J", "--no-playlist", "--no-warnings", "-f", "ba/worst"]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    out = await run(cmd + [url], timeout=300)
    try:
        return json.loads(out)
    except ValueError:
        raise MediaError("yt-dlp returned no metadata for this link")


async def download_audio(url: str, out_dir: Path, cookies: Optional[Path] = None) -> Path:
    """Audio-only download through yt-dlp. Used for non-HLS sources such as YouTube, where
    ffmpeg reading the media URL in one request gets throttled to a crawl."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["yt-dlp", "--no-playlist", "--no-progress", "--quiet", "--no-warnings", "-f", "ba/worst",
           "-o", str(out_dir / "audio.%(ext)s")]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    await run(cmd + [url], timeout=3 * 3600)
    files = [p for p in out_dir.glob("audio.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        raise MediaError("yt-dlp wrote no audio file")
    return files[0]


async def media_duration(url: str) -> float:
    out = await run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", url], timeout=120)
    try:
        return float(out.strip())
    except ValueError:
        raise MediaError("could not read the video's duration")


def parse_astats(text: str) -> list[float]:
    """ffmpeg ametadata output -> RMS dB for each second (index = second)."""
    levels: dict[int, float] = {}
    second = None
    for line in text.splitlines():
        pts = _PTS_RE.search(line)
        if pts:
            second = int(float(pts.group(1)))
            continue
        rms = _RMS_RE.search(line)
        if rms and second is not None:
            value = rms.group(1)
            levels[second] = SILENCE_DB if "inf" in value else max(SILENCE_DB, float(value))
    if not levels:
        return []
    return [levels.get(i, SILENCE_DB) for i in range(max(levels) + 1)]


async def loudness(stream_url: str, headers: Optional[dict[str, str]], duration: float) -> list[float]:
    """Stream the audio through ffmpeg and return RMS dB per second."""
    cmd = ["ffmpeg", "-v", "error", "-nostats"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += ["-i", stream_url, "-vn", "-ac", "1", "-af",
            "aresample=8000,asetnsamples=n=8000:p=0,"
            "astats=metadata=1:reset=1:measure_perchannel=none:measure_overall=RMS_level,"
            "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f", "null", "-"]
    out = await run(cmd, timeout=900 + duration / 5)
    levels = parse_astats(out)
    if not levels:
        raise MediaError("ffmpeg produced no loudness data")
    return levels
