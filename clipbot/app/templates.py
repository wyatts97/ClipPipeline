"""Clip templates: a caption look, optional background music and sound effects, applied by clipbot
after OpenShorts has cut the clip (render.py does the ffmpeg part).

A template, as stored (JSON) and edited in the dashboard:
    {"name": "Meme",
     "captions": {...captions.normalize_style...},
     "music": {"sound": "sneaky-snitch", "volume": 0.18, "duck": true, "start": 0},
     "hook": {...captions.normalize_hook...: the look of the hook line at the start},
     "voice_volume": 1.0,
     "sfx": [{"sounds": ["vine-boom", "bruh"], "at": "moment", "kinds": ["punchline", "shock"],
              "words": [], "volume": 1.0, "max": 2}],
     "credit": "Music: ... (added to post captions)"}

Sounds are files in data/sounds, named by their file name without the extension. A rule with several
sounds picks one per effect (the same picks every time the clip renders, never the same one twice in a
row). Where an effect lands ("at"):
    start    the first frame                 end      the clip's last second
    peak     where chat exploded (clips cut from a /scan moment)
    word     whenever one of `words` is said
    moment   moments of the given `kinds`, found by Gemini in the clip's words:
             punchline, shock, fail, awkward, hype, sneaky
"""
from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from . import captions
from .config import Settings
from .gemini import Gemini, GeminiError, text_part

log = logging.getLogger("clipbot.templates")

SOUND_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus")
TRIGGERS = ("start", "end", "peak", "word", "moment")
# What Gemini labels each moment as, and what that means (also the prompt's definitions).
MOMENT_KINDS = {
    "punchline": "a joke lands",
    "shock": "a shocking, absurd or unbelievable line or reveal",
    "fail": "a mistake, a loss, something breaks or goes wrong",
    "awkward": "cringe, an awkward silence, someone gets roasted",
    "hype": "a win, a clutch play, big excitement",
    "sneaky": "plotting, sneaking, suspicious behaviour",
}
MAX_MOMENTS = 8
CHAT_LAG_SECONDS = 4.0      # chat reacts a few seconds after the moment on stream
MIN_GAP_SECONDS = 1.5       # between two effects
_SOUND_NAME = re.compile(r"[^A-Za-z0-9 _.-]")
_PUNCT = re.compile(r"[^\w']+")

SNEAKY_CREDIT = ('Music: "Sneaky Snitch" Kevin MacLeod (incompetech.com), licensed under Creative Commons: '
                 'By Attribution 4.0 (creativecommons.org/licenses/by/4.0/)')

# Seeded on first start; editable and deletable like any other.
BUILT_IN: dict[str, dict[str, Any]] = {
    "standard": {"name": "Standard", "captions": captions.PRESETS["default"], "hook": captions.DEFAULT_HOOK},
    "hormozi": {"name": "Hormozi", "captions": captions.PRESETS["hormozi"],
                "hook": {**captions.DEFAULT_HOOK, "box_color": "#FFE500", "uppercase": True}},
    "meme": {"name": "Meme",
             "captions": {**captions.PRESETS["oneword"], "font_name": "Luckiest Guy", "font_size": 60,
                          "border_width": 4},
             "hook": {**captions.DEFAULT_HOOK, "style": "outline", "font_name": "Luckiest Guy", "font_size": 34,
                      "text_color": "#FFFFFF", "uppercase": True},
             "sfx": [{"sounds": ["vine-boom"], "at": "moment", "kinds": ["punchline", "shock"], "max": 2}]},
    "sneaky": {"name": "Sneaky", "captions": captions.PRESETS["clean"],
               "hook": {**captions.DEFAULT_HOOK, "box_color": "#121214", "text_color": "#FFFFFF"},
               "music": {"sound": "sneaky-snitch", "volume": 0.18, "duck": True},
               "credit": SNEAKY_CREDIT},
    "hype": {"name": "Hype", "captions": captions.PRESETS["lime"],
             "hook": {**captions.DEFAULT_HOOK, "box_color": "#A3FF12", "uppercase": True},
             "sfx": [{"sounds": ["vine-boom"], "at": "peak", "max": 1}]},
}
DEFAULT_TEMPLATE = "standard"


