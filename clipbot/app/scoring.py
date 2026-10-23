"""Score a long stream in 5-second buckets and pick the moments worth clipping.

Each signal is compared with the stream's own level a few minutes around it (median and
MAD over a rolling window), so a busy chat or a loud streamer doesn't swamp the result:
    - unique chatters per bucket   (spam from one person counts once)
    - "hype" messages per bucket   (KEKW, LUL, ???, ALL CAPS, 😂, "clip it", ...)
    - audio loudness               (shouting, a big play)
    - clips viewers made           (Twitch/Kick), weighted by their views
    - YouTube "most replayed"      (when YouTube has it)
Chat reacts a few seconds after the moment, so each window starts well before the peak.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from .chat import ChatMessage
from .clips_api import ClipMarker

BUCKET = 5.0
SKIP_START = 180.0  # "starting soon" screens and hello floods
SKIP_END = 120.0    # goodbye floods and raids

HYPE_WORDS = frozenset("""
kekw kekl kekwait lul lulw omegalul icant pepelaugh lmao lmfao lol lool rofl xd xdd
pog pogu pogchamp poggers pogo wtf omg omfg holy clip clipit clipped clipthat noway
monkas monkaw aware huh ez gg w l +1 +2 dead sadge copium wow sheesh bruh insane
clap ezclap pogclap peepoclap pepega pausechamp gigachad kreygasm cringe catjam
""".split())
HYPE_EMOJI = ("😂", "🤣", "💀", "😭", "😳", "🔥", "😱", "🤯",
              ":face-with-tears-of-joy:", ":rolling-on-the-floor-laughing:", ":skull:")
GREETINGS = frozenset("""hi hii hiii hello hey heyy yo sup o7 hola gm gn bye byee goodbye goodnight
gnight night morning cya later peace""".split())
STOPWORDS = frozenset("""
the a an i you he she it we they is are was be to of and or in on at for that this what
me my your his her its so just like not do did does have has had im its it's u ur
""".split())
_TOKEN_RE = re.compile(r"[A-Za-z0-9+:_'-]+|[^\w\s]", re.UNICODE)
_LAUGH_RE = re.compile(r"(?:ha){3,}|(?:ja){3,}|(?:he){3,}|l+o+l{2,}", re.IGNORECASE)
_EMOTE_RE = re.compile(r"^[a-z][a-z0-9]{1,11}([A-Z][A-Za-z0-9]*)$")


def tokens(text: str) -> list[str]:
    return [t.strip("'-") for t in _TOKEN_RE.findall(text or "") if t.strip("'-")]


def is_greeting(text: str) -> bool:
    """'hi', 'o7 o7', 'bye chat', 'gn everyone': hellos and goodbyes, not reactions."""
    words = [t.lower() for t in tokens(text) if t.isalnum()]
    return bool(words) and (all(w in GREETINGS for w in words)
                            or (len(words) <= 3 and words[0] in GREETINGS))


def is_hype(text: str, extra: frozenset[str] = frozenset()) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if set(text) <= set("?!¿ "):
        return True
    letters = [c for c in text if c.isalpha()]
    if len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return True
    if any(e in text for e in HYPE_EMOJI) or _LAUGH_RE.search(text):
        return True
    for tok in tokens(text):
        key = tok.lower()
        if key in HYPE_WORDS or key in extra:
            return True
        # Channel emotes are a prefix plus a known emote name: cohhLUL, xqcL, lirikPOG.
        emote = _EMOTE_RE.match(tok)
        if emote and emote.group(1).lower() in HYPE_WORDS:
            return True
    return False


@dataclass
class Moment:
    peak: float
    start: float
    end: float
    score: float
    chat_ratio: Optional[float] = None   # unique chatters at the peak vs. the local normal
    loud: bool = False
    clips: int = 0
    clip_views: int = 0
    replayed: bool = False
    top_tokens: list[tuple[str, int]] = field(default_factory=list)
    clip_titles: list[str] = field(default_factory=list)     # viewers' own clips here, most viewed first
    picks: list[dict] = field(default_factory=list)          # the Director's clips here (director.Pick), best first
    source: str = "signals"                                  # "viewers": found from the clips viewers made

    def info(self) -> dict:
        """What's stored with the moment (moments.info) and shown about it."""
        out = {"chat_ratio": self.chat_ratio, "loud": self.loud, "clips": self.clips, "clip_views": self.clip_views,
               "replayed": self.replayed, "top_tokens": self.top_tokens, "clip_titles": self.clip_titles}
        if self.picks:
            out.update(picks=self.picks, what_happened=self.picks[0]["what_happened"],
                       director_score=self.picks[0]["score"])
        if self.source != "signals":
            out["source"] = self.source
        return out


