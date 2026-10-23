"""The clip editor's edits: which parts of the clip to keep, zooms, freeze frames and transitions.

Stored per clip (clips.edit, JSON):
    {"segments": [[start, end], ...],              # kept stretches, in order
     "zooms": [{"s": start, "e": end, "scale": 1.15,
                "style": "smooth", "x": 0.3, "y": 0.4}, ...],  # optional: punch, centred
     "freezes": [{"t": 2.4, "d": 1.5, "style": "gray"}, ...],   # hold the frame at t for d seconds
     "transitions": [{"after": 3.1, "type": "fade", "d": 0.4}, ...],   # at the cut where a kept part ends
     "overlays": [{"image": "sticker:skull", "s": 1, "e": 3, "x": 0.5, "y": 0.4, "w": 0.3,   # pictures on top:
                   "rot": 0, "opacity": 1, "enter": "pop", "exit": "fade", "motion": "none"}, ...],  # centre, width
     "hook": {"enabled": true, "s": 0, "e": 4.5, "position": "top"}}   # optional: this clip's hook line
                                                    # (e null = to the end; missing keys = the template's)

Every time here, like the clip's words and hand-placed sound effects, is on the timeline of the
clip without captions (data/clean/...). The render plays the segments back to back, holding each
freeze and overlapping the two sides of a smooth transition; timeline() lays that out, and map_time
and friends move words, effects and zooms onto it.
"""
from __future__ import annotations

from typing import Any, Optional, Union

MIN_SEGMENT = 0.3      # seconds; shorter kept bits are dropped
MAX_SEGMENTS = 40
MAX_ZOOMS = 40
MAX_FREEZES = 20
FRAME = 1 / 30
# How a zoom moves (render.zoom_curve has the curves): punch snaps in (the default), smooth eases in
# and out, push creeps in over the whole zoom, shake punches in with a fading camera shake, out
# starts close and pulls back. x, y: the point it zooms toward (0-1, default the centre).
ZOOM_STYLES = ("punch", "smooth", "push", "shake", "out")
# A freeze holds one frame: plain, gray (black and white), zoom (a slow push in), scratch (gray, with
# the record-scratch sound when the library has it).
FREEZE_STYLES = ("plain", "gray", "zoom", "scratch")
FREEZE_SECONDS = (0.3, 5.0)
# Smooth transitions overlap the two sides of a cut (render.XFADE): the clip gets shorter by their length.
SMOOTH_TRANSITIONS = ("fade", "white", "black", "slideleft", "slideright", "slideup", "slidedown", "wipe",
                      "zoomin", "circle", "pixelize", "blur")
# Accents land on the cut without changing timing: a flash, a glitch, a camera shake, a zoom punch.
ACCENT_TRANSITIONS = ("flash", "glitch", "shake", "punch")
TRANSITION_SECONDS = (0.1, 1.0)
# Pictures on top of the clip (render.overlay_graph): how one comes in and goes, and how it moves.
OVERLAY_ENTER = ("none", "fade", "pop", "zoom", "spin", "slide-left", "slide-right", "slide-up", "slide-down")
OVERLAY_MOTION = ("none", "shake", "pulse", "float", "spin")
MAX_OVERLAYS = 20

Timeline = list[dict[str, Any]]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def full(duration: float) -> dict[str, Any]:
    return {"segments": [[0.0, round(duration, 3)]], "zooms": []}


def normalize(raw: Optional[dict[str, Any]], duration: float) -> dict[str, Any]:
    """A safe edit for a clip this long: segments sorted, clamped, merged; zooms clamped; freezes inside
    a kept part; transitions on a cut, no longer than the parts on either side allow."""
    raw = raw or {}
    segs = []
    for item in (raw.get("segments") or [])[:MAX_SEGMENTS * 2]:
        try:
            start, end = max(0.0, _f(item[0])), min(duration, _f(item[1]))
        except (TypeError, IndexError):
            continue
        if round(end - start, 3) >= MIN_SEGMENT:
            segs.append([round(start, 3), round(end, 3)])
    segs.sort()
    merged: list[list[float]] = []
    for start, end in segs:
        if merged and start <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    zooms = []
    for z in (raw.get("zooms") or [])[:MAX_ZOOMS]:
        if not isinstance(z, dict):
            continue
        start, end = max(0.0, _f(z.get("s"))), min(duration, _f(z.get("e")))
        if end - start >= 0.1:
            zoom = {"s": round(start, 3), "e": round(end, 3),
                    "scale": round(min(2.0, max(1.05, _f(z.get("scale"), 1.15))), 3)}
            if z.get("style") in ZOOM_STYLES and z["style"] != "punch":
                zoom["style"] = z["style"]
            for k in ("x", "y"):
                v = round(min(1.0, max(0.0, _f(z.get(k), 0.5))), 3)
                if v != 0.5:
                    zoom[k] = v
            zooms.append(zoom)
    zooms.sort(key=lambda z: z["s"])
    out: dict[str, Any] = {"segments": merged[:MAX_SEGMENTS] or full(duration)["segments"], "zooms": zooms}
    freezes = _freezes(raw.get("freezes"), out["segments"])
    if freezes:
        out["freezes"] = freezes
    transitions = _transitions(raw.get("transitions"), out["segments"], freezes)
    if transitions:
        out["transitions"] = transitions
    overlays = _overlays(raw.get("overlays"), duration)
    if overlays:
        out["overlays"] = overlays
    hook = normalize_hook(raw.get("hook"), duration)
    if hook:
        out["hook"] = hook
    return out


