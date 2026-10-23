"""The editor's pictures: your images and GIFs in data/images/ (images.json: where each came from, its
size), the bundled emoji stickers (clipbot/stickers, Noto Emoji), and frames grabbed from a clip.

A clip's overlays name a picture by its name (the library, the file name without the extension) or
"sticker:<name>" (a bundled sticker).
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from . import media
from .config import Settings

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
STICKERS_DIR = Path(__file__).resolve().parents[1] / "stickers"
STICKER = "sticker:"
MAX_BYTES = 15 * 1024 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) clipbot"}
_NAME = re.compile(r"[^\w .-]+")
_lock = threading.Lock()


class ImageError(RuntimeError):
    pass


def folder(settings: Settings) -> Path:
    return settings.folder("images")


def image_name(value: Any) -> Optional[str]:
    """A picture's name: its file name without the extension ("my face.png" -> "my face")."""
    text = Path(str(value or "").strip()).name
    if Path(text).suffix.lower() in IMAGE_EXTS:
        text = Path(text).stem
    name = _NAME.sub("", text).strip(" .")
    return name[:60] or None


def kind(data: bytes) -> Optional[str]:
    """The picture's extension from its first bytes, or None when it isn't PNG, JPEG, GIF or WebP."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def stickers() -> list[str]:
    return sorted(p.stem for p in STICKERS_DIR.glob("*.png")) if STICKERS_DIR.is_dir() else []


def find_image(settings: Settings, ref: Optional[str]) -> Optional[Path]:
    """The file a picture reference names: a sticker, or data/images/<name>.<ext> (case-insensitive)."""
    if not ref:
        return None
    if ref.startswith(STICKER):
        name = image_name(ref[len(STICKER):])
        path = STICKERS_DIR / f"{name}.png"
        return path if name and path.is_file() else None
    lib = folder(settings)
    for path in sorted(lib.iterdir()):
        if path.suffix.lower() in IMAGE_EXTS and path.stem.lower() == ref.lower():
            return path
    return None


def free_name(settings: Settings, wanted: str) -> str:
    base = image_name(wanted) or "picture"
    name, n = base, 2
    while find_image(settings, name):
        name, n = f"{base}-{n}", n + 1
    return name


# ---- what's known about each picture -----------------------------------------------------------
def load_meta(settings: Settings) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads((folder(settings) / "images.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_meta(settings: Settings, meta: dict[str, Any]) -> None:
    (folder(settings) / "images.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")


def set_meta(settings: Settings, name: str, **fields: Any) -> None:
    with _lock:
        meta = load_meta(settings)
        meta[name] = {**meta.get(name, {}), **{k: v for k, v in fields.items() if v is not None}}
        _save_meta(settings, meta)


def drop_meta(settings: Settings, name: str) -> None:
    with _lock:
        meta = load_meta(settings)
        if meta.pop(name, None) is not None:
            _save_meta(settings, meta)


async def probe(path: Path) -> dict[str, Any]:
    """{width, height, animated}; ImageError when ffmpeg can't read it as a picture."""
    try:
        out = await media.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                               "-show_entries", "stream=width,height,nb_read_packets", "-of", "csv=p=0",
                               str(path)], timeout=60)
        w, h, frames = (out.strip().splitlines() or [""])[0].split(",")[:3]
        return {"width": int(w), "height": int(h), "animated": path.suffix.lower() == ".gif" and int(frames or 1) > 1}
    except (media.MediaError, ValueError) as e:
        raise ImageError("That isn't a picture ffmpeg can read.") from e


