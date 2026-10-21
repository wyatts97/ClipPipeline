"""The clip editor's edits: which parts of the clip to keep, and zoom punch-ins.

Stored per clip (clips.edit, JSON):
    {"segments": [[start, end], ...],              # kept stretches, in order
     "zooms": [{"s": start, "e": end, "scale": 1.15}, ...],
     "hook": {"enabled": true, "s": 0, "e": 4.5, "position": "top"}}   # optional: this clip's hook line
                                                    # (e null = to the end; missing keys = the template's)

Every time here, like the clip's words and hand-placed sound effects, is on the timeline of the
clip without captions (data/clean/...). The render plays the segments back to back; map_time and
friends move words, effects and zooms onto that shorter timeline.
"""
from __future__ import annotations

from typing import Any, Optional

MIN_SEGMENT = 0.3      # seconds; shorter kept bits are dropped
MAX_SEGMENTS = 40
MAX_ZOOMS = 40
ZOOM_EASE = 0.12       # seconds to punch in


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def full(duration: float) -> dict[str, Any]:
    return {"segments": [[0.0, round(duration, 3)]], "zooms": []}


def normalize(raw: Optional[dict[str, Any]], duration: float) -> dict[str, Any]:
    """A safe edit for a clip this long: segments sorted, clamped, merged; zooms clamped."""
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
            zooms.append({"s": round(start, 3), "e": round(end, 3),
                          "scale": round(min(2.0, max(1.05, _f(z.get("scale"), 1.15))), 3)})
    zooms.sort(key=lambda z: z["s"])
    out: dict[str, Any] = {"segments": merged[:MAX_SEGMENTS] or full(duration)["segments"], "zooms": zooms}
    hook = normalize_hook(raw.get("hook"), duration)
    if hook:
        out["hook"] = hook
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


def hook_window(edit: dict[str, Any], template_duration: float, out_duration: float) -> tuple[float, float]:
    """When the hook shows, on the edited timeline: the clip's own window if set, else from the start
    for the template's duration (0 = the whole clip)."""
    hook = edit.get("hook") or {}
    if "s" in hook:
        end = hook.get("e")
        parts = map_range(hook["s"], end if end is not None else float("inf"), edit["segments"])
        if not parts:
            return 0.0, 0.0
        return parts[0][0], (parts[-1][1] if end is not None else out_duration)
    return 0.0, (out_duration if template_duration <= 0 else min(out_duration, template_duration))


def is_cut(edit: dict[str, Any], duration: float) -> bool:
    """True when the edit removes something (otherwise the video needs no trimming)."""
    segs = edit["segments"]
    return not (len(segs) == 1 and segs[0][0] <= 0.05 and segs[0][1] >= duration - 0.05)


def output_duration(edit: dict[str, Any]) -> float:
    return round(sum(e - s for s, e in edit["segments"]), 3)


def map_time(t: float, segments: list[list[float]]) -> Optional[float]:
    """Where source time t lands in the edited clip, or None when it was cut."""
    offset = 0.0
    for start, end in segments:
        if start <= t < end or (t == end and (start, end) == tuple(segments[-1])):
            return round(offset + t - start, 3)
        offset += end - start
    return None


def map_range(start: float, end: float, segments: list[list[float]]) -> list[tuple[float, float]]:
    """The kept parts of [start, end], on the edited timeline (a cut can split it)."""
    out, offset = [], 0.0
    for s, e in segments:
        lo, hi = max(start, s), min(end, e)
        if hi > lo:
            out.append((round(offset + lo - s, 3), round(offset + hi - s, 3)))
        offset += e - s
    return out


def map_words(words: list[dict[str, Any]], segments: list[list[float]]) -> list[dict[str, Any]]:
    """Words whose start was kept, moved onto the edited timeline (ends clipped to their segment)."""
    out = []
    for w in words:
        parts = map_range(w["s"], w["e"], segments)
        if parts and map_time(w["s"], segments) is not None:
            out.append({**w, "s": parts[0][0], "e": parts[0][1]})
    return out


def map_points(items: list[dict[str, Any]], segments: list[list[float]], key: str = "t") -> list[dict[str, Any]]:
    """Effects or moments ({key: time, ...}) that were kept, moved onto the edited timeline."""
    out = []
    for item in items or []:
        t = map_time(_f(item.get(key)), segments)
        if t is not None:
            out.append({**item, key: t})
    return out


def zoom_ranges(edit: dict[str, Any]) -> list[tuple[float, float, float]]:
    """(start, end, scale) on the edited timeline."""
    out = []
    for z in edit["zooms"]:
        for start, end in map_range(z["s"], z["e"], edit["segments"]):
            out.append((start, end, z["scale"]))
    return out


def shift(edit: Optional[dict[str, Any]], sfx: list[dict[str, Any]], by: float, old_duration: float,
          new_duration: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """After OpenShorts re-cut the clip with `by` seconds added at the start: move every time along.
    An edit that kept the whole old clip keeps the whole new one."""
    if not edit or not is_cut(edit, old_duration):
        moved = {"segments": [[0.0, round(new_duration, 3)]], "zooms": [
            {**z, "s": z["s"] + by, "e": z["e"] + by} for z in (edit or {}).get("zooms", [])]}
    else:
        moved = {"segments": [[s + by, e + by] for s, e in edit["segments"]],
                 "zooms": [{**z, "s": z["s"] + by, "e": z["e"] + by} for z in edit["zooms"]]}
    hook = dict((edit or {}).get("hook") or {})
    if "s" in hook:
        hook["s"] = hook["s"] + by
        hook["e"] = None if hook.get("e") is None else hook["e"] + by
    if hook:
        moved["hook"] = hook
    return (normalize(moved, new_duration),
            [{**e, "t": round(_f(e.get("t")) + by, 3)} for e in sfx if 0 <= _f(e.get("t")) + by < new_duration])
