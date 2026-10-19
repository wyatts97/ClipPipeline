"""Caption fonts: the ones bundled in clipbot/fonts plus your own in data/fonts.

Captions name a font by its family (as libass looks it up), so each file's family name is read
from its 'name' table. Renders get one folder holding both sets (libass takes a single fontsdir).
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path
from typing import Optional

from .config import Settings

FONT_EXTS = (".ttf", ".otf")
BUNDLED_DIR = Path(__file__).resolve().parents[1] / "fonts"


def family_name(path: Path) -> Optional[str]:
    """The font's family as libass matches it (name ID 1, the legacy family: "Montserrat ExtraBold"
    for that static weight), or None when the file isn't a readable TrueType/OpenType font."""
    try:
        data = path.read_bytes()
        if data[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            return None
        (num_tables,) = struct.unpack(">H", data[4:6])
        name_offset = None
        for i in range(num_tables):
            tag, _, offset, _ = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            if tag == b"name":
                name_offset = offset
        if name_offset is None:
            return None
        _, count, strings = struct.unpack(">HHH", data[name_offset:name_offset + 6])
        found: dict[tuple[int, int], str] = {}
        for i in range(count):
            rec = name_offset + 6 + 12 * i
            platform, encoding, language, name_id, length, offset = struct.unpack(">HHHHHH", data[rec:rec + 12])
            if name_id != 1:
                continue
            raw = data[name_offset + strings + offset: name_offset + strings + offset + length]
            if platform == 3 or platform == 0:                    # Windows / Unicode: UTF-16BE
                text = raw.decode("utf-16-be", errors="ignore")
                found[(0 if language in (0x409, 0) else 1, platform)] = text
            elif platform == 1:                                  # Macintosh Roman
                found[(2, platform)] = raw.decode("mac_roman", errors="ignore")
        return found[min(found)].strip() if found else None
    except (OSError, struct.error, IndexError):
        return None


def list_fonts(settings: Settings) -> list[dict]:
    """[{family, file, bundled}] for every usable font, bundled first."""
    out, seen = [], set()
    for folder, bundled in ((BUNDLED_DIR, True), (settings.data_dir / "fonts", False)):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in FONT_EXTS:
                continue
            family = family_name(path)
            if family and family.lower() not in seen:
                seen.add(family.lower())
                out.append({"family": family, "file": path.name, "bundled": bundled})
    return out


def font_file(settings: Settings, name: str) -> Optional[Path]:
    """A font file by its file name, from either folder."""
    for folder in (settings.data_dir / "fonts", BUNDLED_DIR):
        path = folder / Path(name).name
        if path.suffix.lower() in FONT_EXTS and path.is_file():
            return path
    return None


def render_fonts_dir(settings: Settings) -> Path:
    """One folder with every font, for ffmpeg's fontsdir. Kept in step with the two sources."""
    target = settings.folder("tmp") / "fonts"
    target.mkdir(parents=True, exist_ok=True)
    wanted = {}
    for folder in (BUNDLED_DIR, settings.data_dir / "fonts"):
        if folder.is_dir():
            for path in folder.iterdir():
                if path.suffix.lower() in FONT_EXTS:
                    wanted[path.name] = path
    for name, src in wanted.items():
        dst = target / name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copyfile(src, dst)
    for old in target.iterdir():
        if old.name not in wanted:
            old.unlink(missing_ok=True)
    return target
