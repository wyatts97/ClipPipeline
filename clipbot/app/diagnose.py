"""Turn OpenShorts' log of a failed job into a reason a person can act on.

OpenShorts ends every failed job with the same few lines ("raise RuntimeError(...",
"Process failed with exit code 1"); the actual cause is printed earlier.
"""
from __future__ import annotations

# Lines OpenShorts prints when something specific went wrong.
_REASON_MARKERS = ("❌", "🚫", "🔇", "returned no clips", "returned no usable clips", "RESOURCE_EXHAUSTED")
_NOISE = ("raise RuntimeError(", "Process failed with exit code", "Traceback (most recent call last)")

# (substring in the log, advice) - first match wins.
_HINTS = (
    (("RESOURCE_EXHAUSTED", "429 Too Many", "code: 429", "exceeded your current quota"),
     "Gemini's quota or rate limit was hit. Wait a few minutes and retry, or enable billing on the "
     "key at aistudio.google.com (flash-lite costs cents per stream)."),
    (("API key not valid", "API_KEY_INVALID", "PERMISSION_DENIED", "GEMINI_API_KEY not found"),
     "Gemini refused the API key. Check GEMINI_API_KEY (bash setup.sh lets you paste a new one)."),
    (("not found for API version", "NOT_FOUND"),
     "The Gemini model isn't available to this key. Set GEMINI_MODEL in .env to one it can use."),
    (("2-pass returned no clips", "Vision pass returned no usable clips"),
     "Gemini found nothing it rated clip-worthy in this part. Try a wider window (➕ Wider ×2) or "
     "another moment; gameplay with little talking is the usual case."),
    (("no usable speech", "switching to visual analysis"),
     "There's little speech here, so OpenShorts judged the picture instead. A wider window with "
     "more talking usually works better."),
    (("blocked", "SAFETY"),
     "Gemini's safety filter refused this content."),
)


def failure_reason(lines: list[str]) -> str:
    """The lines that explain the failure, falling back to the last few lines."""
    tail = [str(line).strip() for line in lines[-80:] if str(line).strip()]
    picked: list[str] = []
    for line in tail:
        if any(m in line for m in _REASON_MARKERS) and line not in picked:
            picked.append(line)
    final = next((l for l in reversed(tail) if "Error:" in l and not any(n in l for n in _NOISE)), None)
    if final and final not in picked:
        picked.append(final)
    if not picked:
        picked = [l for l in tail if not any(n in l for n in _NOISE)][-3:] or tail[-3:]
    return " | ".join(picked[-4:])[:1500] or "no log output"


def failure_hint(lines: list[str]) -> str | None:
    text = "\n".join(str(line) for line in lines[-80:])
    for needles, hint in _HINTS:
        if any(n in text for n in needles):
            return hint
    return None
