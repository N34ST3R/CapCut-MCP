"""Local MCP server for CapCut desktop drafts.

CapCut exposes no public API, so this package edits the project files the app reads
and uses Win32 calls to launch or focus the editor.
"""

__version__ = "0.2.0"

__all__ = ["draft", "anim", "fonts", "media", "paths", "state", "subtitles", "app"]
