"""Turning a clip without captions into the finished clip: cuts, zoom punch-ins, captions, music and
sound effects in one ffmpeg run. Without cuts, zooms or captions the video stream is copied, so
audio-only templates take seconds."""
from __future__ import annotations

import array
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .media import MediaError, run

VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
ZOOM_EASE = 0.12   # seconds to punch in


@dataclass
class Music:
    path: Path
    volume: float = 0.15
    duck: bool = True       # quieter while someone talks
    start: float = 0.0      # seconds into the track


@dataclass
class Effect:
    path: Path
    t: float                # seconds on the finished clip
    volume: float = 1.0


@dataclass
class Plan:
    duration: float                                   # of the finished clip
    ass: Optional[Path] = None
    fonts_dir: Optional[Path] = None
    music: Optional[Music] = None
    effects: list[Effect] = field(default_factory=list)
    voice_volume: float = 1.0
    has_audio: bool = True
    segments: Optional[list[list[float]]] = None      # kept stretches of the source (None = all of it)
    zooms: list[tuple[float, float, float]] = field(default_factory=list)   # (start, end, scale), finished-clip time
    size: Optional[tuple[int, int]] = None            # source width/height, needed for zooms

    @property
    def touches_audio(self) -> bool:
        return bool(self.music or self.effects or abs(self.voice_volume - 1.0) > 1e-3 or not self.has_audio
                    or self.segments)

    @property
    def touches_video(self) -> bool:
        return bool(self.ass or self.segments or (self.zooms and self.size))


