"""Turning a clip without captions into the finished clip: cuts, freeze frames, transitions, zooms,
captions, music and sound effects in one ffmpeg run. Without cuts, zooms or captions the video stream is copied, so
audio-only templates take seconds."""
from __future__ import annotations

import array
import asyncio
import math
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from .media import MediaError, run

VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
DRAFT_ARGS = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30", "-pix_fmt", "yuv420p"]
DRAFT_HEIGHT = 854   # a draft render is 480p: quick to make, enough to check timing
ZOOM_EASE = 0.12     # seconds to punch in
SHAKE_EASE = 0.08    # a shake punches in faster
SMOOTH_EASE = 0.4    # the longest ease in (and out) of a smooth zoom
SHAKE = 0.03         # how far a shake moves the picture, a share of the frame's width
# editing.SMOOTH_TRANSITIONS -> ffmpeg xfade transitions
XFADE = {"fade": "fade", "white": "fadewhite", "black": "fadeblack", "slideleft": "slideleft",
         "slideright": "slideright", "slideup": "slideup", "slidedown": "slidedown", "wipe": "wipeleft",
         "zoomin": "zoomin", "circle": "circleopen", "pixelize": "pixelize", "blur": "hblur"}
AUDIO_FORMAT = "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
PICTURE_EASE = 0.3   # seconds a picture takes to come in or go


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
    zooms: list[dict] = field(default_factory=list)   # editing zooms ({s, e, scale, style, x, y}), finished-clip time
    size: Optional[tuple[int, int]] = None            # source width/height, needed for zooms
    timeline: Optional[list[dict]] = None             # editing.timeline, with freezes or smooth transitions
    fps: str = "30"                                   # the source's frame rate, for the timeline
    gray: list[tuple[float, float]] = field(default_factory=list)   # black-and-white stretches
    flashes: list[float] = field(default_factory=list)              # cuts that flash white
    glitches: list[float] = field(default_factory=list)             # cuts that glitch
    draft: bool = False                                             # quick and 480p (the editor's Draft render)
    # Pictures on top: editing overlays (finished-clip time) plus "path", "size" (its pixels) and "gif".
    overlays: list[dict] = field(default_factory=list)

    @property
    def touches_audio(self) -> bool:
        return bool(self.music or self.effects or abs(self.voice_volume - 1.0) > 1e-3 or not self.has_audio
                    or self.segments or self.timeline)

    @property
    def touches_video(self) -> bool:
        return bool(self.ass or self.segments or self.timeline or (self.zooms and self.size) or self.gray
                    or self.flashes or self.glitches or self.draft or (self.overlays and self.size))