def item(settings: Settings, path: Path, meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    entry = (load_meta(settings) if meta is None else meta).get(path.stem, {})
    return {"name": path.stem, "ref": path.stem, "file": path.name, "size": path.stat().st_size,
            "width": entry.get("width"), "height": entry.get("height"), "animated": bool(entry.get("animated")),
            "source": entry.get("source") or "upload", "url": f"/api/images/{path.stem}/file"}


def items(settings: Settings) -> list[dict[str, Any]]:
    meta = load_meta(settings)
    return [item(settings, p, meta) for p in sorted(folder(settings).iterdir(), key=lambda p: p.stem.lower())
            if p.suffix.lower() in IMAGE_EXTS]


async def _store(settings: Settings, data: bytes, name: str, **meta: Any) -> str:
    ext = kind(data)
    if not ext:
        raise ImageError("Pictures can be PNG, JPEG, GIF or WebP.")
    if len(data) > MAX_BYTES:
        raise ImageError("That picture is bigger than 15 MB.")
    path = folder(settings) / f"{name}{ext}"
    path.write_bytes(data)
    try:
        info = await probe(path)
    except ImageError:
        path.unlink(missing_ok=True)
        raise
    set_meta(settings, name, **info, **meta)
    return name


async def save_upload(settings: Settings, filename: str, data: bytes) -> str:
    """A picture you uploaded; one of the same name is replaced."""
    name = image_name(filename)            # never has a colon, so never looks like "sticker:…"
    if not name:
        raise ImageError("Name the picture something else.")
    for old in folder(settings).iterdir():
        if old.suffix.lower() in IMAGE_EXTS and old.stem.lower() == name.lower():
            old.unlink()
    drop_meta(settings, name)
    return await _store(settings, data, name, source="upload")


def _public(host: str) -> None:
    """Refuse addresses on this server's own network: the dashboard fetches, so it mustn't be a way in."""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise ImageError(f"Couldn't find {host}.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ImageError("That address is on a private network.")


async def import_url(settings: Settings, url: str, name: Optional[str] = None,
                     client: Optional[httpx.AsyncClient] = None) -> str:
    """Download a picture from the web into the library (http/https, public addresses, 15 MB at most)."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=60, headers=HEADERS)
    try:
        for _ in range(4):                                  # follow up to 3 redirects, checking each hop
            parts = urlparse(url)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ImageError("Give a web address (http or https).")
            await asyncio.to_thread(_public, parts.hostname)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = urljoin(url, resp.headers["location"])
                    continue
                if resp.status_code >= 400:
                    raise ImageError(f"{parts.hostname} answered {resp.status_code}.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ImageError("That picture is bigger than 15 MB.")
                    chunks.append(chunk)
            data = b"".join(chunks)
            wanted = name or Path(parts.path).stem or parts.hostname
            return await _store(settings, data, free_name(settings, wanted), source="url", page=url)
        raise ImageError("Too many redirects.")
    except httpx.HTTPError as e:
        raise ImageError(f"Couldn't download it: {e}") from e
    finally:
        if own:
            await client.aclose()


async def grab_frame(settings: Settings, source: Path, t: float, crop: Optional[dict[str, float]] = None,
                     name: Optional[str] = None) -> str:
    """The frame of a video at t (optionally a box of it: x, y, w, h as shares of the frame) as a PNG."""
    vf = []
    if crop:
        x, y = (min(0.95, max(0.0, float(crop.get(k, 0)))) for k in ("x", "y"))
        w, h = min(1 - x, max(0.05, float(crop.get("w", 1)))), min(1 - y, max(0.05, float(crop.get("h", 1))))
        vf = ["-vf", f"crop=trunc(iw*{w:.4f}):trunc(ih*{h:.4f}):trunc(iw*{x:.4f}):trunc(ih*{y:.4f})"]
    name = free_name(settings, name or f"frame {t:.1f}s")
    path = folder(settings) / f"{name}.png"
    try:
        await media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(source),
                         "-frames:v", "1", *vf, str(path)], timeout=60)
    except media.MediaError as e:
        raise ImageError(f"Couldn't grab that frame: {e}") from e
    if not path.is_file():
        raise ImageError("That time is past the end of the clip.")
    set_meta(settings, name, **(await probe(path)), source="frame")
    return name