def _freezes(raw: Any, segments: list[list[float]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in (raw or [])[:MAX_FREEZES * 2]:
        if not isinstance(f, dict):
            continue
        t = _f(f.get("t"), -1.0)
        seg = next((s for s in segments if s[0] - 0.001 <= t <= s[1]), None)
        if seg is None:
            continue
        t = round(max(seg[0], min(t, seg[1] - FRAME)), 3)       # a frame that plays: not past the end
        if any(abs(t - x["t"]) < 0.05 for x in out):
            continue
        freeze = {"t": t, "d": round(min(FREEZE_SECONDS[1], max(FREEZE_SECONDS[0], _f(f.get("d"), 1.0))), 3)}
        if f.get("style") in FREEZE_STYLES and f["style"] != "plain":
            freeze["style"] = f["style"]
        out.append(freeze)
    out.sort(key=lambda f: f["t"])
    return out[:MAX_FREEZES]


def _transitions(raw: Any, segments: list[list[float]], freezes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each on a cut (matched to the end of a kept part, within 0.05 s); a smooth one at most 45% of
    the part on either side, so both sides still play on their own."""
    cuts = [s[1] for s in segments[:-1]]
    length = {i: (e - s) + sum(f["d"] for f in freezes if s <= f["t"] < e) for i, (s, e) in enumerate(segments)}
    used: dict[int, float] = {}            # seconds of each part already taken by a transition
    out: list[dict[str, Any]] = []
    for tr in sorted((x for x in raw or [] if isinstance(x, dict)), key=lambda x: _f(x.get("after"))):
        kind, after = tr.get("type"), _f(tr.get("after"), -1.0)
        if kind not in SMOOTH_TRANSITIONS + ACCENT_TRANSITIONS or not cuts:
            continue
        i = min(range(len(cuts)), key=lambda k: abs(cuts[k] - after))
        if abs(cuts[i] - after) > 0.05 or any(x["after"] == cuts[i] for x in out):
            continue
        item: dict[str, Any] = {"after": cuts[i], "type": kind}
        if kind in SMOOTH_TRANSITIONS:
            room = min(length[i] * 0.45 - used.get(i, 0.0), length[i + 1] * 0.45)
            d = min(TRANSITION_SECONDS[1], max(TRANSITION_SECONDS[0], _f(tr.get("d"), 0.4)), room)
            if d < TRANSITION_SECONDS[0]:
                continue                                         # the parts are too short to overlap
            item["d"] = round(d, 3)
            used[i + 1] = d
        out.append(item)
    return out


def _overlays(raw: Any, duration: float) -> list[dict[str, Any]]:
    out = []
    for o in (raw or [])[:MAX_OVERLAYS]:
        if not isinstance(o, dict) or not str(o.get("image") or "").strip():
            continue
        start, end = max(0.0, _f(o.get("s"))), min(duration, _f(o.get("e")))
        if end - start < 0.1:
            continue

        def num(key: str, lo: float, hi: float, default: float) -> float:
            return round(min(hi, max(lo, _f(o.get(key), default))), 3)

        out.append({"image": str(o["image"]).strip()[:80], "s": round(start, 3), "e": round(end, 3),
                    "x": num("x", 0, 1, 0.5), "y": num("y", 0, 1, 0.5), "w": num("w", 0.05, 1, 0.35),
                    "rot": num("rot", -180, 180, 0), "opacity": num("opacity", 0.1, 1, 1),
                    "enter": o.get("enter") if o.get("enter") in OVERLAY_ENTER else "none",
                    "exit": o.get("exit") if o.get("exit") in OVERLAY_ENTER else "none",
                    "motion": o.get("motion") if o.get("motion") in OVERLAY_MOTION else "none"})
    return out


def normalize_hook(raw: Any, duration: float) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    hook: dict[str, Any] = {}
    if isinstance(raw.get("enabled"), bool):
        hook["enabled"] = raw["enabled"]
    if raw.get("s") is not None:
        start = min(max(0.0, _f(raw["s"])), max(0.0, duration - 0.3))
        hook["s"] = round(start, 3)
        hook["e"] = None if raw.get("e") is None else round(min(duration, max(start + 0.3, _f(raw["e"]))), 3)
    if raw.get("position") in ("top", "middle", "bottom"):
        hook["position"] = raw["position"]
    return hook


# ---- the finished clip's timeline -------------------------------------------------------------
def timeline(edit: Union[dict[str, Any], list]) -> Timeline:
    """The kept parts in order: where each starts on the finished clip ("o"), how long it plays there
    ("len": its freezes add time), its freezes, and the transition into it ("into"). A smooth
    transition overlaps a part with the one before, so it starts that much earlier ("x").
    Takes an edit, or bare segments (cuts only)."""
    if not isinstance(edit, dict):
        edit = {"segments": edit}
    into = {tr["after"]: tr for tr in edit.get("transitions") or []}
    freezes = edit.get("freezes") or []
    out: Timeline = []
    o, prev_end = 0.0, None
    for s, e in edit["segments"]:
        tr = into.get(prev_end) if prev_end is not None else None
        x = tr["d"] if tr and tr["type"] in SMOOTH_TRANSITIONS else 0.0
        o -= x
        held = [f for f in freezes if s <= f["t"] < e]
        length = (e - s) + sum(f["d"] for f in held)
        out.append({"s": s, "e": e, "o": round(o, 3), "len": round(length, 3), "x": x, "into": tr,
                    "freezes": held})
        o += length
        prev_end = e
    return out


def _tl(edit: Union[dict[str, Any], list]) -> Timeline:
    if isinstance(edit, list) and edit and isinstance(edit[0], dict):
        return edit                                              # already a timeline
    return timeline(edit)


def _place(seg: dict[str, Any], t: float) -> float:
    """Source time t (inside seg) on the finished clip; at a freeze, where the freeze starts."""
    return seg["o"] + (t - seg["s"]) + sum(f["d"] for f in seg["freezes"] if f["t"] < t)


def is_cut(edit: dict[str, Any], duration: float) -> bool:
    """True when the edit changes the clip's timing (otherwise the video needs no trimming)."""
    segs = edit["segments"]
    return (not (len(segs) == 1 and segs[0][0] <= 0.05 and segs[0][1] >= duration - 0.05)
            or bool(edit.get("freezes")))


def reshapes(edit: dict[str, Any]) -> bool:
    """True when the render needs more than plain cuts: freezes or smooth transitions."""
    return bool(edit.get("freezes")) or any(tr["type"] in SMOOTH_TRANSITIONS for tr in edit.get("transitions") or [])


def output_duration(edit: Union[dict[str, Any], list]) -> float:
    tl = _tl(edit)
    return round(tl[-1]["o"] + tl[-1]["len"], 3) if tl else 0.0


def map_time(t: float, edit: Union[dict[str, Any], list]) -> Optional[float]:
    """Where source time t lands in the edited clip, or None when it was cut."""
    tl = _tl(edit)
    for seg in tl:
        if seg["s"] <= t < seg["e"] or (t == seg["e"] and seg is tl[-1]):
            return round(_place(seg, t), 3)
    return None


def map_range(start: float, end: float, edit: Union[dict[str, Any], list]) -> list[tuple[float, float]]:
    """The kept parts of [start, end], on the edited timeline (a cut can split it; a freeze inside it
    stretches it)."""
    out = []
    for seg in _tl(edit):
        lo, hi = max(start, seg["s"]), min(end, seg["e"])
        if hi > lo:
            out.append((round(_place(seg, lo), 3), round(_place(seg, hi), 3)))
    return out


def map_words(words: list[dict[str, Any]], edit: Union[dict[str, Any], list]) -> list[dict[str, Any]]:
    """Words whose start was kept, moved onto the edited timeline (ends clipped to their segment)."""
    tl = _tl(edit)
    out = []
    for w in words:
        parts = map_range(w["s"], w["e"], tl)
        if parts and map_time(w["s"], tl) is not None:
            out.append({**w, "s": parts[0][0], "e": parts[0][1]})
    return out


def map_points(items: list[dict[str, Any]], edit: Union[dict[str, Any], list], key: str = "t") -> list[dict[str, Any]]:
    """Effects or moments ({key: time, ...}) that were kept, moved onto the edited timeline."""
    tl = _tl(edit)
    out = []
    for item in items or []:
        t = map_time(_f(item.get(key)), tl)
        if t is not None:
            out.append({**item, key: t})
    return out


def zoom_ranges(edit: dict[str, Any]) -> list[dict[str, Any]]:
    """The zooms on the edited timeline; a cut splits one in parts, each moving on its own."""
    tl = timeline(edit)
    out = []
    for z in edit["zooms"]:
        for start, end in map_range(z["s"], z["e"], tl):
            out.append({**z, "s": start, "e": end})
    return out


def overlay_ranges(edit: dict[str, Any]) -> list[dict[str, Any]]:
    """The pictures on the edited timeline: from where the first kept bit of each lands to where the
    last one ends (a picture over a cut stays up through it; over a freeze it stays for the freeze)."""
    tl = timeline(edit)
    out = []
    for o in edit.get("overlays") or []:
        parts = map_range(o["s"], o["e"], tl)
        if parts:
            out.append({**o, "s": parts[0][0], "e": parts[-1][1]})
    return out


def hook_window(edit: dict[str, Any], template_duration: float, out_duration: float) -> tuple[float, float]:
    """When the hook shows, on the edited timeline: the clip's own window if set, else from the start
    for the template's duration (0 = the whole clip)."""
    hook = edit.get("hook") or {}
    if "s" in hook:
        end = hook.get("e")
        parts = map_range(hook["s"], end if end is not None else float("inf"), edit)
        if not parts:
            return 0.0, 0.0
        return parts[0][0], (parts[-1][1] if end is not None else out_duration)
    return 0.0, (out_duration if template_duration <= 0 else min(out_duration, template_duration))


def accents(edit: dict[str, Any]) -> dict[str, list]:
    """What freezes and transitions add on the finished clip: zooms (a freeze's slow push, a shake or
    a zoom punch on a cut), gray stretches, flashes and glitches (times of cuts), and scratches (where
    a scratch freeze wants the record-scratch sound)."""
    out: dict[str, list] = {"zooms": [], "gray": [], "flash": [], "glitch": [], "scratch": []}
    for seg in timeline(edit):
        for f in seg["freezes"]:
            at = round(_place(seg, f["t"]), 3)
            style = f.get("style", "plain")
            if style in ("gray", "scratch"):
                out["gray"].append((at, round(at + f["d"], 3)))
            if style == "scratch":
                out["scratch"].append(at)
            if style == "zoom":
                out["zooms"].append({"s": at, "e": round(at + f["d"], 3), "scale": 1.15, "style": "push"})
        tr = seg["into"]
        if tr and tr["type"] in ACCENT_TRANSITIONS:
            cut = seg["o"]
            if tr["type"] in ("flash", "glitch"):
                out[tr["type"]].append(cut)
            elif tr["type"] == "shake":
                out["zooms"].append({"s": max(0.0, round(cut - 0.05, 3)), "e": round(cut + 0.5, 3), "scale": 1.12,
                                     "style": "shake"})
            else:
                out["zooms"].append({"s": max(0.0, round(cut - 0.2, 3)), "e": round(cut + 0.35, 3), "scale": 1.3,
                                     "style": "smooth"})
    return out


def shift(edit: Optional[dict[str, Any]], sfx: list[dict[str, Any]], by: float, old_duration: float,
          new_duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """After OpenShorts re-cut the clip with `by` seconds added at the start: move every time along.
    An edit that kept the whole old clip keeps the whole new one."""
    edit = edit or {}
    if not edit or not is_cut({"segments": edit.get("segments") or [[0, old_duration]]}, old_duration):
        moved: dict[str, Any] = {"segments": [[0.0, round(new_duration, 3)]]}
    else:
        moved = {"segments": [[s + by, e + by] for s, e in edit["segments"]]}
    moved["zooms"] = [{**z, "s": z["s"] + by, "e": z["e"] + by} for z in edit.get("zooms") or []]
    if edit.get("freezes"):
        moved["freezes"] = [{**f, "t": f["t"] + by} for f in edit["freezes"]]
    if edit.get("transitions"):
        moved["transitions"] = [{**tr, "after": tr["after"] + by} for tr in edit["transitions"]]
    if edit.get("overlays"):
        moved["overlays"] = [{**o, "s": o["s"] + by, "e": o["e"] + by} for o in edit["overlays"]]
    hook = dict(edit.get("hook") or {})
    if "s" in hook:
        hook["s"] = hook["s"] + by
        hook["e"] = None if hook.get("e") is None else hook["e"] + by
    if hook:
        moved["hook"] = hook
    return (normalize(moved, new_duration),
            [{**e, "t": round(_f(e.get("t")) + by, 3)} for e in sfx if 0 <= _f(e.get("t")) + by < new_duration])
