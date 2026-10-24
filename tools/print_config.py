"""Print a ready-to-paste MCP client config with absolute paths for this machine."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1] / "server.py"
DRAFTS = Path(os.environ["LOCALAPPDATA"]) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft"


def main() -> int:
    config = {
        "mcpServers": {
            "capcut": {
                "command": sys.executable,
                "args": [str(SERVER)],
                "env": {"CAPCUT_DRAFTS_DIR": str(DRAFTS)},
            }
        }
    }
    print(json.dumps(config, indent=2))
    print(
        "\nMerge the 'capcut' entry into your client's MCP config "
        "(Claude Desktop: %APPDATA%\\Claude\\claude_desktop_config.json), then restart the client.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
