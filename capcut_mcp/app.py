"""Window-level helpers for the running CapCut desktop app.

CapCut exposes no local API and no documented deep link that opens a specific draft,
so automation stops at: detect the app, bring it to the front, launch it if needed,
and reveal the project folder in Explorer. Anything inside the editor is done by
editing draft files while CapCut is closed (or reopened afterwards).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from . import paths

IS_WINDOWS = sys.platform.startswith("win")
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SW_RESTORE = 9
SW_SHOW = 5


def _process_exe(pid: int) -> str:
    """Full path of the executable behind a pid, or "" if not accessible."""
    if not IS_WINDOWS or pid <= 0:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _windows_with_titles() -> list[tuple[int, str]]:
    if not IS_WINDOWS:
        return []
    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            found.append((int(hwnd), buffer.value))
        return True

    user32.EnumWindows(callback, 0)
    return found


def windows() -> list[dict]:
    """Visible top-level windows owned by CapCut.exe."""
    result = []
    for hwnd, title in _windows_with_titles():
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_exe(int(pid.value))
        if Path(exe).name.lower() == "capcut.exe":
            result.append({"hwnd": hwnd, "title": title, "pid": int(pid.value), "exe": exe})
    return result


def is_running() -> bool:
    return bool(windows())


def focus() -> bool:
    """Bring an existing CapCut window to the foreground."""
    wins = windows()
    if not wins:
        return False
    user32 = ctypes.windll.user32
    hwnd = wins[0]["hwnd"]
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.ShowWindow(hwnd, SW_SHOW)
    return bool(user32.SetForegroundWindow(hwnd))


def launch() -> dict:
    """Start CapCut if it is not already running."""
    if is_running():
        return {"launched": False, "reason": "CapCut is already running", "focused": focus()}
    exe = paths.capcut_exe()
    if not exe:
        raise RuntimeError("CapCut.exe not found; is CapCut installed for this user?")
    subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    return {"launched": True, "exe": str(exe)}


def reveal(folder: Path | str) -> bool:
    """Open a folder in Explorer (the draft the user should open in CapCut)."""
    path = Path(folder)
    if not path.is_dir():
        return False
    os.startfile(str(path))  # noqa: S606 - intended: open Explorer on a known folder
    return True


def status() -> dict:
    exe = paths.capcut_exe()
    root = paths.drafts_root()
    return {
        "running": is_running(),
        "window_titles": [w["title"] for w in windows()],
        "executable": str(exe) if exe else None,
        "app_version": paths.app_version() or None,
        "drafts_root": str(root),
        "drafts_root_exists": root.is_dir(),
        "project_count": len(paths.project_dirs()),
        "platform": "windows" if IS_WINDOWS else sys.platform,
    }
