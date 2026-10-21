"""Burned-in captions and the hook line as an ASS file: the active word highlighted, the TikTok/CapCut
look, and the hook (the short line at the start of a clip) in its own style.

Adapted from OpenShorts' subtitles.py (MIT, github.com/mutonby/openshorts): the same word
grouping, ASS styles, effects and presets, so a clip captioned here looks like one OpenShorts
captioned. The difference is where it runs: clipbot keeps each clip without captions plus its
word timings, so a template can restyle it any time.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

# PlayResY 288 units, ~15% of the frame: clear of TikTok's and Reels' bottom UI.
SAFE_MARGIN_V = 43
EFFECTS = ("none", "pop", "glow", "box", "highlight")
POSITIONS = ("bottom", "middle", "top")

# The look every clip got from OpenShorts before templates (its AUTO_CAPTION_STYLE).
DEFAULT_STYLE: dict[str, Any] = {
    "enabled": True, "font_name": "Anton", "font_size": 44, "font_color": "#FFFFFF",
    "highlight_color": "#FFE500", "border_color": "#000000", "border_width": 4, "effect": "pop",
    "uppercase": True, "reveal": False, "max_chars": 16, "max_duration": 1.4, "position": "bottom",
    "base_opacity": 1.0, "shadow": 0, "bg_opacity": 0.0, "bg_color": "#000000",
}

_BASE = {**DEFAULT_STYLE, "shadow": 0, "reveal": False, "base_opacity": 1.0}
# OpenShorts' named looks (subtitles.CAPTION_PRESETS), the starting points in the template editor.
PRESETS: dict[str, dict[str, Any]] = {
    "default": dict(DEFAULT_STYLE),
    "hormozi": {**_BASE, "font_name": "Montserrat ExtraBold", "font_size": 44, "highlight_color": "#FFE500",
                "border_width": 4, "shadow": 2, "effect": "pop", "uppercase": True, "reveal": True, "max_chars": 9},
    "pill": {**_BASE, "font_name": "Montserrat ExtraBold", "font_size": 44, "highlight_color": "#7C3AED",
             "border_width": 3, "effect": "highlight", "uppercase": True, "max_chars": 9},
    "lime": {**_BASE, "font_name": "Montserrat ExtraBold", "font_size": 44, "highlight_color": "#A3FF12",
             "border_width": 3, "effect": "highlight", "uppercase": True, "max_chars": 9},
    "oneword": {**_BASE, "font_name": "Anton", "font_size": 70, "highlight_color": "#FFFFFF",
                "border_width": 5, "effect": "pop", "uppercase": True, "max_chars": 1},
    "clean": {**_BASE, "font_name": "Montserrat ExtraBold", "font_size": 34, "highlight_color": "#FFFFFF",
              "border_width": 0, "shadow": 2, "effect": "none", "uppercase": False, "base_opacity": 0.7,
              "max_chars": 12},
}

HOOK_STYLES = ("box", "outline", "plain")
# OpenShorts' own default ("pill"): a white box, black Montserrat ExtraBold, 5 seconds at the top.
DEFAULT_HOOK: dict[str, Any] = {
    "enabled": True, "style": "box", "font_name": "Montserrat ExtraBold", "font_size": 30,
    "text_color": "#000000", "box_color": "#FFFFFF", "outline_color": "#000000", "position": "top",
    "duration": 5.0, "uppercase": False,
}
HOOK_MARGIN = {"top": 38, "middle": 0, "bottom": 88}     # bottom sits above the captions
# Emoji: the burned line has no emoji font, so they're left out of it (the saved text keeps them).
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F\u200D\u20E3]")

_HEX = re.compile(r"^#?([0-9A-Fa-f]{6})$")
_FONT_SAFE = re.compile(r"[^A-Za-z0-9 _-]")


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def color(value: Any, default: str) -> str:
    m = _HEX.match(str(value or "").strip())
    return f"#{m.group(1).upper()}" if m else default


def normalize_style(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A complete, safe caption style from whatever the editor sent."""
    raw = raw or {}
    d = DEFAULT_STYLE
    return {
        "enabled": bool(raw.get("enabled", d["enabled"])),
        "font_name": _FONT_SAFE.sub("", str(raw.get("font_name") or d["font_name"]))[:60] or d["font_name"],
        "font_size": int(_clamp(raw.get("font_size"), 12, 120, d["font_size"])),
        "font_color": color(raw.get("font_color"), d["font_color"]),
        "highlight_color": color(raw.get("highlight_color"), d["highlight_color"]),
        "border_color": color(raw.get("border_color"), d["border_color"]),
        "border_width": int(_clamp(raw.get("border_width"), 0, 10, d["border_width"])),
        "effect": raw.get("effect") if raw.get("effect") in EFFECTS else d["effect"],
        "uppercase": bool(raw.get("uppercase", d["uppercase"])),
        "reveal": bool(raw.get("reveal", d["reveal"])),
        "max_chars": int(_clamp(raw.get("max_chars"), 1, 40, d["max_chars"])),
        "max_duration": _clamp(raw.get("max_duration"), 0.5, 5.0, d["max_duration"]),
        "position": raw.get("position") if raw.get("position") in POSITIONS else d["position"],
        "base_opacity": _clamp(raw.get("base_opacity"), 0.05, 1.0, d["base_opacity"]),
        "shadow": int(_clamp(raw.get("shadow"), 0, 6, d["shadow"])),
        "bg_opacity": _clamp(raw.get("bg_opacity"), 0.0, 1.0, d["bg_opacity"]),
        "bg_color": color(raw.get("bg_color"), d["bg_color"]),
    }


