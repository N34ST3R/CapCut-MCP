"""Font catalogue for CapCut text.

CapCut text materials reference a font by absolute file path, so any TrueType/OpenType
file on the machine can be used: Windows fonts plus the fonts CapCut ships or downloads
into its cache. Family and style names are read from each font's name table with Pillow.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import paths

FONT_SUFFIXES = {".ttf", ".otf", ".ttc"}


@dataclass(frozen=True)
class Font:
    family: str
    style: str
    path: str
    source: str

    def as_dict(self) -> dict:
        return {"family": self.family, "style": self.style, "source": self.source, "path": self.path}


def font_dirs() -> list[tuple[Path, str]]:
    """Directories to scan, with a short source label."""
    found: list[tuple[Path, str]] = []
    windows = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    if windows.is_dir():
        found.append((windows, "system"))

    for version in paths.installed_versions():
        resource = version / "Resources" / "Font"
        if resource.is_dir():
            found.append((resource, "capcut"))

    cache = paths.local_appdata() / "CapCut" / "User Data" / "Cache" / "effect"
    if cache.is_dir():
        found.append((cache, "capcut-download"))
    return found


def _font_name(path: Path) -> tuple[str, str] | None:
    """(family, style) from the font file, or None when it cannot be parsed."""
    try:
        from PIL import ImageFont
    except ImportError:  # pragma: no cover - Pillow is a documented requirement
        return None

    for index in range(4 if path.suffix.lower() == ".ttc" else 1):
        try:
            family, style = ImageFont.truetype(str(path), 12, index=index).getname()
        except Exception:
            continue
        if family:
            return str(family), str(style or "Regular")
    return None


@lru_cache(maxsize=1)
def catalogue() -> tuple[Font, ...]:
    """Every usable font file, de-duplicated by family + style (system wins)."""
    entries: dict[tuple[str, str], Font] = {}
    for directory, source in font_dirs():
        try:
            files = sorted(directory.rglob("*"))
        except OSError:
            continue
        for path in files:
            if path.suffix.lower() not in FONT_SUFFIXES:
                continue
            name = _font_name(path)
            if not name:
                continue
            family, style = name
            key = (family.casefold(), style.casefold())
            existing = entries.get(key)
            # Prefer system fonts, then CapCut-bundled ones, over cache copies.
            if existing is None or (source == "system" and existing.source != "system"):
                entries[key] = Font(family=family, style=style, path=str(path), source=source)
    return tuple(sorted(entries.values(), key=lambda f: (f.family.casefold(), f.style.casefold())))


def search(query: str = "", limit: int = 50) -> list[Font]:
    text = query.strip().casefold()
    items = [
        font
        for font in catalogue()
        if not text or text in font.family.casefold() or text in font.style.casefold()
    ]
    return items[:limit]


def default_font() -> Font | None:
    """CapCut's own default UI font for the installed version."""
    for version in paths.installed_versions():
        candidate = version / "Resources" / "Font" / "SystemFont" / "en.ttf"
        if candidate.is_file():
            name = _font_name(candidate) or ("CapCut Default", "Regular")
            return Font(family=name[0], style=name[1], path=str(candidate), source="capcut")
    return None


def resolve(family: str = "", style: str = "") -> Font | None:
    """Best match for a family (and optional style) among the installed fonts."""
    want_family = family.strip().casefold()
    want_style = style.strip().casefold()

    if not want_family:
        return default_font()

    candidates = [f for f in catalogue() if f.family.casefold() == want_family]
    if not candidates:
        # Allow partial family names ("arial" -> "Arial", "comic" -> "Comic Sans MS").
        candidates = [f for f in catalogue() if want_family in f.family.casefold()]
    if not candidates:
        return None

    if want_style:
        exact = [f for f in candidates if f.style.casefold() == want_style]
        if exact:
            return exact[0]
        partial = [f for f in candidates if want_style in f.style.casefold()]
        if partial:
            return partial[0]

    preferred = ("regular", "normal", "book", "roman")
    for name in preferred:
        for font in candidates:
            if font.style.casefold() == name:
                return font
    return sorted(candidates, key=lambda f: len(f.style))[0]
