"""Turning a clip without captions into the finished clip: captions, music and sound effects in
one ffmpeg run. Without captions the video stream is copied, so audio-only templates take seconds."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .media import run

VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]


@dataclass
class Music:
    path: Path
    volume: float = 0.15
    duck: bool = True       # quieter while someone talks
    start: float = 0.0      # seconds into the track


@dataclass
class Effect:
    path: Path
    t: float                # clip seconds
    volume: float = 1.0


@dataclass
class Plan:
    duration: float
    ass: Optional[Path] = None
    fonts_dir: Optional[Path] = None
    music: Optional[Music] = None
    effects: list[Effect] = field(default_factory=list)
    voice_volume: float = 1.0
    has_audio: bool = True

    @property
    def touches_audio(self) -> bool:
        return bool(self.music or self.effects or abs(self.voice_volume - 1.0) > 1e-3 or not self.has_audio)


def filter_path(path: Path) -> str:
    """A path inside a quoted filter option (Windows drive colons need escaping too)."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "")


def command(src: Path, dst: Path, plan: Plan) -> Optional[list[str]]:
    """The ffmpeg command for this plan, or None when there is nothing to change."""
    if not plan.ass and not plan.touches_audio:
        return None
    inputs = ["-i", str(src)]
    graph: list[str] = []
    vmap = "0:v"
    if plan.ass:
        fonts = f":fontsdir='{filter_path(plan.fonts_dir)}'" if plan.fonts_dir else ""
        graph.append(f"[0:v]ass=filename='{filter_path(plan.ass)}'{fonts}[v]")
        vmap = "[v]"

    amap = "0:a?"
    if plan.touches_audio:
        dur = f"{plan.duration:.3f}"
        if plan.has_audio:
            voice = f"[0:a]volume={plan.voice_volume:.3f}"
        else:
            voice = f"anullsrc=r=48000:cl=stereo,atrim=0:{dur}"
        duck = bool(plan.music and plan.music.duck and plan.has_audio)
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
    cmd += VIDEO_ARGS if plan.ass else ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if plan.touches_audio else ["-c:a", "copy"]
    cmd += ["-t", f"{plan.duration:.3f}", "-movflags", "+faststart", str(dst)]
    return cmd


async def has_audio(path: Path) -> bool:
    out = await run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                     "-of", "csv=p=0", str(path)], timeout=60)
    return bool(out.strip())


async def render(src: Path, dst: Path, plan: Plan, timeout: float = 1800) -> bool:
    """Write the finished clip to dst. False (and nothing written) when the plan changes nothing."""
    cmd = command(src, dst, plan)
    if cmd is None:
        return False
    await run(cmd, timeout=timeout)
    return True
