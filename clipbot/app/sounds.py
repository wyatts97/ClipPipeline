"""The sound library: the files in data/sounds/ and what's known about each (sounds.json: category,
where it came from, its license, a ★), plus ways to fill it:

- the meme pack: popular meme sounds from Myinstants (vine boom, HUH, wow, bruh, …) downloaded
  onto this server with one click. They're not licensed, so they're never stored in the repo;
  platforms rarely claim them, but posting them is on you.
- search on Myinstants (more memes) and on Freesound, CC0 only, so safe to post anywhere
  (needs a free FREESOUND_API_KEY).

Templates and clips name a sound by its file name without the extension (templates.find_sound).
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Optional

import httpx

from . import templates
from .config import Settings

log = logging.getLogger("clipbot.sounds")

CATEGORIES = ("impact", "reaction", "fail", "hype", "transition", "funny", "suspense", "music", "other")
MYINSTANTS = "https://www.myinstants.com"
FREESOUND = "https://freesound.org/apiv2"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) clipbot"}
MAX_BYTES = 12 * 1024 * 1024
CC0 = "Creative Commons 0"

# name, category, Myinstants page. Checked against the site (Oct 2026); a page that's gone is
# reported as failed, the rest still install.
MEME_PACK: tuple[tuple[str, str, str], ...] = (
    ("vine-boom", "impact", "vine-boom-sound-70972"),
    ("vine-boom-loud", "impact", "vine-boom-loud-33438"),
    ("metal-pipe", "impact", "metal-pipe-falling-sound-effect-7737"),
    ("taco-bell-bong", "impact", "taco-bell-bong-42481"),
    ("bruh", "reaction", "bruh-sound-effect-26614"),
    ("huh", "reaction", "huh-cat-21280"),
    ("wow", "reaction", "wow-owen-wilson-61924"),
    ("oh-no", "reaction", "oh-no-no-no-tik-tok-song-sound-effect-63466"),
    ("emotional-damage", "reaction", "emotional-damage-meme-74555"),
    ("oh-hell-nah", "reaction", "oh-my-god-bro-oh-hell-nah-man-42939"),
    ("what-the-hell", "reaction", "what-the-hell-meme-sound-effect-95969"),
    ("sad-violin", "fail", "sad-violin-the-meme-one"),
    ("sad-trombone", "fail", "sad-trombone"),
    ("windows-error", "fail", "windows-xp-error"),
    ("curb-your-enthusiasm", "fail", "curb-your-enthusiasm-26943"),
    ("dun-dun-dun", "suspense", "dun-dun-dunnnnnnnn-68584"),
    ("suspense", "suspense", "cinematic-suspense-riser-10260"),
    ("airhorn", "hype", "mlg-air-horn"),
    ("hitmarker", "hype", "hitmarker-noise"),
    ("anime-wow", "hype", "anime-wow"),
    ("lets-go", "hype", "okay-lets-go-99131"),
    ("record-scratch", "transition", "record-scratch"),
    ("whoosh", "transition", "transition-whoosh-87155"),
    ("to-be-continued", "transition", "to-be-continued-jojo"),
    ("ding", "transition", "ding-sound-effect"),
    ("among-us", "funny", "among-us-role-reveal-sound-34956"),
    ("fart", "funny", "fart-with-reverb-17715"),
    ("boing", "funny", "cartoon-boing-sfx-15600"),
    ("spongebob-disappointed", "funny", "spongebob-disappointed-7369"),
    ("galaxy-brain", "funny", "galaxy-brain-meme-2099"),
    ("rizz", "funny", "rizz-sound-effect-54189"),
    ("cartoon-run", "funny", "cartoon-running-20646"),
)
# Licensed music the built-in templates use: downloaded from its author's own site.
PACK_MUSIC = (
    ("sneaky-snitch", "music", "https://incompetech.com/music/royalty-free/mp3-royaltyfree/Sneaky%20Snitch.mp3",
     "CC BY 4.0", "Kevin MacLeod (incompetech.com)"),
)

_OG_AUDIO = re.compile(r'og:audio"\s+content="([^"]+\.mp3)"')
_SEARCH_ITEM = re.compile(r"play\('(/media/sounds/[^']+?\.mp3)'[^)]*?'([\w-]+)'\)[\s\S]*?"
                          r'class="instant-link[^"]*">([^<]+)</a>')
_lock = threading.Lock()


class SoundError(RuntimeError):
    pass


# ---- what's known about each sound --------------------------------------------------------
def folder(settings: Settings) -> Path:
    return settings.folder("sounds")


def load_meta(settings: Settings) -> dict[str, dict[str, Any]]:
    path = folder(settings) / "sounds.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_meta(settings: Settings, name: str, **fields: Any) -> dict[str, Any]:
    with _lock:
        meta = load_meta(settings)
        entry = {**meta.get(name, {}), **{k: v for k, v in fields.items() if v is not None}}
        if entry.get("category") not in CATEGORIES:
            entry["category"] = "other"
        meta[name] = entry
        (folder(settings) / "sounds.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    return entry


def drop_meta(settings: Settings, name: str) -> None:
    with _lock:
        meta = load_meta(settings)
        if meta.pop(name, None) is not None:
            (folder(settings) / "sounds.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False),
                                                          encoding="utf-8")


def describe(settings: Settings, name: str, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """A sound's category, source, license, author and ★ (defaults for one nobody described)."""
    entry = (load_meta(settings) if meta is None else meta).get(name, {})
    return {"category": entry.get("category") if entry.get("category") in CATEGORIES else "other",
            "source": entry.get("source") or "upload", "license": entry.get("license"),
            "author": entry.get("author"), "favorite": bool(entry.get("favorite"))}