def rolling_robust_z(values: Sequence[float], half_window: int, floor: float,
                     floor_ratio: float = 0.0, stride: int = 6) -> list[float]:
    """(value - local median) / local MAD-scale, baseline recomputed every `stride` buckets."""
    n = len(values)
    out = [0.0] * n
    for block in range(0, n, stride):
        lo, hi = max(0, block - half_window), min(n, block + half_window + 1)
        window = sorted(values[lo:hi])
        med = window[len(window) // 2]
        mad = sorted(abs(v - med) for v in window)[len(window) // 2]
        scale = max(1.4826 * mad, floor, floor_ratio * abs(med))
        for i in range(block, min(n, block + stride)):
            out[i] = (values[i] - med) / scale
    return out


def _clip(values: Iterable[float], lo: float, hi: float) -> list[float]:
    return [min(hi, max(lo, v)) for v in values]


def _smooth(values: Sequence[float], radius: int = 1) -> list[float]:
    out = []
    for i in range(len(values)):
        window = values[max(0, i - radius):i + radius + 1]
        out.append(sum(window) / len(window))
    return out


@dataclass
class StreamSignals:
    duration: float
    messages: Optional[list[ChatMessage]] = None
    loudness_db: Optional[list[float]] = None         # one value per second
    markers: list[ClipMarker] = field(default_factory=list)
    heatmap: Optional[list[dict]] = None              # yt-dlp: [{start_time, end_time, value}]
    extra_hype: frozenset[str] = frozenset()

    @property
    def n(self) -> int:
        return max(1, int(math.ceil(self.duration / BUCKET)))


def chat_buckets(sig: StreamSignals) -> tuple[list[float], list[float], list[float]]:
    n = sig.n
    total, authors, hype = [0.0] * n, [set() for _ in range(n)], [0.0] * n
    for m in sig.messages or []:
        i = int(m.t // BUCKET)
        if not 0 <= i < n or is_greeting(m.text):
            continue
        total[i] += 1
        authors[i].add(m.author)
        if is_hype(m.text, sig.extra_hype):
            hype[i] += 1
    return total, [float(len(a)) for a in authors], hype


def score(sig: StreamSignals) -> tuple[list[float], dict[str, list[float]]]:
    """Combined score per bucket, plus the per-signal parts (for describing moments)."""
    n = sig.n
    parts: dict[str, list[float]] = {}
    half = int(300 / BUCKET)  # +-5 minutes of context

    has_chat = bool(sig.messages)
    if has_chat:
        _, uniq, hype = chat_buckets(sig)
        parts["uniq"] = uniq
        parts["z_uniq"] = _clip(rolling_robust_z(uniq, half, floor=1.0, floor_ratio=0.25), -3, 10)
        parts["z_hype"] = _clip(rolling_robust_z(hype, half, floor=1.0, floor_ratio=0.25), -3, 10)

    if sig.loudness_db:
        per_bucket = []
        for i in range(n):
            chunk = sig.loudness_db[int(i * BUCKET):int((i + 1) * BUCKET)]
            per_bucket.append(max(chunk) if chunk else -100.0)
        parts["z_audio"] = _clip(rolling_robust_z(per_bucket, half, floor=2.0), -3, 6)

    boost = [0.0] * n
    for mk in sig.markers:
        weight = 1.0 + math.log10(1 + max(0, mk.views))
        for i in range(int(mk.t // BUCKET), min(n, int((mk.t + mk.duration) // BUCKET) + 1)):
            if i >= 0:
                boost[i] = min(8.0, boost[i] + weight)
    parts["clips"] = boost

    if sig.heatmap:
        heat = [0.0] * n
        for seg in sig.heatmap:
            for i in range(int(seg["start_time"] // BUCKET), min(n, int(seg["end_time"] // BUCKET) + 1)):
                heat[i] = max(heat[i], float(seg.get("value") or 0))
        parts["z_heat"] = _clip(rolling_robust_z(heat, n, floor=0.05, stride=n), -2, 6)  # whole video

    zero = [0.0] * n
    if has_chat:
        combined = [parts["z_uniq"][i] + 0.8 * parts["z_hype"][i]
                    + 0.5 * parts.get("z_audio", zero)[i] + boost[i] + parts.get("z_heat", zero)[i]
                    for i in range(n)]
    else:
        combined = [parts.get("z_audio", zero)[i] + boost[i] + 1.2 * parts.get("z_heat", zero)[i]
                    for i in range(n)]
    return _smooth(combined), parts


def top_tokens(messages: Sequence[ChatMessage], start: float, end: float, k: int = 3) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    forms: dict[str, Counter[str]] = defaultdict(Counter)
    for m in messages:
        if not start <= m.t <= end:
            continue
        seen = set()
        for tok in tokens(m.text):
            key = tok.lower()
            if key in STOPWORDS or key in seen or sum(c.isdigit() for c in key) >= 4 or (
                    len(key) < 2 and key not in HYPE_WORDS and key != "?"
                    and not any(e in tok for e in HYPE_EMOJI)):
                continue  # 4+ digits: usually a username (gift subs, @mentions), not a reaction
            seen.add(key)
            counts[key] += 1
            forms[key][tok] += 1
    return [(forms[key].most_common(1)[0][0], c) for key, c in counts.most_common(k) if c >= 3]


def timeline(sig: StreamSignals, scores: Sequence[float], parts: dict[str, list[float]],
             max_points: int = 1200) -> dict:
    """The score and each signal, downsampled for the dashboard chart. Each point is the max of
    its group, so short peaks survive the downsampling."""
    n = len(scores)
    group = max(1, math.ceil(n / max_points))

    def pool(values: Sequence[float]) -> list[float]:
        return [round(max(values[i:i + group]), 2) for i in range(0, n, group)]

    series = {"score": pool(scores)}
    for key, name in (("z_uniq", "chat"), ("z_hype", "hype"), ("z_audio", "audio"),
                      ("clips", "clips"), ("z_heat", "replayed")):
        if key in parts and any(parts[key]):
            series[name] = pool(parts[key])
    return {"step": BUCKET * group, "duration": sig.duration, "series": series,
            "markers": [{"t": m.t, "duration": m.duration, "views": m.views, "title": m.title} for m in sig.markers[:500]]}


def find_moments(sig: StreamSignals, top: int = 5, before: float = 90, after: float = 45,
                 min_score: float = 1.5,
                 scored: Optional[tuple[list[float], dict[str, list[float]]]] = None) -> list[Moment]:
    scores, parts = scored or score(sig)
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    picked: list[float] = []
    moments: list[Moment] = []
    for i in order:
        if len(moments) >= top or scores[i] < min_score:
            break
        t = (i + 0.5) * BUCKET
        if (t < min(SKIP_START, sig.duration / 10) or t > sig.duration - min(SKIP_END, sig.duration / 20)
                or any(abs(t - p) < before + after for p in picked)):
            continue
        picked.append(t)
        start, end = max(0.0, t - before), min(sig.duration, t + after)
        if end - start < 60:  # stay above OpenShorts' minimum source length
            start, end = max(0.0, min(start, end - 60)), min(sig.duration, max(end, start + 60))
        moment = Moment(peak=t, start=start, end=end, score=scores[i])
        if "uniq" in parts:
            lo, hi = max(0, i - 60), min(len(scores), i + 61)
            local = sorted(parts["uniq"][lo:hi])
            normal = max(1.0, local[len(local) // 2])
            moment.chat_ratio = round(max(parts["uniq"][max(0, i - 2):i + 3]) / normal, 1)
            moment.top_tokens = top_tokens(sig.messages or [], t - 25, t + 10)
        if "z_audio" in parts:
            moment.loud = max(parts["z_audio"][max(0, i - 3):i + 4]) >= 2.0
        if "z_heat" in parts:
            moment.replayed = parts["z_heat"][i] >= 2.0
        inside = [mk for mk in sig.markers if start - 10 <= mk.t <= end]
        moment.clips, moment.clip_views = len(inside), sum(mk.views for mk in inside)
        moment.clip_titles = clip_titles(inside)
        moments.append(moment)
    return moments


VIEWER_CLIP = "viewer clip"   # the category of a pick that is a clip a viewer made


def viewer_moments(markers: Sequence[ClipMarker], duration: float, top: int = 10, min_len: float = 15,
                   before: float = 10, after: float = 8) -> list[Moment]:
    """Moments straight from the clips viewers made, most viewed first. Clips of the same moment
    (overlapping, or less than 10 s apart) count as one, ranked by their views together, then by how
    many people clipped it. Each moment's pick is its most viewed clip, cut as the viewer made it
    (at least min_len long); the window around it leaves the Director a little room either side."""
    groups: list[list[ClipMarker]] = []
    for mk in sorted((m for m in markers if 0 <= m.t < duration), key=lambda m: m.t):
        if groups and mk.t <= max(m.t + m.duration for m in groups[-1]) + 10:
            groups[-1].append(mk)
        else:
            groups.append([mk])
    groups.sort(key=lambda g: (sum(m.views for m in g), len(g)), reverse=True)
    moments = []
    for g in groups[:top]:
        best = max(g, key=lambda m: m.views)
        start, end = best.t, min(duration, best.t + max(1.0, best.duration))
        if end - start < min_len:                   # too short for a short: grow it around its middle
            mid = (start + end) / 2
            start, end = max(0.0, mid - min_len / 2), min(duration, mid + min_len / 2)
        views = sum(m.views for m in g)
        title = " ".join(best.title.split())[:120] or "a clip viewers made"
        window_start, window_end = max(0.0, start - before), min(duration, end + after)
        moment = Moment(peak=round(start, 1), start=round(window_start, 1), end=round(window_end, 1),
                        score=round(1 + math.log10(1 + views), 2), clips=len(g), clip_views=views,
                        clip_titles=clip_titles(g), source="viewers")
        moment.picks = [{"start": round(start, 2), "end": round(end, 2),
                         "score": min(99, 40 + round(15 * math.log10(1 + views))), "category": VIEWER_CLIP,
                         "what_happened": title, "why": f"{len(g)} viewer clip{'s' if len(g) > 1 else ''}, {views} views",
                         "hook": title, "title": title, "tiktok": title, "instagram": title, "window": ""}]
        moments.append(moment)
    return moments


def clip_titles(markers: Iterable[ClipMarker], k: int = 5) -> list[str]:
    """The titles viewers gave their clips, most viewed first, without repeats."""
    out: list[str] = []
    for mk in sorted(markers, key=lambda m: m.views, reverse=True):
        title = " ".join(mk.title.split())[:120]
        if title and title.lower() not in (t.lower() for t in out):
            out.append(title)
    return out[:k]


def chat_sample(messages: Sequence[ChatMessage], start: float, end: float,
                limit: int = 40) -> list[tuple[float, str, int]]:
    """What chat said between start and end, for a model to read: greetings dropped, repeats folded
    into one line with a count (KEKW ×14), at most `limit` lines (the most repeated, then the longest,
    which tend to say what happened), in time order. [(t, text, count)]."""
    seen: dict[str, list] = {}
    for m in messages:
        if not start <= m.t <= end:
            continue
        text = " ".join((m.text or "").split())[:200]
        if not text or is_greeting(text):
            continue
        key = text.lower()
        if key in seen:
            seen[key][2] += 1
        else:
            seen[key] = [m.t, text, 1]
    lines = sorted(seen.values(), key=lambda line: (-line[2], -len(line[1])))[:limit]
    return [(round(t, 1), text, n) for t, text, n in sorted(lines, key=lambda line: line[0])]