def filter_path(path: Path) -> str:
    """A path inside a quoted filter option (Windows drive colons need escaping too)."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "")


def _ease(v: str) -> str:
    """Smoothstep of v (clipped to 0-1): slow, fast, slow."""
    c = f"clip({v},0,1)"
    return f"({c}*{c}*(3-2*{c}))"


def zoom_curve(z: dict) -> str:
    """How far into its zoom (0-1) a zoom is at time t, by style (lib/edit.ts zoomCurve is the same)."""
    s, e = z["s"], z["e"]
    d = max(e - s, 0.001)
    u = f"(t-{s:.3f})"
    style = z.get("style", "punch")
    if style == "smooth":
        a = min(SMOOTH_EASE, d / 3)
        return f"{_ease(f'{u}/{a:.3f}')}*{_ease(f'({e:.3f}-t)/{a:.3f}')}"
    if style == "push":
        return f"clip({u}/{d:.3f},0,1)"
    if style == "out":
        return f"(1-{_ease(f'{u}/{d:.3f}')})"
    return f"min(1,{u}/{SHAKE_EASE if style == 'shake' else ZOOM_EASE})"


def zoom_filter(zooms: list[dict], size: tuple[int, int]) -> str:
    """Scale up during each zoom, then crop the frame back out around its focus point (shaking it for a
    shake). The crop's x/y come from the known frame size: ffmpeg 7.1's crop doesn't see the per-frame
    size scale makes (its in_w stays the first frame's), so its own centring would stick to the corner."""
    w, h = size

    def during(z: dict, then: str, otherwise: str = "0") -> str:
        return f"if(between(t,{z['s']:.3f},{z['e']:.3f}),{then},{otherwise})"

    factor = "(1+" + "+".join(during(z, f"{z['scale'] - 1:.3f}*{zoom_curve(z)}") for z in zooms) + ")"
    sw, sh = f"(2*trunc({w}*{factor}/2))", f"(2*trunc({h}*{factor}/2))"
    fx = fy = "0.5"
    if any(z.get("x", 0.5) != 0.5 or z.get("y", 0.5) != 0.5 for z in zooms):
        for z in reversed(zooms):                       # the first zoom on screen picks the point
            fx, fy = during(z, f"{z.get('x', 0.5):.3f}", fx), during(z, f"{z.get('y', 0.5):.3f}", fy)
    sx = sy = ""
    for z in zooms:
        if z.get("style") == "shake":
            u, d, amp = f"(t-{z['s']:.3f})", max(z["e"] - z["s"], 0.001), SHAKE * w
            fade = f"{amp:.1f}*(1-{u}/{d:.3f})"
            sx += "+" + during(z, f"{fade}*(sin({2 * math.pi * 9:.2f}*{u})+0.5*sin({2 * math.pi * 23:.2f}*{u}))/1.5")
            sy += "+" + during(z, f"{fade}*(sin({2 * math.pi * 7:.2f}*{u}+1.3)+0.5*sin({2 * math.pi * 19:.2f}*{u}))/1.5")
    x = f"clip({fx}*{sw}-{w / 2:g}{sx},0,{sw}-{w})"
    y = f"clip({fy}*{sh}-{h / 2:g}{sy},0,{sh}-{h})"
    return f"scale=w='{sw}':h='{sh}':eval=frame,crop=w={w}:h={h}:x='{x}':y='{y}',setsar=1"


def command(src: Path, dst: Path, plan: Plan) -> Optional[list[str]]:
    """The ffmpeg command for this plan, or None when there is nothing to change."""
    if not plan.touches_video and not plan.touches_audio:
        return None
    inputs = ["-i", str(src)]
    graph: list[str] = []
    vin, ain = "[0:v]", "[0:a]" if plan.has_audio else None
    if plan.timeline:
        vin, ain = timeline_graph(graph, plan)
    elif plan.segments:
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

    # The picture: zooms and accents, then pictures on top, then captions and the hook over everything.
    chain = []
    if plan.zooms and plan.size:
        chain.append(zoom_filter(plan.zooms, plan.size))
    chain += accent_filters(plan)
    current = vin
    if chain:
        graph.append(f"{vin}{','.join(chain)}[vz]")
        current = "[vz]"
    index = 1                                       # the next input: pictures first, then music and effects
    if plan.overlays and plan.size:
        for k, o in enumerate(plan.overlays):
            if o.get("gif"):
                inputs += ["-ignore_loop", "0", "-t", f"{plan.duration:.3f}", "-i", str(o["path"])]
            else:
                inputs += ["-loop", "1", "-framerate", "30", "-t", f"{plan.duration:.3f}", "-i", str(o["path"])]
            picture, place = overlay_graph(o, plan.size)
            graph.append(f"[{index}:v]{picture}[ov{k}]")
            graph.append(f"{current}[ov{k}]{place}[vo{k}]")
            current = f"[vo{k}]"
            index += 1
    after = []
    if plan.ass:
        fonts = f":fontsdir='{filter_path(plan.fonts_dir)}'" if plan.fonts_dir else ""
        after.append(f"ass=filename='{filter_path(plan.ass)}'{fonts}")
    if plan.draft:
        after.append(f"scale=-2:{DRAFT_HEIGHT}")
    if after:
        graph.append(f"{current}{','.join(after)}[v]")
        vmap = "[v]"
    elif current != vin:
        vmap = current
    else:
        vmap = vin if (plan.segments or plan.timeline) else "0:v"

    amap = "0:a?"
    if plan.touches_audio:
        dur = f"{plan.duration:.3f}"
        voice = (f"{ain}volume={plan.voice_volume:.3f}" if ain
                 else f"anullsrc=r=48000:cl=stereo,atrim=0:{dur}")
        duck = bool(plan.music and plan.music.duck and ain)
        graph.append(f"{voice},asplit=2[voice][side]" if duck else f"{voice}[voice]")
        mix = ["[voice]"]
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
    cmd += (DRAFT_ARGS if plan.draft else VIDEO_ARGS) if plan.touches_video else ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "160k"] if plan.touches_audio else ["-c:a", "copy"]
    cmd += ["-t", f"{plan.duration:.3f}", "-movflags", "+faststart", str(dst)]
    return cmd


