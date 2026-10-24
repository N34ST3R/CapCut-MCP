"""Filesystem locations for the installed CapCut app and its draft projects.

CapCut has no public automation API. Its drafts are ordinary folders of JSON files
under the user's LocalAppData directory, which is what this package reads and writes.

Override the drafts directory with CAPCUT_DRAFTS_DIR if the projects folder was
moved in CapCut's settings.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DRAFT_JSON = "draft_content.json"
META_JSON = "draft_meta_info.json"


class CapCutNotFound(RuntimeError):
    """The CapCut install or its drafts folder could not be located."""


def local_appdata() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    return Path(value) if value else Path.home() / "AppData" / "Local"


def drafts_root() -> Path:
    override = os.environ.get("CAPCUT_DRAFTS_DIR")
    if override:
        return Path(override).expanduser()
    return local_appdata() / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"


def apps_dir() -> Path:
    return local_appdata() / "CapCut" / "Apps"


def installed_versions() -> list[Path]:
    """Versioned app folders, newest first. Each one contains CapCut.exe."""
    root = apps_dir()
    found: list[tuple[list[int], Path]] = []
    if root.is_dir():
        for child in root.iterdir():
            if not child.is_dir() or not re.fullmatch(r"\d+(?:\.\d+)*", child.name):
                continue
            if (child / "CapCut.exe").is_file():
                found.append(([int(p) for p in child.name.split(".")], child))
    found.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in found]


def capcut_exe() -> Path | None:
    for version_dir in installed_versions():
        return version_dir / "CapCut.exe"
    launcher = apps_dir() / "CapCut.exe"
    return launcher if launcher.is_file() else None


def app_version() -> str:
    """Version string as CapCut records it in drafts (e.g. 8.5.0 from 8.5.0.3590)."""
    versions = installed_versions()
    if not versions:
        return ""
    return ".".join(versions[0].name.split(".")[:3])


def project_dirs(root: Path | None = None) -> list[Path]:
    base = Path(root) if root else drafts_root()
    if not base.is_dir():
        return []
    return sorted(
        (
            p
            for p in base.iterdir()
            if p.is_dir()
            and (p / DRAFT_JSON).is_file()
            and not p.name.startswith(".")
            and p.name not in {".recycle_bin"}
        ),
        key=lambda p: (p / DRAFT_JSON).stat().st_mtime,
        reverse=True,
    )


def resolve_project(name_or_path: str, root: Path | None = None) -> Path:
    """Accept a project name, folder name, or absolute folder path."""
    raw = Path(name_or_path).expanduser()
    if raw.is_dir() and (raw / DRAFT_JSON).is_file():
        return raw

    base = Path(root) if root else drafts_root()
    direct = base / name_or_path
    if direct.is_dir() and (direct / DRAFT_JSON).is_file():
        return direct

    wanted = name_or_path.strip().lower()
    for folder in project_dirs(base):
        if folder.name.lower() == wanted:
            return folder
    for folder in project_dirs(base):
        try:
            import json

            meta = json.loads((folder / META_JSON).read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(meta.get("draft_name", "")).strip().lower() == wanted:
            return folder

    raise CapCutNotFound(f"No CapCut project named {name_or_path!r} in {base}")


def safe_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return cleaned[:120] or "Untitled"


def unique_folder(base: Path, name: str) -> Path:
    candidate = base / name
    index = 2
    while candidate.exists():
        candidate = base / f"{name} ({index})"
        index += 1
    return candidate