def _num(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return round(min(hi, max(lo, float(value))), 3)
    except (TypeError, ValueError):
        return default


def sound_name(value: Any) -> Optional[str]:
    """A sound's name: its file name without the audio extension ("vine-boom.mp3" -> "vine-boom")."""
    text = Path(str(value or "").strip()).name
    if Path(text).suffix.lower() in SOUND_EXTS:
        text = Path(text).stem
    name = _SOUND_NAME.sub("", text).strip()
    return name[:80] or None


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "template"


def normalize_rule(rule: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One effect rule, or None when it has no sound or no place to land. Reads the older shape
    too: a single "sound", and at="punchline" (now a moment rule for punchlines and shocks)."""
    raw_sounds = rule.get("sounds") if isinstance(rule.get("sounds"), list) else [rule.get("sound")]
    sounds = list(dict.fromkeys(n for n in (sound_name(s) for s in raw_sounds) if n))[:8]
    at, kinds = rule.get("at"), rule.get("kinds")
    if at == "punchline":
        at, kinds = "moment", kinds or ["punchline", "shock"]
    if not sounds or at not in TRIGGERS:
        return None
    kinds = [k for k in (kinds or []) if k in MOMENT_KINDS] or list(MOMENT_KINDS) if at == "moment" else []
    words = [w.strip().lower() for w in (rule.get("words") or []) if isinstance(w, str) and w.strip()][:20]
    return {"sounds": sounds, "at": at, "kinds": list(dict.fromkeys(kinds)), "words": words,
            "volume": _num(rule.get("volume"), 0, 2, 1.0),
            "max": int(_num(rule.get("max"), 1, 10, 2 if at in ("word", "moment") else 1))}


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """A complete template from whatever the editor (or an old version) stored."""
    raw = raw or {}
    music = raw.get("music") or {}
    sfx = [r for r in (normalize_rule(rule) for rule in (raw.get("sfx") or [])[:12] if isinstance(rule, dict)) if r]
    return {
        "name": str(raw.get("name") or "Template").strip()[:60] or "Template",
        "captions": captions.normalize_style(raw.get("captions")),
        "hook": captions.normalize_hook(raw.get("hook")),
        "music": {"sound": sound_name(music.get("sound")), "volume": _num(music.get("volume"), 0, 1, 0.15),
                  "duck": bool(music.get("duck", True)), "start": _num(music.get("start"), 0, 3600, 0)},
        "voice_volume": _num(raw.get("voice_volume"), 0, 2, 1.0),
        "sfx": sfx,
        "credit": str(raw.get("credit") or "").strip()[:400],
    }


def sounds_used(template: dict[str, Any]) -> list[str]:
    names = [n for r in template.get("sfx", []) for n in r["sounds"]]
    if (template.get("music") or {}).get("sound"):
        names.insert(0, template["music"]["sound"])
    return list(dict.fromkeys(names))


def wants_moments(template: dict[str, Any]) -> bool:
    return any(r["at"] == "moment" for r in template.get("sfx", []))


# ---- sounds ----------------------------------------------------------------------------
def sounds_dir(settings: Settings) -> Path:
    return settings.folder("sounds")


def find_sound(settings: Settings, name: Optional[str]) -> Optional[Path]:
    """data/sounds/<name>.<any audio ext>, case-insensitive."""
    if not name:
        return None
    folder = settings.data_dir / "sounds"
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() in SOUND_EXTS and path.stem.lower() == name.lower():
            return path
    return None


def missing_sounds(settings: Settings, template: dict[str, Any]) -> list[str]:
    return [n for n in sounds_used(template) if not find_sound(settings, n)]


# ---- where effects land ------------------------------------------------------------------
def _norm(word: str) -> str:
    return _PUNCT.sub("", word.lower())


def place_effects(template: dict[str, Any], words: list[dict[str, Any]], duration: float,
                  peak: Optional[float] = None, moments: Optional[list[dict[str, Any]]] = None,
                  manual: Optional[list[dict[str, Any]]] = None, seed: str = "") -> list[dict[str, Any]]:
    """[{sound, t, volume}] for one clip: the template's rules plus effects placed by hand.
    Effects closer than MIN_GAP_SECONDS to an earlier one are dropped (hand-placed ones always stay).
    `seed` (the clip) fixes which sound of a rule's pool each effect gets, so re-renders match."""
    events: list[dict[str, Any]] = []

    def free(t: float) -> bool:
        return 0 <= t < duration and not any(abs(t - e["t"]) < MIN_GAP_SECONDS for e in events)

    for item in manual or []:
        sound = sound_name(item.get("sound"))
        if sound:   # placed at the very end (the video had finished playing): still audible
            t = min(_num(item.get("t"), 0, 3600, 0), max(0.0, duration - 0.3))
            events.append({"sound": sound, "t": round(t, 3), "volume": _num(item.get("volume"), 0, 2, 1.0)})
    for index, rule in enumerate(template.get("sfx", [])):
        times: list[float] = []
        if rule["at"] == "start":
            times = [0.0]
        elif rule["at"] == "end":
            times = [max(0.0, duration - 1.0)]
        elif rule["at"] == "peak" and peak is not None:
            times = [peak]
        elif rule["at"] == "word":
            wanted = {_norm(w) for w in rule["words"]}
            times = [w["e"] for w in words if _norm(w["w"]) in wanted]
        elif rule["at"] == "moment":
            times = sorted(m["t"] for m in moments or [] if m.get("kind") in rule["kinds"])
        rng = random.Random(f"{seed}:{index}")
        last = None
        placed = 0
        for t in times:
            if placed >= rule["max"]:
                break
            if not free(t):
                continue
            pool = [s for s in rule["sounds"] if s != last] or rule["sounds"]
            last = rng.choice(pool)
            events.append({"sound": last, "t": round(t, 3), "volume": rule["volume"]})
            placed += 1
    return sorted(events, key=lambda e: e["t"])


def peak_in_clip(moment_peak: Optional[float], clip_start_in_source: Optional[float]) -> Optional[float]:
    """Where a /scan chat peak falls inside the clip (chat reacts CHAT_LAG_SECONDS late)."""
    if moment_peak is None or clip_start_in_source is None:
        return None
    return moment_peak - CHAT_LAG_SECONDS - clip_start_in_source


def cached_moments(meta: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """Moments found on an earlier render (older renders stored bare punchline times)."""
    if isinstance(meta.get("moments"), list):
        return meta["moments"]
    if isinstance(meta.get("punchlines"), list):
        return [{"t": float(t), "kind": "punchline"} for t in meta["punchlines"]]
    return None


# ---- hook ideas from Gemini ---------------------------------------------------------------
HOOK_PROMPT = """Write {n} different hook lines for the start of this short vertical clip cut from a \
livestream{streamer}. The hook is the text on the video's first seconds.
Each line: at most 8 words, about this exact moment (name the people or the thing that happens), in
the plain voice of a clip channel that actually watches this streamer, in the clip's language. No
hashtags, no emoji, no quotes around it. Never "POV:", "you won't believe", "wait for it", "wait till
the end", "insane moment", "this is crazy". Vary them: say what happens, lead with the reaction,
tease the payoff without spoiling it.
{context}
Transcript:
{lines}"""


async def suggest_hooks(settings: Settings, words: list[dict[str, Any]], title: Optional[str] = None,
                        current: Optional[str] = None, n: int = 5,
                        client: Optional[httpx.AsyncClient] = None, *, what_happened: str = "",
                        streamer: str = "", notes: str = "",
                        examples: Optional[list[str]] = None) -> Optional[list[str]]:
    """Hook lines for a clip, written with what the Director saw happen, your notes about the
    streamer and hooks you posted; None when Gemini isn't set up or failed."""
    if not settings.gemini_api_key:
        return None
    lines = " ".join(w["w"] for w in words[:900]) or "(no speech)"
    context = "\n".join(x for x in (
        f"What happens in the clip: {what_happened}" if what_happened else "",
        f"Clip title: {title}" if title else "",
        f"The current hook, which needs replacing: {current}" if current else "",
        f"About the streamer, from the channel owner: {notes}" if notes else "",
        ("Hooks the owner posted recently (match this voice): " + " | ".join(examples)) if examples else "") if x)
    prompt = HOOK_PROMPT.format(n=n, streamer=f" ({streamer})" if streamer else "", context=context, lines=lines)
    schema = {"type": "OBJECT", "properties": {"hooks": {"type": "ARRAY", "items": {"type": "STRING"}}},
              "required": ["hooks"]}
    try:
        gem = Gemini(settings, client)
        try:
            answer = await gem.generate_json([text_part(prompt)], schema, temperature=0.9, retries=0)
        finally:
            await gem.aclose()
        hooks = []
        for item in answer.get("hooks") or []:
            line = captions.strip_emoji(str(item)).strip().strip('"').strip()
            if line and line.lower() not in (h.lower() for h in hooks):
                hooks.append(line[:80])
        return hooks[:n]
    except (GeminiError, TypeError, AttributeError) as e:
        log.warning("hook ideas failed: %s", e)
        return None


# ---- moments from Gemini ------------------------------------------------------------------
MOMENT_PROMPT = """This is the transcript of a short vertical clip cut from a livestream, one word per line as
"<seconds> <word>". Find up to {n} moments where a meme sound effect would land, and say what kind each is:
{kinds}
Give the time in seconds right after the word where the sound should hit. Only real moments; if nothing
fits, return an empty list.

{lines}"""


async def find_moments(settings: Settings, words: list[dict[str, Any]], n: int = MAX_MOMENTS,
                       client: Optional[httpx.AsyncClient] = None) -> Optional[list[dict[str, Any]]]:
    """[{t, kind}] in clip seconds ([] when nothing fits), or None when Gemini isn't set up or failed,
    so the next render asks again."""
    if not words:
        return []
    if not settings.gemini_api_key:
        return None
    lines = "\n".join(f"{w['e']:.2f} {w['w']}" for w in words[:600])
    kinds = "\n".join(f"- {k}: {d}" for k, d in MOMENT_KINDS.items())
    body = {"contents": [{"parts": [{"text": MOMENT_PROMPT.format(n=n, kinds=kinds, lines=lines)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "responseSchema": {"type": "OBJECT", "properties": {"moments": {
                                     "type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                                         "time": {"type": "NUMBER"},
                                         "kind": {"type": "STRING", "enum": list(MOMENT_KINDS)}},
                                         "required": ["time", "kind"]}}},
                                     "required": ["moments"]}}}
    url = f"{settings.gemini_api_url}/v1beta/models/{settings.gemini_model}:generateContent"
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        resp = await client.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key})
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        found: dict[float, str] = {}
        for item in json.loads(text).get("moments") or []:
            if isinstance(item, dict) and item.get("kind") in MOMENT_KINDS and isinstance(item.get("time"), (int, float)):
                found.setdefault(round(float(item["time"]), 2), item["kind"])
        return [{"t": t, "kind": k} for t, k in sorted(found.items())][:n]
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        log.warning("moment pick failed: %s", e)
        return None
    finally:
        if own:
            await client.aclose()
