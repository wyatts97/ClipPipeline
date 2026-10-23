"""ffmpeg / ffprobe / yt-dlp wrappers."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional

log = logging.getLogger("clipbot.media")


class MediaError(RuntimeError):
    pass


async def run(cmd: list[str], timeout: float) -> str:
    """Run a command, return stdout; raise MediaError with stderr's tail on failure."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError(f"{cmd[0]} timed out after {int(timeout)}s")
    if proc.returncode != 0:
        tail = err.decode(errors="replace").strip().splitlines()[-6:]
        raise MediaError(f"{cmd[0]} failed: " + " | ".join(tail))
    return out.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    out = await run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(path)], timeout=60)
    try:
        return float(out.strip())
    except ValueError:
        raise MediaError(f"ffprobe returned no duration for {path.name}")


def preview_bitrate_kbps(duration: float, max_mb: int, audio_kbps: int = 96,
                         ceiling_kbps: int = 2500) -> int:
    """Video bitrate that keeps the preview under max_mb (10% headroom for the container)."""
    total_kbps = max_mb * 8 * 1024 * 0.9 / max(duration, 1.0)
    return max(200, min(ceiling_kbps, int(total_kbps - audio_kbps)))


async def make_preview(src: Path, dst: Path, max_mb: int, height: int) -> Path:
    """Small H.264 copy for Telegram (bots may upload at most 50 MB)."""
    duration = await probe_duration(src)
    kbps = preview_bitrate_kbps(duration, max_mb)
    for attempt in range(2):
        await run([
            "ffmpeg", "-y", "-v", "error", "-i", str(src),
            "-vf", f"scale=-2:min(ih\\,{height})",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", f"{kbps}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst),
        ], timeout=900)
        if dst.stat().st_size <= max_mb * 1024 * 1024:
            return dst
        kbps = int(kbps * 0.6)
        log.info("preview %s too big on attempt %d, retrying at %dk", dst.name, attempt + 1, kbps)
    raise MediaError(f"could not get {src.name} under {max_mb} MB")


async def make_thumbnail(src: Path, dst: Path, height: int = 480) -> Path:
    """A poster frame for the dashboard, one second in (or the first frame of very short clips)."""
    for seek in ("1", "0"):
        try:
            await run(["ffmpeg", "-y", "-v", "error", "-ss", seek, "-i", str(src), "-frames:v", "1",
                       "-vf", f"scale=-2:{height}", "-q:v", "4", str(dst)], timeout=120)
        except MediaError:
            continue
        if dst.exists() and dst.stat().st_size > 0:
            return dst
    raise MediaError(f"no frame could be grabbed from {src.name}")


async def download_section(url: str, start: float, end: float, out_dir: Path,
                           cookies: Optional[Path] = None, max_height: int = 1080,
                           name: str = "source") -> Path:
    """Download only [start, end] of a video with yt-dlp (cuts at the nearest keyframes).
    max_height=360 gets a small copy, enough for Gemini to watch."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{name}.*"):
        old.unlink()
    h = max_height
    cmd = [
        "yt-dlp", "--no-playlist", "--no-progress",
        "-f", f"bv*[height<={h}]+ba/b[height<={h}]/bv*+ba/b" if h >= 720 else f"b[height<={h}]/bv*[height<={h}]+ba/wv*+ba/w",
        "--download-sections", f"*{start:.0f}-{end:.0f}",
        "--merge-output-format", "mp4",
        "-o", str(out_dir / f"{name}.%(ext)s"),
    ]
    if cookies and cookies.exists():
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    await run(cmd, timeout=3 * 3600)
    files = [p for p in out_dir.glob(f"{name}.*") if p.suffix not in (".part", ".ytdl")]
    if not files:
        raise MediaError("yt-dlp finished but wrote no file")
    return max(files, key=lambda p: p.stat().st_size)


async def make_proxy(src: Path, dst: Path, start: float = 0.0, duration: Optional[float] = None,
                     height: int = 360) -> Path:
    """A small copy for Gemini to watch: one frame a second (all it samples), 360p, mono speech-rate
    audio. Encodes fast and uploads in seconds."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vf", f"fps=1,scale=-2:min(ih\\,{height})", "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
            "-c:a", "aac", "-ac", "1", "-ar", "16000", "-b:a", "32k", "-movflags", "+faststart", str(dst)]
    await run(cmd, timeout=1800)
    return dst


def slugify(text: Optional[str], max_len: int = 50) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].rstrip("-") or "clip"