def timeline_graph(graph: list[str], plan: Plan) -> tuple[str, Optional[str]]:
    """Each kept part with its freezes (one frame held, silence under it), made exactly as long as the
    timeline says; then the parts joined: concat at a plain cut, xfade and acrossfade over a smooth
    transition. Returns the joined video and audio labels."""
    rate = Fraction(plan.fps)
    frame = 1 / float(rate)
    audio = plan.has_audio
    n, length, joined = 0, 0.0, ("", "")
    for i, seg in enumerate(plan.timeline or []):
        pieces = []
        marks = [seg["s"], *(f["t"] for f in seg["freezes"]), seg["e"]]
        for k in range(len(marks) - 1):
            a, b = marks[k], marks[k + 1]
            if b - a >= frame / 2:
                graph.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[p{n}]")
                if audio:
                    graph.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,{AUDIO_FORMAT}[q{n}]")
                pieces.append(n)
                n += 1
            if k < len(seg["freezes"]):
                hold = seg["freezes"][k]["d"]
                graph.append(f"[0:v]trim=start={b:.3f},setpts=PTS-STARTPTS,trim=end_frame=1,"
                             f"tpad=stop_mode=clone:stop_duration={hold:.3f}[p{n}]")
                if audio:
                    graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{hold:.3f},{AUDIO_FORMAT}[q{n}]")
                pieces.append(n)
                n += 1
        frames = max(1, round(seg["len"] * float(rate)))
        seconds = frames / float(rate)
        streams = "".join(f"[p{j}]" + (f"[q{j}]" if audio else "") for j in pieces)
        graph.append(f"{streams}concat=n={len(pieces)}:v=1:a={int(audio)}[c{i}]" + (f"[ca{i}]" if audio else ""))
        # Exactly `frames` long (a held frame pads a short part), so xfade's offsets land right.
        graph.append(f"[c{i}]fps={plan.fps},setsar=1,tpad=stop_mode=clone:stop_duration=1,trim=end_frame={frames}[s{i}]")
        if audio:
            graph.append(f"[ca{i}]apad=whole_dur={seconds:.4f},atrim=0:{seconds:.4f}[sa{i}]")
        if i == 0:
            joined, length = (f"[s{i}]", f"[sa{i}]" if audio else ""), seconds
            continue
        v, a = joined
        x = seg["x"]
        if x > 0:
            kind = XFADE.get((seg.get("into") or {}).get("type"), "fade")
            graph.append(f"{v}[s{i}]xfade=transition={kind}:duration={x:.3f}:offset={max(0.0, length - x):.4f}[j{i}]")
            if audio:
                graph.append(f"{a}[sa{i}]acrossfade=d={x:.3f}:c1=tri:c2=tri[ja{i}]")
            length += seconds - x
        else:      # concat's output counts in microseconds: back to frames, as the next xfade needs
            graph.append(f"{v}{a}[s{i}]{f'[sa{i}]' if audio else ''}concat=n=2:v=1:a={int(audio)}[k{i}]"
                         + (f"[ja{i}]" if audio else ""))
            graph.append(f"[k{i}]fps={plan.fps}[j{i}]")
            length += seconds
        joined = (f"[j{i}]", f"[ja{i}]" if audio else "")
    return joined[0], (joined[1] or None)


