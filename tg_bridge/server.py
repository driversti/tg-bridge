"""MCP bootstrap: stdio transport, launched by LM Studio."""

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from tg_bridge import tools
from tg_bridge.config import Settings, load_settings
from tg_bridge.errors import ConfigError
from tg_bridge.logging_setup import configure_logging
from tg_bridge.source import MessageSource
from tg_bridge.source.dialog_cache import DialogCache
from tg_bridge.source.live import LiveSource
from tg_bridge.telegram.client import ReadOnlyTelegram

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)


def build_server(source: MessageSource, settings: Settings) -> MCPServer:
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

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_chats(query: str | None = None, kind: str | None = None, limit: int = 30) -> str:
        """List the user's Telegram chats (private, groups, channels, bots, incl. archived), newest first.
        `query`: words from the chat title or @username. `kind`: user, bot, group (incl. supergroups),
        supergroup, channel. The second line has totals per kind for all matches, so for "how many" questions
        use limit=1 and read the numbers as given ("public"/"private" = groups/channels with/without
        an @username).
        Then one line per chat starting with its numeric id; pass that id to other tools."""
        return await tools.list_chats(source, settings, query=query, kind=kind, limit=limit)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_chat_info(chat: str) -> str:
        """Details of one chat: type, description, member count, the other person's identity for private chats,
        forum topics. `chat`: numeric id, @username, or words from the title."""
        return await tools.get_chat_info(source, settings, chat)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_messages(
        chat: str,
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        topic: str | int | None = None,
        from_me: bool = False,
        cursor: str | None = None,
        limit: int = 100,
    ) -> str:
        """Read one chat's messages in time order, as compact lines grouped by day.
        `chat`: numeric id, @username, or words from the title.
        Period: `days` (1 = today, 2 = since yesterday 00:00, …) OR `since`/`until` (YYYY-MM-DD, "YYYY-MM-DD HH:MM",
        "today", "yesterday"; `until` dates include that whole day). No period = the latest messages.
        `topic`: forum topic id or title words (see get_chat_info). `from_me`: only messages the user sent.
        If the result ends with "Next: call get_messages(...)", more messages exist: make exactly that call, copying
        chat and cursor character for character (other arguments come from the cursor). To read a whole period,
        keep calling until the result ends with "End of period."."""
        return await tools.get_messages(
            source, settings, chat, days=days, since=since, until=until, topic=topic, from_me=from_me,
            cursor=cursor, limit=limit,
        )

    return server


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge: {e}", file=sys.stderr)
        sys.exit(2)
    log.info("starting tg-bridge (stdio)")
    dialog_cache = DialogCache(settings.session_path.with_name(f"{settings.session_path.stem}.dialogs.json"))
    build_server(LiveSource(ReadOnlyTelegram(settings), dialog_cache=dialog_cache), settings).run()
