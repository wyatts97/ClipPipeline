"""The Director: Gemini watches stretches of a stream, with the sound, and picks the exact clips.

OpenShorts picks clips from the transcript alone, so anything people do rather than say (a monitor
going through the wall, a fail, a face) is invisible to it, and its copy is written for podcasts.
Here Gemini sees the video and hears it, reads what chat said and what viewers named their own
clips, knows the streamer (your notes) and how you like clips written (your recent approvals and
edits), and returns each clip's start and end around setup → payoff, what happens, a score, and
the hook, title and captions. OpenShorts then only reframes and renders those clips.

Windows are small proxies (media.make_proxy: 360p, one frame a second), batched a few per request
so a scan costs a handful of requests on the free tier (gemini.py keeps under its limits).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .config import Settings
from .gemini import Gemini, GeminiError, QuotaExhausted, text_part, video_part  # noqa: F401 (re-exported)

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger("clipbot.director")

CATEGORIES = ("funny", "fail", "rage", "chaos", "reaction", "clutch", "wholesome", "story")
TOKENS_PER_SECOND = 100       # video at low resolution: ~70 per frame at 1 fps + ~30 for the audio
MAX_WINDOWS_PER_REQUEST = 4
MIN_CLIP = 3.0

COPY_STYLE = """\
- Voice: an editor of a clip channel who actually watches this streamer, not a marketer. Plain,
  specific, a bit casual. Write in the language the stream is in.
- hook (text on the first seconds of the video): at most 8 words, about this exact moment. Name
  the people or the thing: "his roommate just destroyed his monitor", "chat called it before he did".
- title (YouTube Shorts): at most 70 characters; says what happens, with names when you know them.
  No claims the clip doesn't back up.
- tiktok and instagram: one or two short sentences in the community's tone, then 2-4 hashtags that
  fit (the streamer, the game, the kind of moment). No hashtag walls.
- Never write "POV:", "you won't believe", "wait for it", "wait till the end", "insane moment",
  "this is crazy", "when you…", "no one:", or ask people to follow, like or comment. Usually no
  emoji; at most one in the hook."""

PROMPT = """\
You edit a clip channel for the streamer {streamer}. Below {videos} cut from one of their \
streams{title}. Watch each video fully, with the sound, and find the moments worth posting as \
standalone short clips (TikTok, YouTube Shorts, Instagram Reels).

What makes a clip:
- Something actually happens: a reaction, a fail, a fight, a prank, a thing getting broken, a \
clutch play, a story with a punchline, a chat interaction that lands. Watch the picture as well as \
the words: many of the best moments are things people do, not say.
- It works for someone who has never seen this streamer: the setup is inside the clip.
- Chat's reactions and the titles viewers gave their own clips (listed with each video) are strong \
hints of where the moment is and what it is. Use them, but judge for yourself.
{hints}{note}
Cutting:
- start: 1-3 seconds before the setup begins (the line, the action, or the first sign of it); \
never in the middle of a sentence.
- end: once the payoff and the reaction have landed. No dead air after it, and never cut the payoff off.
- Between {min_len:g} and {max_len:g} seconds long; within that, the moment decides.
- Times are seconds from the start of that video (a number, e.g. 83.5). Each video has its own clock.
- {count}
- Never return two clips of the same moment.

For each clip give a score from 0 to 100 for how well it would do as a short (be honest: most are \
40-70; 85+ only for moments people would send their friends), its category, what happens in plain \
words (who does what; name people when you know them) and why it works.