# ---- downloading ----------------------------------------------------------------------------
def _client(client: Optional[httpx.AsyncClient]) -> tuple[httpx.AsyncClient, bool]:
    if client is not None:
        return client, False
    return httpx.AsyncClient(timeout=60, follow_redirects=True, headers=HEADERS), True


def free_name(settings: Settings, wanted: str) -> str:
    """wanted, or wanted-2, -3… when a sound by that name exists."""
    base = templates.sound_name(wanted) or "sound"
    name, n = base, 2
    while templates.find_sound(settings, name):
        name, n = f"{base}-{n}", n + 1
    return name


async def _download(client: httpx.AsyncClient, url: str, dst: Path) -> None:
    async with client.stream("GET", url) as resp:
        if resp.status_code >= 400:
            raise SoundError(f"{resp.status_code} from {url}")
        size, chunks = 0, []
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise SoundError("bigger than 12 MB")
            chunks.append(chunk)
    data = b"".join(chunks)
    if not (data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa")):
        raise SoundError("not an MP3")
    dst.write_bytes(data)


async def myinstants_mp3(client: httpx.AsyncClient, slug: str) -> str:
    """The MP3 of a Myinstants page (its og:audio)."""
    resp = await client.get(f"{MYINSTANTS}/en/instant/{slug}/")
    if resp.status_code >= 400:
        raise SoundError(f"page {slug} gave {resp.status_code}")
    match = _OG_AUDIO.search(resp.text)
    if not match:
        raise SoundError(f"no sound on page {slug}")
    url = match.group(1)
    return url if url.startswith("http") else MYINSTANTS + url


async def install_pack(settings: Settings, client: Optional[httpx.AsyncClient] = None) -> dict[str, Any]:
    """Download the meme pack (and the templates' music) into data/sounds. Sounds already there are
    kept. {"added", "kept", "failed": {name: why}}."""
    client, own = _client(client)
    added, kept, failed = [], [], {}
    items = ([{"name": n, "category": c, "slug": s} for n, c, s in MEME_PACK]
             + [{"name": n, "category": c, "url": u, "license": lic, "author": by} for n, c, u, lic, by in PACK_MUSIC])
    try:
        for item in items:
            name = item["name"]
            if templates.find_sound(settings, name):
                kept.append(name)
                continue
            try:
                if "url" in item:
                    url, source, page = item["url"], "incompetech", item["url"]
                else:
                    url, source = await myinstants_mp3(client, item["slug"]), "myinstants"
                    page = f"{MYINSTANTS}/en/instant/{item['slug']}/"
                await _download(client, url, folder(settings) / f"{name}.mp3")
                set_meta(settings, name, category=item["category"], source=source, license=item.get("license"),
                         author=item.get("author"), page=page)
                added.append(name)
            except (SoundError, httpx.HTTPError) as e:
                failed[name] = str(e) or type(e).__name__
                log.warning("meme pack: %s failed: %s", name, failed[name])
    finally:
        if own:
            await client.aclose()
    log.info("meme pack: %d added, %d already there, %d failed", len(added), len(kept), len(failed))
    return {"added": added, "kept": kept, "failed": failed}


# ---- search ---------------------------------------------------------------------------------
async def search(settings: Settings, query: str, source: str,
                 client: Optional[httpx.AsyncClient] = None) -> list[dict[str, Any]]:
    """Sounds to import: [{source, id, title, preview_url, duration, license, author}]."""
    query = query.strip()[:100]
    if not query:
        return []
    client, own = _client(client)
    try:
        if source == "freesound":
            if not settings.freesound_api_key:
                raise SoundError("Freesound search needs FREESOUND_API_KEY (a free key from freesound.org/apiv2/apply)")
            resp = await client.get(f"{FREESOUND}/search/text/", params={
                "query": query, "filter": f'license:"{CC0}"', "page_size": 24,
                "fields": "id,name,previews,duration,username,license", "token": settings.freesound_api_key})
            if resp.status_code >= 400:
                raise SoundError(f"Freesound answered {resp.status_code}")
            return [{"source": "freesound", "id": str(r["id"]), "title": r.get("name") or str(r["id"]),
                     "preview_url": (r.get("previews") or {}).get("preview-hq-mp3"),
                     "duration": r.get("duration"), "license": "CC0", "author": r.get("username")}
                    for r in resp.json().get("results") or [] if (r.get("previews") or {}).get("preview-hq-mp3")]
        resp = await client.get(f"{MYINSTANTS}/en/search/", params={"name": query})
        if resp.status_code >= 400:
            raise SoundError(f"Myinstants answered {resp.status_code}")
        seen, out = set(), []
        for mp3, slug, title in _SEARCH_ITEM.findall(resp.text):
            if slug not in seen:
                seen.add(slug)
                out.append({"source": "myinstants", "id": slug, "title": _unescape(title.strip()),
                            "preview_url": MYINSTANTS + mp3, "duration": None, "license": None, "author": None})
        return out[:24]
    except httpx.HTTPError as e:
        raise SoundError(f"{source} couldn't be reached: {e}") from e
    finally:
        if own:
            await client.aclose()


async def import_sound(settings: Settings, item: dict[str, Any], name: Optional[str] = None,
                       category: Optional[str] = None, client: Optional[httpx.AsyncClient] = None) -> str:
    """Download a search result into the library; its name."""
    source, url = item.get("source"), str(item.get("preview_url") or "")
    if source == "myinstants" and not url.startswith(MYINSTANTS + "/media/"):
        raise SoundError("not a Myinstants sound")
    if source == "freesound" and not re.match(r"^https://(cdn\.)?freesound\.org/", url):
        raise SoundError("not a Freesound preview")
    if source not in ("myinstants", "freesound"):
        raise SoundError("unknown source")
    name = free_name(settings, name or re.sub(r"[^a-z0-9]+", "-", str(item.get("title") or "").lower()).strip("-")[:40])
    client, own = _client(client)
    try:
        await _download(client, url, folder(settings) / f"{name}.mp3")
    except httpx.HTTPError as e:
        raise SoundError(f"download failed: {e}") from e
    finally:
        if own:
            await client.aclose()
    set_meta(settings, name, category=category if category in CATEGORIES else "other", source=source,
             license="CC0" if source == "freesound" else None, author=item.get("author"),
             page=(f"https://freesound.org/s/{item.get('id')}/" if source == "freesound"
                   else f"{MYINSTANTS}/en/instant/{item.get('id')}/"))
    return name


def _unescape(text: str) -> str:
    return (text.replace("&#x27;", "'").replace("&#39;", "'").replace("&quot;", '"')
            .replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
