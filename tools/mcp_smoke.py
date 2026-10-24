"""End-to-end smoke test: drive the MCP server through its own client.

Runs against a throwaway drafts folder, so it never touches real CapCut projects:

    python tools/mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

tmp = Path(tempfile.mkdtemp(prefix="capcut-mcp-smoke-"))
os.environ["CAPCUT_DRAFTS_DIR"] = str(tmp / "drafts")
os.environ["CAPCUT_MCP_STATE"] = str(tmp / "writes.json")
os.environ["LOCALAPPDATA"] = os.environ.get("LOCALAPPDATA", str(tmp))
(tmp / "drafts").mkdir(parents=True)

from fastmcp import Client  # noqa: E402

import server  # noqa: E402


def payload(result):
    if getattr(result, "data", None) is not None:
        return result.data
    return result.content


async def main() -> int:
    failures = []
    async with Client(server.mcp) as client:
        tools = sorted(t.name for t in await client.list_tools())
        print(f"tools ({len(tools)}): {', '.join(tools)}")
        expected = {
            "capcut_status",
            "list_projects",
            "inspect_project",
            "create_project",
            "sync_status",
            "reload_project",
            "add_video",
            "add_audio",
            "add_text",
            "style_text",
            "list_fonts",
            "animate_segment",
            "list_transitions",
            "add_transition",
            "list_filters",
            "apply_filter",
            "apply_mask",
            "set_speed",
            "trim_segment",
            "split_segment",
            "duplicate_segment",
            "ripple_delete_segment",
            "delete_segment",
            "import_srt",
            "set_canvas",
            "rename_project",
            "open_project_folder",
            "focus_capcut",
        }
        missing = expected - set(tools)
        if missing:
            failures.append(f"missing tools: {sorted(missing)}")

        created = payload(await client.call_tool("create_project", {"name": "Smoke Test"}))
        print("create_project ->", json.dumps(created, indent=1))
        if not created.get("ok"):
            failures.append("create_project did not report ok")

        folder = Path(created["folder"])
        if not (folder / "draft_content.json").is_file():
            failures.append("draft_content.json missing")

        added = payload(
            await client.call_tool(
                "add_text",
                {"project": "Smoke Test", "text": "Hello CapCut", "start": 0.5, "duration": 2.0, "color": "#FFEE00"},
            )
        )
        print("add_text ->", json.dumps(added["added"], indent=1))

        summary = payload(await client.call_tool("inspect_project", {"project": "Smoke Test"}))
        print("inspect_project ->", json.dumps(summary, indent=1))
        if summary["duration_s"] != 2.5:
            failures.append(f"expected 2.5s duration, got {summary['duration_s']}")

        segment_id = added["added"]["segment_id"]
        styled = payload(
            await client.call_tool(
                "style_text",
                {"project": "Smoke Test", "segment_id": segment_id, "size": 22, "bold": True, "color": "#00E5FF"},
            )
        )
        print("style_text ->", json.dumps(styled["styled"], indent=1))
        if styled["styled"]["size"] != 22.0:
            failures.append("style_text did not apply the new size")

        animated = payload(
            await client.call_tool(
                "animate_segment", {"project": "Smoke Test", "segment_id": segment_id, "preset": "fade_in"}
            )
        )
        print("animate_segment ->", json.dumps(animated["animated"], indent=1))
        if animated["animated"]["properties"] != ["alpha"]:
            failures.append("fade_in did not animate alpha")

        painted = payload(await client.call_tool("list_fonts", {"query": "arial", "limit": 3}))
        print("list_fonts ->", painted["count"], "matching fonts")

        synced = payload(await client.call_tool("sync_status", {"project": "Smoke Test"}))
        print("sync_status ->", json.dumps(synced, indent=1))
        if synced["changed_since_last_edit"]:
            failures.append("sync_status reported a foreign save right after our own edit")

        listed = payload(await client.call_tool("list_projects", {}))
        if listed["count"] != 1:
            failures.append(f"expected 1 project, got {listed['count']}")

        # Errors must surface to the client instead of looking like success.
        try:
            await client.call_tool("inspect_project", {"project": "Does Not Exist"})
            failures.append("missing project did not raise")
        except Exception as exc:  # expected
            print("error path ->", type(exc).__name__, str(exc)[:100])

    if failures:
        print("\nFAILED:")
        for item in failures:
            print(" -", item)
        return 1
    print("\nOK: MCP server responded correctly over the client transport.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
