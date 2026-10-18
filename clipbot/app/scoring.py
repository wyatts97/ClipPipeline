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


def find_moments(sig: StreamSignals, top: int = 5, before: float = 90, after: float = 45,
                 min_score: float = 1.5) -> list[Moment]:
    scores, parts = score(sig)
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
        moments.append(moment)
    return moments