Then write its copy:
{style}
{examples}{notes}"""

SCHEMA = {"type": "OBJECT", "properties": {"clips": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {
        "video": {"type": "STRING"}, "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"},
        "score": {"type": "INTEGER"}, "category": {"type": "STRING", "enum": list(CATEGORIES)},
        "what_happened": {"type": "STRING"}, "why": {"type": "STRING"}, "hook": {"type": "STRING"},
        "title": {"type": "STRING"}, "tiktok": {"type": "STRING"}, "instagram": {"type": "STRING"}},
    "required": ["video", "start", "end", "score", "category", "what_happened", "why", "hook", "title",
                 "tiktok", "instagram"]}}}, "required": ["clips"]}


@dataclass
class Window:
    """A stretch of video Gemini watches: its proxy file and where it sits on the clock the picks
    should come back in (VOD seconds for scans, section seconds for jobs)."""
    id: str
    path: Path
    start: float
    end: float
    chat: list[tuple[float, str, int]] = field(default_factory=list)    # (t on that clock, text, count)
    clip_titles: list[str] = field(default_factory=list)
    signals: str = ""                                                   # "chat 8× its normal, loud"

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Pick:
    start: float          # on the windows' clock
    end: float
    score: int
    category: str
    what_happened: str
    why: str
    hook: str
    title: str
    tiktok: str
    instagram: str
    window: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Pick":
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__ if k in data})

    def shifted(self, by: float) -> "Pick":
        return Pick(**{**self.to_dict(), "start": round(self.start + by, 2), "end": round(self.end + by, 2)})


@dataclass
class Context:
    streamer: str = ""                    # "twitch/xqc"
    title: str = ""                       # the stream's title
    notes: str = ""                       # yours, about this streamer
    note: str = ""                        # what to look for in this job
    examples: list[dict[str, str]] = field(default_factory=list)   # recent approvals: hook, title, ai_hook, ai_title
    skipped: list[str] = field(default_factory=list)               # what happened in clips you skipped
    hints: list[Pick] = field(default_factory=list)                # moments already found, on the windows' clock
    want: Optional[int] = None            # how many clips in all (None: every real moment)
    min_len: float = 15
    max_len: float = 60


def tc(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rest = divmod(seconds, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60}:{rest % 60:02d}"


def window_text(w: Window) -> str:
    lines = [f"Video {w.id} ({tc(w.duration)} long):"]
    if w.signals:
        lines.append(f"Signals: {w.signals}.")
    if w.clip_titles:
        lines.append("Viewers clipped it as: " + "; ".join(f'"{t}"' for t in w.clip_titles) + ".")
    if w.chat:
        lines.append("Chat during it (seconds from the start of this video; ×n = said n times):")
        lines += [f"{t - w.start:.0f} {text}" + (f" ×{n}" if n > 1 else "") for t, text, n in w.chat]
    return "\n".join(lines)


def build_prompt(windows: list[Window], ctx: Context) -> str:
    n = len(windows)
    hints = ""
    if ctx.hints:
        by_window = {w.id: w for w in windows}
        lines = []
        for h in ctx.hints:
            w = next((w for w in windows if w.start <= h.start < w.end), None) or by_window.get(h.window)
            if w:
                lines.append(f"- video {w.id}, around {h.start - w.start:.0f}-{h.end - w.start:.0f} s: {h.what_happened}")
        if lines:
            hints = ("\nThese moments were already spotted in the videos (times are rough). Find each one and "
                     "cut it exactly; add another only if it's clearly a better clip:\n" + "\n".join(lines) + "\n")
    note = f"\nThe channel owner says to look for: {ctx.note.strip()}\n" if ctx.note.strip() else ""
    if ctx.hints:
        count = "Return one clip per moment listed above (and any clearly better one)."
    elif ctx.want:
        count = (f"Return the {ctx.want} best clip{'s' if ctx.want > 1 else ''} across "
                 f"{'all the videos' if n > 1 else 'the video'}; fewer only if there aren't that many real moments.")
    else:
        count = "Return every real moment, 0 to 3 per video; none for a video where nothing happens."
    examples = ""
    if ctx.examples:
        lines = []
        for e in ctx.examples:
            line = f'- hook "{e.get("hook", "")}" | title "{e.get("title", "")}"'
            if e.get("ai_hook") and e["ai_hook"] != e.get("hook"):
                line += f' (an AI wrote hook "{e["ai_hook"]}"; the owner changed it to theirs)'
            lines.append(line)
        examples += "\nClips the owner approved recently, with the copy they posted. Match this voice:\n" + "\n".join(lines) + "\n"
    if ctx.skipped:
        examples += ("\nThe owner skipped clips like these; don't pick moments like them:\n"
                     + "\n".join(f"- {s}" for s in ctx.skipped) + "\n")
    notes = f"\nAbout {ctx.streamer or 'the streamer'}, from the owner:\n{ctx.notes.strip()}\n" if ctx.notes.strip() else ""
    return PROMPT.format(
        streamer=ctx.streamer or "of this channel", videos="is a video" if n == 1 else f"are {n} videos",
        title=f' (stream title: "{ctx.title}")' if ctx.title else "", hints=hints, note=note,
        min_len=ctx.min_len, max_len=ctx.max_len, count=count, style=COPY_STYLE, examples=examples, notes=notes)


def batches(windows: list[Window], max_tokens: int) -> list[list[Window]]:
    """Windows grouped so each request stays under max_tokens (a window longer than that goes alone)."""
    out: list[list[Window]] = []
    for w in windows:
        cost = w.duration * TOKENS_PER_SECOND
        last = out[-1] if out else None
        if (last and len(last) < MAX_WINDOWS_PER_REQUEST
                and sum(x.duration * TOKENS_PER_SECOND for x in last) + cost <= max_tokens):
            last.append(w)
        else:
            out.append([w])
    return out


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_picks(answer: dict[str, Any], windows: list[Window], ctx: Context) -> list[Pick]:
    """Gemini's clips on the windows' clock: clamped inside their video, held to the length limits."""
    by_id = {w.id: w for w in windows}
    picks = []
    for item in answer.get("clips") or []:
        if not isinstance(item, dict):
            continue
        w = by_id.get(str(item.get("video") or "").strip()) or (windows[0] if len(windows) == 1 else None)
        try:
            start, end = float(item.get("start")), float(item.get("end"))
        except (TypeError, ValueError):
            continue
        if w is None:
            continue
        length = w.duration
        start, end = max(0.0, min(start, length)), max(0.0, min(end, length))
        if end - start < ctx.min_len:              # too short: grow the end first, then the start
            end = min(length, start + ctx.min_len)
            start = max(0.0, end - ctx.min_len)
        if end - start > ctx.max_len:
            end = start + ctx.max_len
        if end - start < MIN_CLIP:
            continue
        category = str(item.get("category") or "")
        picks.append(Pick(
            start=round(w.start + start, 2), end=round(w.start + end, 2),
            score=max(0, min(100, int(item.get("score") or 0))),
            category=category if category in CATEGORIES else "funny",
            what_happened=_text(item.get("what_happened"), 300), why=_text(item.get("why"), 300),
            hook=_text(item.get("hook"), 100), title=_text(item.get("title"), 100),
            tiktok=str(item.get("tiktok") or "").strip()[:600], instagram=str(item.get("instagram") or "").strip()[:600],
            window=w.id))
    return picks


def dedupe(picks: list[Pick]) -> list[Pick]:
    """Best first; a clip overlapping a better one by more than half of the shorter is the same moment."""
    kept: list[Pick] = []
    for p in sorted(picks, key=lambda p: p.score, reverse=True):
        if all(min(p.end, k.end) - max(p.start, k.start) <= 0.5 * min(p.end - p.start, k.end - k.start)
               for k in kept):
            kept.append(p)
    return kept


async def direct(settings: Settings, windows: list[Window], ctx: Context,
                 client: Optional[httpx.AsyncClient] = None) -> list[Pick]:
    """Watch the windows and return the clips, best first. Raises GeminiError (QuotaExhausted when
    the free quota is gone) so callers can fall back to the old way."""
    if not windows:
        return []
    gem = Gemini(settings, client, model=settings.director_model)
    uploaded: dict[str, dict[str, Any]] = {}
    picks: list[Pick] = []
    try:
        for batch in batches(windows, settings.director_batch_tokens):
            for w in batch:
                uploaded[w.id] = await gem.upload(w.path)
            parts = [text_part(build_prompt(batch, ctx))]
            for w in batch:
                parts += [text_part(window_text(w)), video_part(uploaded[w.id])]
            est = int(sum(w.duration for w in batch) * TOKENS_PER_SECOND) + 3000
            answer = await gem.generate_json(parts, SCHEMA, est_tokens=est, temperature=0.5, low_res=True)
            found = parse_picks(answer, batch, ctx)
            log.info("director: %d clip(s) in %s", len(found), ", ".join(w.id for w in batch))
            picks += found
            for w in batch:
                await gem.delete(uploaded.pop(w.id)["name"])
    finally:
        for f in uploaded.values():
            await gem.delete(f["name"])
        await gem.aclose()
    picks = dedupe(picks)
    return picks[:ctx.want] if ctx.want else picks


def learned(store: "Store", streamer: str, n: int = 8, skips: int = 5) -> tuple[list[dict[str, str]], list[str]]:
    """What your decisions teach: the copy you posted on recent approvals (with the AI's, when you
    changed it) and what happened in clips you skipped. This streamer's first, newest first."""
    def theirs_first(clips):
        return sorted(clips, key=lambda c: c.meta.get("streamer") != streamer)     # stable: keeps recency
    examples = [{"hook": c.hook or "", "title": c.title or "", "ai_hook": c.meta.get("ai_hook") or "",
                 "ai_title": c.meta.get("ai_title") or ""}
                for c in theirs_first(store.list_clips("approved", limit=60)) if c.hook or c.title][:n]
    skipped = [c.meta["what_happened"] for c in theirs_first(store.list_clips("rejected", limit=60))
               if c.meta.get("what_happened")][:skips]
    return examples, skipped


def signals_text(info: dict[str, Any]) -> str:
    """A moment's signals in words, for the prompt."""
    parts = []
    if info.get("chat_ratio"):
        parts.append(f"chat {info['chat_ratio']:g}× its normal")
    if info.get("top_tokens"):
        parts.append("chat said " + ", ".join(f"{tok} ×{n}" for tok, n in info["top_tokens"]))
    if info.get("loud"):
        parts.append("loud")
    if info.get("clips"):
        parts.append(f"{info['clips']} viewer clip{'s' if info['clips'] > 1 else ''}")
    if info.get("replayed"):
        parts.append("YouTube's most replayed")
    return ", ".join(parts)