def filter_path(path: Path) -> str:
    """A path inside a quoted filter option (Windows drive colons need escaping too)."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "")


def zoom_filter(zooms: list[tuple[float, float, float]], size: tuple[int, int]) -> str:
    """Scale up during each zoom (quick ease in), cropped back to the frame from the centre."""
    w, h = size
    terms = "+".join(f"if(between(t,{s:.3f},{e:.3f}),{z - 1:.3f}*min(1,(t-{s:.3f})/{ZOOM_EASE}),0)"
                     for s, e, z in zooms)
    factor = f"(1+{terms})"
    return (f"scale=w='2*trunc(iw*{factor}/2)':h='2*trunc(ih*{factor}/2)':eval=frame,"
            f"crop={w}:{h},setsar=1")


def command(src: Path, dst: Path, plan: Plan) -> Optional[list[str]]:
    """The ffmpeg command for this plan, or None when there is nothing to change."""
    if not plan.touches_video and not plan.touches_audio:
        return None
    inputs = ["-i", str(src)]
    graph: list[str] = []
    vin, ain = "[0:v]", "[0:a]" if plan.has_audio else None
    if plan.segments:
        parts = []
        for i, (s, e) in enumerate(plan.segments):
            graph.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
            if plan.has_audio:
                graph.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
                parts.append(f"[v{i}][a{i}]")
            else:
                parts.append(f"[v{i}]")
        audio_out = "[acut]" if plan.has_audio else ""
        graph.append(f"{''.join(parts)}concat=n={len(plan.segments)}:v=1:a={int(plan.has_audio)}[vcut]{audio_out}")
        vin, ain = "[vcut]", ("[acut]" if plan.has_audio else None)

    chain = []
    if plan.zooms and plan.size:
        chain.append(zoom_filter(plan.zooms, plan.size))
    if plan.ass:
        fonts = f":fontsdir='{filter_path(plan.fonts_dir)}'" if plan.fonts_dir else ""
        chain.append(f"ass=filename='{filter_path(plan.ass)}'{fonts}")
    if chain:
        graph.append(f"{vin}{','.join(chain)}[v]")
        vmap = "[v]"
    else:
        vmap = vin if plan.segments else "0:v"

    amap = "0:a?"
    if plan.touches_audio:
        dur = f"{plan.duration:.3f}"
        voice = (f"{ain}volume={plan.voice_volume:.3f}" if ain
                 else f"anullsrc=r=48000:cl=stereo,atrim=0:{dur}")
        duck = bool(plan.music and plan.music.duck and ain)
        graph.append(f"{voice},asplit=2[voice][side]" if duck else f"{voice}[voice]")
        mix = ["[voice]"]
        index = 1
        if plan.music:
            inputs += ["-stream_loop", "-1", "-ss", f"{plan.music.start:.3f}", "-i", str(plan.music.path)]
            fade = max(0.0, plan.duration - 1.0)
            graph.append(f"[{index}:a]atrim=0:{dur},asetpts=PTS-STARTPTS,volume={plan.music.volume:.3f},"
                         f"afade=t=out:st={fade:.3f}:d=1[music0]")
            if duck:
                graph.append("[music0][side]sidechaincompress=threshold=0.02:ratio=8:attack=20:release=400[music]")
            else:
                graph.append("[music0]anull[music]")
            mix.append("[music]")
            index += 1
        for k, effect in enumerate(plan.effects):
            inputs += ["-i", str(effect.path)]
            delay = int(round(effect.t * 1000))
            graph.append(f"[{index}:a]volume={effect.volume:.3f},adelay=delays={delay}:all=1[fx{k}]")
            mix.append(f"[fx{k}]")
            index += 1
        if len(mix) > 1:
            graph.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:dropout_transition=0:normalize=0,"
                         "alimiter=limit=0.95[a]")
        else:
            graph.append("[voice]anull[a]")
        amap = "[a]"

    cmd = ["ffmpeg", "-y", "-v", "error", *inputs]
    if graph:
        cmd += ["-filter_complex", ";".join(graph)]
    cmd += ["-map", vmap, "-map", amap]
    cmd += VIDEO_ARGS if plan.touches_video else ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if plan.touches_audio else ["-c:a", "copy"]
    cmd += ["-t", f"{plan.duration:.3f}", "-movflags", "+faststart", str(dst)]
    return cmd


async def has_audio(path: Path) -> bool:
    out = await run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                     "-of", "csv=p=0", str(path)], timeout=60)
    return bool(out.strip())


async def video_size(path: Path) -> Optional[tuple[int, int]]:
    out = await run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                     "-of", "csv=p=0:s=x", str(path)], timeout=60)
    try:
        w, h = out.strip().split("x")[:2]
        return int(w), int(h)
    except ValueError:
        return None


async def ass_mask(ass: Path, fonts_dir: Optional[Path], size: tuple[int, int]) -> bytes:
    """The first frame of an ASS file drawn on black, gray, one byte a pixel: where libass puts things."""
    fonts = f":fontsdir='{filter_path(fonts_dir)}'" if fonts_dir else ""
    return await run_bytes(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={size[0]}x{size[1]}:d=1",
                            "-vf", f"ass=filename='{filter_path(ass)}'{fonts}", "-frames:v", "1",
                            "-f", "rawvideo", "-pix_fmt", "gray", "-"], timeout=120)


async def peaks(path: Path, per_second: int = 50) -> list[float]:
    """Loudness peaks (0-1) for the editor's waveform, per_second values per second of audio."""
    rate = per_second * 80
    proc = await run_bytes(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(rate),
                            "-f", "s16le", "-"], timeout=300)
    samples = array.array("h")
    samples.frombytes(proc[: len(proc) - len(proc) % 2])
    step = 80
    return [round(max((abs(x) for x in samples[i:i + step]), default=0) / 32768, 3)
            for i in range(0, len(samples), step)]


async def run_bytes(cmd: list[str], timeout: float) -> bytes:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError(f"{cmd[0]} timed out after {int(timeout)}s")
    if proc.returncode != 0:
        raise MediaError(f"{cmd[0]} failed: " + " | ".join(err.decode(errors="replace").strip().splitlines()[-4:]))
    return out


async def render(src: Path, dst: Path, plan: Plan, timeout: float = 1800) -> bool:
    """Write the finished clip to dst. False (and nothing written) when the plan changes nothing."""
    cmd = command(src, dst, plan)
    if cmd is None:
        return False
    await run(cmd, timeout=timeout)
    return True