def normalize_hook(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    """A complete, safe hook style."""
    raw = raw or {}
    d = DEFAULT_HOOK
    return {
        "enabled": bool(raw.get("enabled", d["enabled"])),
        "style": raw.get("style") if raw.get("style") in HOOK_STYLES else d["style"],
        "font_name": _FONT_SAFE.sub("", str(raw.get("font_name") or d["font_name"]))[:60] or d["font_name"],
        "font_size": int(_clamp(raw.get("font_size"), 12, 90, d["font_size"])),
        "text_color": color(raw.get("text_color"), d["text_color"]),
        "box_color": color(raw.get("box_color"), d["box_color"]),
        "outline_color": color(raw.get("outline_color"), d["outline_color"]),
        "position": raw.get("position") if raw.get("position") in POSITIONS else d["position"],
        "duration": round(_clamp(raw.get("duration"), 0, 60, d["duration"]), 2),   # 0 = the whole clip
        "uppercase": bool(raw.get("uppercase", d["uppercase"])),
    }


def strip_emoji(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", _EMOJI.sub("", text or "")).strip()


def _hook_style_line(h: dict[str, Any], position: str) -> str:
    size = max(10, int(h["font_size"] * 0.85))
    align = {"top": 8, "middle": 5, "bottom": 2}[position]
    margin = HOOK_MARGIN[position]
    text = _ass_color(h["text_color"])
    if h["style"] == "box":
        box = _ass_color(h["box_color"])
        pad = max(3, round(size * 0.28))
        return (f"Style: Hook,{h['font_name']},{size},{text},{text},{box},{box},1,0,0,0,100,100,0,0,3,{pad},0,"
                f"{align},28,28,{margin},1\n")
    if h["style"] == "outline":
        outline = _ass_color(h["outline_color"])
        return (f"Style: Hook,{h['font_name']},{size},{text},{text},{outline},&H00000000,1,0,0,0,100,100,0,0,1,"
                f"{max(2, round(size * 0.1))},0,{align},28,28,{margin},1\n")
    return (f"Style: Hook,{h['font_name']},{size},{text},{text},&H00000000,&H73000000,1,0,0,0,100,100,0,0,1,0,2,"
            f"{align},28,28,{margin},1\n")


# ---- words ---------------------------------------------------------------------------
def clean_word(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def group_words(words: list[dict[str, Any]], max_chars: int, max_duration: float) -> list[list[dict[str, Any]]]:
    """Words ({w, s, e} in clip seconds) into short on-screen blocks."""
    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for word in words:
        if not clean_word(word.get("w", "")):
            continue
        if not current:
            current = [word]
            continue
        length = sum(len(w["w"]) + 1 for w in current)
        if length + len(word["w"]) > max_chars or word["e"] - current[0]["s"] > max_duration:
            blocks.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        blocks.append(current)
    return blocks


# ---- ASS -------------------------------------------------------------------------------
def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    centis = min(99, int(round((seconds - int(seconds)) * 100)))
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}.{centis:02d}"


def _ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """#RRGGBB -> &HAABBGGRR (style lines; alpha 00 = opaque)."""
    h = color(hex_color, "#FFFFFF")[1:]
    alpha = int(round((1 - opacity) * 255))
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _inline_color(hex_color: str) -> str:
    h = color(hex_color, "#FFD700")[1:]
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _dim(hex_color: str, opacity: float) -> str:
    """Dim inactive words by scaling the color, not alpha (libass blends alpha into the outline)."""
    h = color(hex_color, "#FFFFFF")[1:]
    factor = 0.5 + 0.5 * opacity
    return "#" + "".join(f"{min(255, round(int(h[i:i + 2], 16) * factor)):02X}" for i in (0, 2, 4))


def _luminance(hex_color: str) -> float:
    h = color(hex_color, "#FFFFFF")[1:]
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _escape(text: str) -> str:
    return str(text).replace("\\", "/").replace("{", "(").replace("}", ")")


def build_ass(words: list[dict[str, Any]], style: dict[str, Any],
              split_ranges: Optional[list[tuple[float, float]]] = None,
              hook: Optional[dict[str, Any]] = None) -> Optional[str]:
    """The ASS document for these words in this style, or None when there is nothing to show.
    split_ranges: clip stretches where OpenShorts stacked two speakers; captions sit on the seam there.
    hook: {"text", "style" (normalize_hook), "start", "end", "position"} for the hook line."""
    s = normalize_style(style)
    blocks = group_words(words, s["max_chars"], s["max_duration"]) if s["enabled"] else []
    hook_text = strip_emoji(hook.get("text") or "") if hook else ""
    if not blocks and not hook_text:
        return None
    size = max(10, int(s["font_size"] * 0.85))
    align = {"top": 8, "middle": 5, "bottom": 2}[s["position"]]
    seams = [(float(a), float(b)) for a, b in (split_ranges or [])]
    font = s["font_name"]
    primary = _ass_color(_dim(s["font_color"], s["base_opacity"]))
    shadow = s["shadow"]
    if s["bg_opacity"] > 0:
        border_style, outline, outline_width = 3, _ass_color(s["bg_color"], s["bg_opacity"]), 1
    else:
        border_style, outline = 1, _ass_color(s["border_color"])
        outline_width = s["border_width"] if shadow else max(1, s["border_width"])
    back = _ass_color("#000000", 0.55 if shadow else 0.0)
    hi = _inline_color(s["highlight_color"])
    effect = s["effect"]
    if effect == "glow":
        active = f"{{\\c&HFFFFFF&\\3c{hi}\\bord{max(3, outline_width + 2)}\\blur4}}"
    elif effect == "box":
        active = f"{{\\c&HFFFFFF&\\3c{hi}\\bord{max(4, outline_width + 3)}\\blur0}}"
    elif effect == "highlight":
        active = "{\\rActive}"
    elif effect == "pop":
        active = f"{{\\c{hi}\\fscx90\\fscy90\\t(0,110,\\fscx108\\fscy108)}}"
    else:
        active = f"{{\\c{hi}}}"

    styles = (f"Style: Default,{font},{size},{primary},{primary},{outline},{back},1,0,0,0,100,100,0,0,"
              f"{border_style},{outline_width},{shadow},{align},10,10,{SAFE_MARGIN_V},1\n")
    if effect == "highlight":
        box = _ass_color(s["highlight_color"])
        on_box = _ass_color("#000000" if _luminance(s["highlight_color"]) > 0.6 else s["font_color"])
        pad = max(2, round(size * 0.12))
        styles += (f"Style: Active,{font},{size},{on_box},{on_box},{box},{box},1,0,0,0,100,100,0,0,3,{pad},0,"
                   f"{align},10,10,{SAFE_MARGIN_V},1\n")

    events = []
    for block in blocks:
        for i, word in enumerate(block):
            start = block[0]["s"] if i == 0 else word["s"]
            end = block[i + 1]["s"] if i < len(block) - 1 else block[-1]["e"]
            if end <= start:
                continue
            parts = []
            for j, other in enumerate(block):
                text = _escape(clean_word(other["w"]))
                text = text.upper() if s["uppercase"] else text
                if j == i:
                    parts.append(f"{active}{text}{{\\r}}")
                elif s["reveal"] and j > i:
                    parts.append(f"{{\\alpha&HFF&}}{text}{{\\r}}")
                else:
                    parts.append(text)
            seam = "{\\an5}" if any(a <= start < b for a, b in seams) else ""
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{seam}{' '.join(parts)}")
    if hook_text and hook["end"] > hook["start"]:
        h = normalize_hook(hook.get("style"))
        position = hook.get("position") if hook.get("position") in POSITIONS else h["position"]
        styles += _hook_style_line(h, position)
        text = _escape(hook_text.upper() if h["uppercase"] else hook_text).replace("\n", "\\N")
        events.append(f"Dialogue: 1,{_ass_time(hook['start'])},{_ass_time(hook['end'])},Hook,,0,0,0,,"
                      f"{{\\fad(120,150)}}{text}")
    if not events:
        return None
    return ("[Script Info]\nScriptType: v4.00+\nPlayResY: 288\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
            "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" + styles +
            "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            + "\n".join(events) + "\n")


def write_ass(path: Path, words: list[dict[str, Any]], style: dict[str, Any],
              split_ranges: Optional[list[tuple[float, float]]] = None,
              hook: Optional[dict[str, Any]] = None) -> bool:
    doc = build_ass(words, style, split_ranges, hook)
    if doc is None:
        return False
    path.write_text(doc, encoding="utf-8")
    return True


def words_from_openshorts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenShorts' /transcript words ({text, startMs, endMs}) -> {w, s, e} in seconds."""
    out = []
    for item in items or []:
        text = clean_word(item.get("text", ""))
        try:
            start, end = float(item["startMs"]) / 1000, float(item["endMs"]) / 1000
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            out.append({"w": text, "s": round(start, 3), "e": round(end, 3)})
    return out


def split_ranges_from_layout(doc: Any) -> list[tuple[float, float]]:
    """OpenShorts' <clip>.mp4.layout.json -> stretches with the split (two-speaker) layout."""
    ranges = (doc or {}).get("ranges") if isinstance(doc, dict) else None
    out = []
    for r in ranges or []:
        try:
            if str(r.get("layout")).lower() == "split" and float(r["end"]) > float(r["start"]):
                out.append((float(r["start"]), float(r["end"])))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out
