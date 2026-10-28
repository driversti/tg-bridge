"""End-to-end over real stdio: stdout must carry only MCP, startup must not touch Telegram."""

import os
import subprocess
import sys
from pathlib import Path

from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO = Path(__file__).resolve().parent.parent
# The installed console script, i.e. what `uv run tg-bridge` (and LM Studio) launches.
SERVER = [str(Path(sys.executable).parent / "tg-bridge")]


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_API_", "TG_BRIDGE_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def test_missing_credentials_exits_cleanly(tmp_path):
    proc = subprocess.run(SERVER, env=clean_env(tmp_path), cwd=REPO, capture_output=True, text=True, timeout=30)

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "TG_API_ID" in proc.stderr
    assert "Traceback" not in proc.stderr


async def test_stdio_lists_tools_without_telegram(tmp_path):
    session = tmp_path / "none.session"
    env = clean_env(tmp_path, TG_API_ID="1", TG_API_HASH="x", TG_BRIDGE_SESSION=str(session))
    params = StdioServerParameters(command=SERVER[0], args=SERVER[1:], env=env, cwd=str(REPO))

    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = await client.list_tools()
        result = await client.call_tool("whoami", {})

    assert sorted(t.name for t in tools.tools) == ["download_media", "export_chat", "get_chat_info", "get_messages", "list_chats", "now", "search_messages", "whoami"]
    assert result.is_error is True
    assert "tg_bridge.login" in result.content[0].text
    assert not session.exists()
