"""Bookkeeping for live editing.

CapCut keeps an open project in memory and has no reload hook, so the server cannot
watch the app. It can, however, remember the file hash it last wrote. Comparing that
with the file on disk answers the only question that matters while both sides edit the
same project: did CapCut save something after our last change?

The state file lives outside the project folders, so drafts stay byte-identical to what
CapCut itself would write.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def state_path() -> Path:
    override = os.environ.get("CAPCUT_MCP_STATE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".capcut-mcp"
    return root / "CapCut-MCP" / "writes.json"


def _key(folder: Path) -> str:
    return str(Path(folder).resolve()).casefold()


def _load() -> dict:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def record_write(folder: Path | str, digest: str) -> None:
    """Remember that this server wrote ``digest`` to ``folder``."""
    data = _load()
    data[_key(Path(folder))] = {"hash": digest, "at": time.time()}
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # Bookkeeping is a convenience; never fail an edit because it cannot be saved.
        pass


def last_write(folder: Path | str) -> dict | None:
    entry = _load().get(_key(Path(folder)))
    return entry if isinstance(entry, dict) else None
