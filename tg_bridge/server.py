"""MCP bootstrap: stdio transport, launched by LM Studio."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.errors import ConfigError
from tg_bridge.logging_setup import configure_logging
from tg_bridge.source import MessageSource
from tg_bridge.source.live import LiveSource
from tg_bridge.telegram.client import ReadOnlyTelegram

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def build_server(source: MessageSource) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await source.close()

    server = MCPServer("tg-bridge", lifespan=lifespan)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def whoami() -> str:
        """Return the Telegram account this bridge is logged in as: name, @username and numeric id."""
        return await tools.whoami(source)

    return server


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge: {e}", file=sys.stderr)
        sys.exit(2)
    log.info("starting tg-bridge (stdio)")
    build_server(LiveSource(ReadOnlyTelegram(settings))).run()
