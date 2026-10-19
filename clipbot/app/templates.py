"""Clip templates: a caption look, optional background music and sound effects, applied by clipbot
after OpenShorts has cut the clip (render.py does the ffmpeg part).

A template, as stored (JSON) and edited in the dashboard:
    {"name": "Meme",
     "captions": {...captions.normalize_style...},
     "music": {"sound": "sneaky-snitch", "volume": 0.18, "duck": true, "start": 0},
     "voice_volume": 1.0,
     "sfx": [{"sound": "vine-boom", "at": "punchline", "words": [], "volume": 1.0, "max": 2}],
     "credit": "Music: ... (added to post captions)"}

Sounds are files in data/sounds, named by their file name without the extension. Where an effect
lands ("at"):
    start      the first frame             end        the clip's last moment
    peak       where chat exploded (clips cut from a /scan moment)
    word       whenever one of `words` is said
    punchline  the punchline(s), picked by Gemini from the clip's words
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from . import captions
from .config import Settings

log = logging.getLogger("clipbot.templates")

SOUND_EXTS = (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus")
TRIGGERS = ("start", "end", "peak", "word", "punchline")
CHAT_LAG_SECONDS = 4.0      # chat reacts a few seconds after the moment on stream
MIN_GAP_SECONDS = 1.5       # between two effects
_SOUND_NAME = re.compile(r"[^A-Za-z0-9 _.-]")
_PUNCT = re.compile(r"[^\w']+")

SNEAKY_CREDIT = ('Music: "Sneaky Snitch" Kevin MacLeod (incompetech.com), licensed under Creative Commons: '
                 'By Attribution 4.0 (creativecommons.org/licenses/by/4.0/)')

# Seeded on first start; editable and deletable like any other.
BUILT_IN: dict[str, dict[str, Any]] = {
    "standard": {"name": "Standard", "captions": captions.PRESETS["default"]},
    "hormozi": {"name": "Hormozi", "captions": captions.PRESETS["hormozi"]},
    "meme": {"name": "Meme",
             "captions": {**captions.PRESETS["oneword"], "font_name": "Luckiest Guy", "font_size": 60,
                          "border_width": 4},
             "sfx": [{"sound": "vine-boom", "at": "punchline", "volume": 1.0, "max": 2}]},
    "sneaky": {"name": "Sneaky", "captions": captions.PRESETS["clean"],
               "music": {"sound": "sneaky-snitch", "volume": 0.18, "duck": True},
               "credit": SNEAKY_CREDIT},
    "hype": {"name": "Hype", "captions": captions.PRESETS["lime"],
             "sfx": [{"sound": "vine-boom", "at": "peak", "volume": 1.0, "max": 1}]},
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


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """A complete template from whatever the editor (or an old version) stored."""
    raw = raw or {}
    music = raw.get("music") or {}
    sfx = []
    for rule in (raw.get("sfx") or [])[:12]:
        sound = sound_name(rule.get("sound"))
        at = rule.get("at") if rule.get("at") in TRIGGERS else None
        if not sound or not at:
            continue
        words = [w.strip().lower() for w in (rule.get("words") or []) if isinstance(w, str) and w.strip()][:20]
        sfx.append({"sound": sound, "at": at, "words": words, "volume": _num(rule.get("volume"), 0, 2, 1.0),
                    "max": int(_num(rule.get("max"), 1, 10, 2 if at in ("word", "punchline") else 1))})
    return {
        "name": str(raw.get("name") or "Template").strip()[:60] or "Template",
        "captions": captions.normalize_style(raw.get("captions")),
        "music": {"sound": sound_name(music.get("sound")), "volume": _num(music.get("volume"), 0, 1, 0.15),
                  "duck": bool(music.get("duck", True)), "start": _num(music.get("start"), 0, 3600, 0)},
        "voice_volume": _num(raw.get("voice_volume"), 0, 2, 1.0),
        "sfx": sfx,
        "credit": str(raw.get("credit") or "").strip()[:400],
    }


def sounds_used(template: dict[str, Any]) -> list[str]:
    names = [r["sound"] for r in template.get("sfx", [])]
    if (template.get("music") or {}).get("sound"):
        names.insert(0, template["music"]["sound"])
    return list(dict.fromkeys(names))


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
                  peak: Optional[float] = None, punchlines: Optional[list[float]] = None,
                  manual: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    """[{sound, t, volume}] for one clip: the template's rules plus effects placed by hand.
    Effects closer than MIN_GAP_SECONDS to an earlier one are dropped (hand-placed ones always stay)."""
    events: list[dict[str, Any]] = []

    def add(sound: str, t: float, volume: float, force: bool = False) -> bool:
        if not 0 <= t < duration:
            return False
        if not force and any(abs(t - e["t"]) < MIN_GAP_SECONDS for e in events):
            return False
        events.append({"sound": sound, "t": round(t, 3), "volume": volume})
        return True

    for item in manual or []:
        sound = sound_name(item.get("sound"))
        if sound:   # placed at the very end (the video had finished playing): still audible
            t = min(_num(item.get("t"), 0, 3600, 0), max(0.0, duration - 0.3))
            add(sound, t, _num(item.get("volume"), 0, 2, 1.0), force=True)
    for rule in template.get("sfx", []):
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
        elif rule["at"] == "punchline":
            times = list(punchlines or [])
        placed = 0
        for t in times:
            if placed >= rule["max"]:
                break
            placed += add(rule["sound"], t, rule["volume"])
    return sorted(events, key=lambda e: e["t"])


def peak_in_clip(moment_peak: Optional[float], clip_start_in_source: Optional[float]) -> Optional[float]:
    """Where a /scan chat peak falls inside the clip (chat reacts CHAT_LAG_SECONDS late)."""
    if moment_peak is None or clip_start_in_source is None:
        return None
    return moment_peak - CHAT_LAG_SECONDS - clip_start_in_source


# ---- punchlines from Gemini -------------------------------------------------------------
PUNCHLINE_PROMPT = """This is the transcript of a short vertical clip cut from a livestream, one word per line as
"<seconds> <word>". Pick up to {n} moments where a meme sound effect (like the vine boom) lands best:
the punchline of a joke, a shocking or absurd line, a sudden reaction or a fail. Give the time in seconds
right after the word where the sound should hit. If nothing fits, return an empty list.

{lines}"""


async def find_punchlines(settings: Settings, words: list[dict[str, Any]], n: int = 2,
                          client: Optional[httpx.AsyncClient] = None) -> list[float]:
    """Times (clip seconds) for punchline effects, or [] when Gemini isn't set up or answers oddly."""
    if not settings.gemini_api_key or not words:
        return []
    lines = "\n".join(f"{w['e']:.2f} {w['w']}" for w in words[:600])
    body = {"contents": [{"parts": [{"text": PUNCHLINE_PROMPT.format(n=n, lines=lines)}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2,
                                 "responseSchema": {"type": "OBJECT", "properties": {
                                     "times": {"type": "ARRAY", "items": {"type": "NUMBER"}}},
                                     "required": ["times"]}}}
    url = f"{settings.gemini_api_url}/v1beta/models/{settings.gemini_model}:generateContent"
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        resp = await client.post(url, json=body, headers={"x-goog-api-key": settings.gemini_api_key})
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        times = json.loads(text).get("times") or []
        return sorted({round(float(t), 2) for t in times if isinstance(t, (int, float))})[:n]
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as e:
        log.warning("punchline pick failed: %s", e)
        return []
    finally:
        if own:
            await client.aclose()