def overlay_graph(o: dict, size: tuple[int, int]) -> tuple[str, str]:
    """One picture: the filters that size, turn and fade it (on its own input, which runs on the finished
    clip's clock), and the overlay that puts it on the frame. Its box only changes size inside a fixed
    canvas (pad), and x/y come from known sizes, as ffmpeg 7.1's overlay keeps the first frame's."""
    w, h = size
    s, e = o["s"], o["e"]
    iw, ih = o.get("size") or (1, 1)
    pw = max(2, round(o["w"] * w))
    ph = max(2, round(pw * ih / max(1, iw)))
    u, v = f"clip((t-{s:.3f})/{PICTURE_EASE},0,1)", f"clip(({e:.3f}-t)/{PICTURE_EASE},0,1)"
    enter, leave, motion = o.get("enter", "none"), o.get("exit", "none"), o.get("motion", "none")

    def grow(kind: str, p: str) -> Optional[str]:
        if kind == "pop":        # overshoot to 1.2, settle at 1
            return f"if(lt({p},0.7),1.2*{p}/0.7,1.2-0.2*({p}-0.7)/0.3)"
        return _ease(p) if kind in ("zoom", "spin") else None

    sizes = [x for x in (grow(enter, u), grow(leave, v)) if x]
    if motion == "pulse":
        sizes.append(f"(1+0.08*sin({2 * math.pi * 1.5:.3f}*t))")
    most = (1.2 if "pop" in (enter, leave) else 1.0) * (1.08 if motion == "pulse" else 1.0)
    turn = [f"{math.radians(o.get('rot', 0)):.4f}"] if o.get("rot") else []
    if motion == "spin":
        turn.append(f"{math.pi:.4f}*(t-{s:.3f})")                       # half a turn a second
    if enter == "spin":
        turn.append(f"-{2 * math.pi:.4f}*(1-{_ease(u)})")
    if leave == "spin":
        turn.append(f"{2 * math.pi:.4f}*(1-{_ease(v)})")

    steps = ["format=rgba", f"scale={pw}:{ph}"]
    bw, bh = pw, ph
    if turn:                                       # turn inside a square that fits it at any angle
        bw = bh = math.ceil(math.hypot(pw, ph))
        steps.append(f"rotate=a='{'+'.join(turn)}':c=none:ow={bw}:oh={bh}")
    if sizes:
        f = "*".join(sizes)
        cw, ch = math.ceil(bw * most) + 2, math.ceil(bh * most) + 2
        steps += [f"scale=w='max(1,trunc({bw}*{f}))':h='max(1,trunc({bh}*{f}))':eval=frame",
                  f"pad=w={cw}:h={ch}:x='(ow-iw)/2':y='(oh-ih)/2':color=0x00000000:eval=frame"]
        bw, bh = cw, ch
    if o.get("opacity", 1) < 1:
        steps.append(f"colorchannelmixer=aa={o['opacity']:.3f}")
    if enter == "fade":
        steps.append(f"fade=t=in:st={s:.3f}:d={PICTURE_EASE}:alpha=1")
    if leave == "fade":
        steps.append(f"fade=t=out:st={max(s, e - PICTURE_EASE):.3f}:d={PICTURE_EASE}:alpha=1")

    dx, dy = [], []
    slide = {"slide-left": (-w, 0), "slide-right": (w, 0), "slide-up": (0, h), "slide-down": (0, -h)}
    if enter in slide:          # comes in from that side
        sx, sy = slide[enter]
        if sx:
            dx.append(f"{sx}*(1-{_ease(u)})")
        else:
            dy.append(f"{sy}*(1-{_ease(u)})")
    if leave in slide:          # goes out that way
        sx, sy = slide[leave]
        if sx:
            dx.append(f"{sx}*(1-{_ease(v)})")
        else:
            dy.append(f"{-sy}*(1-{_ease(v)})")
    if motion == "float":
        dy.append(f"{0.015 * h:.1f}*sin({math.pi:.4f}*t)")
    if motion == "shake":
        dx.append(f"{0.012 * w:.1f}*sin({2 * math.pi * 13:.2f}*t)")
        dy.append(f"{0.012 * w:.1f}*sin({2 * math.pi * 11:.2f}*t+1)")
    x = f"{o['x'] * w - bw / 2:.1f}" + "".join(f"+{d}" for d in dx)
    y = f"{o['y'] * h - bh / 2:.1f}" + "".join(f"+{d}" for d in dy)
    place = f"overlay=x='{x}':y='{y}':enable='between(t,{s:.3f},{e:.3f})':eval=frame:eof_action=pass"
    return ",".join(steps), place


def _during(ranges: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)


def accent_filters(plan: Plan) -> list[str]:
    """Black and white over gray freezes, a white flash or an RGB glitch on a cut (finished-clip times)."""
    out = []
    if plan.gray:
        out.append(f"hue=s=0:enable='{_during(plan.gray)}'")
    if plan.flashes:       # up in 0.06 s, back down over 0.3 s
        terms = "+".join(f"clip(min((t-{c - 0.06:.3f})/0.06,1-(t-{c:.3f})/0.3),0,1)" for c in plan.flashes)
        out.append(f"eq=brightness='0.85*min(1,{terms})':eval=frame")
    if plan.glitches:
        on = _during([(max(0.0, c - 0.08), c + 0.16) for c in plan.glitches])
        out.append(f"chromashift=cbh=-14:crh=14:cbv=4:crv=-4:enable='{on}',noise=alls=30:allf=t:enable='{on}'")
    return out


async def video_fps(path: Path) -> str:
    """The video's frame rate as ffmpeg writes it ("30/1", "30000/1001"); 30 when unknown."""
    out = await run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                     "-of", "csv=p=0", str(path)], timeout=60)
    try:
        rate = Fraction(out.strip().splitlines()[0])
        return f"{rate.numerator}/{rate.denominator}" if 5 <= rate <= 120 else "30"
    except (ValueError, ZeroDivisionError, IndexError):
        return "30"


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
