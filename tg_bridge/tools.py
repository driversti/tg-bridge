"""MCP tool bodies: thin, plain-text, Telethon-free."""

from mcp.server.mcpserver.exceptions import ToolError

from tg_bridge.errors import TgBridgeError
from tg_bridge.source import MessageSource


async def whoami(source: MessageSource) -> str:
    try:
        me = await source.whoami()
    except TgBridgeError as e:
        # Anticipated failures reach the model as readable text; anything else stays a generic crash.
        raise ToolError(str(e)) from e
    handle = f"@{me.username}" if me.username else "no username"
    return f"{me.display_name} ({handle}), id {me.id}"
