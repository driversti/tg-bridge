"""MCP tool bodies: thin, plain-text, Telethon-free."""

from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.mcpserver.exceptions import ToolError

from tg_bridge.config import Settings
from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError, TgBridgeError
from tg_bridge.format import (
    ambiguous_text,
    chat_info_block,
    chat_line,
    not_found_text,
    render_lines,
)
from tg_bridge.resolve import match_chats, resolve_chat
from tg_bridge.source import Chat, MessageSource

KINDS = ("user", "bot", "group", "supergroup", "channel")
MIN_LIMIT, MAX_LIMIT = 1, 200
# A "not found" retries with fresh dialogs only when the cache is older than this (seconds).
REFRESH_AFTER = 30.0


@contextmanager
def _tool_errors() -> Iterator[None]:
    try:
        yield
    except TgBridgeError as e:
        # Anticipated failures reach the model as readable text; anything else stays a generic crash.
        raise ToolError(str(e)) from e


async def whoami(source: MessageSource) -> str:
    with _tool_errors():
        me = await source.whoami()
    handle = f"@{me.username}" if me.username else "no username"
    return f"{me.display_name} ({handle}), id {me.id}"


async def resolve_chat_arg(source: MessageSource, arg: str, settings: Settings) -> Chat:
    """Shared by every chat-taking tool: one dialog, or a ToolError the model can act on."""
    with _tool_errors():
        try:
            return resolve_chat(arg, await source.list_dialogs())
        except ChatNotFoundError as first:
            age_of = getattr(source, "dialogs_age", None)
            if age_of is None:
                raise _not_found(first, settings) from first
            age = age_of()
            if age is not None and age <= REFRESH_AFTER:
                raise _not_found(first, settings) from first
            try:  # a chat created a minute ago is not in the cache yet
                return resolve_chat(arg, await source.list_dialogs(refresh=True))
            except ChatNotFoundError as e:
                raise _not_found(e, settings) from e
        except AmbiguousChatError as e:
            raise ToolError(ambiguous_text(e, settings.timezone, settings.max_chars)) from e


def _not_found(e: ChatNotFoundError, settings: Settings) -> ToolError:
    return ToolError(not_found_text(e, settings.timezone, settings.max_chars))


async def list_chats(
    source: MessageSource,
    settings: Settings,
    query: str | None = None,
    kind: str | None = None,
    limit: int = 30,
) -> str:
    kinds = _kinds(kind)
    limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))
    query = (query or "").strip() or None
    with _tool_errors():
        chats = await source.list_dialogs()
    if kinds is not None:
        chats = [c for c in chats if c.kind in kinds]
    if query is not None:
        chats = match_chats(query, chats)
    if not chats:
        return f'No chats match "{query}".' if query else "No chats found."
    # Archived dialogs come after the non-archived ones; sorted() is stable so Telegram's order is kept inside each.
    shown = sorted(chats, key=lambda c: c.is_archived)[:limit]
    label = f'Chats matching "{query}"' if query else "Chats"
    header = f"{label} ({len(shown)} shown of {len(chats)})\n{_totals(chats)}"
    return render_lines(header, [chat_line(c, settings.timezone) for c in shown], settings.max_chars)


def _totals(chats: list[Chat]) -> str:
    """Counts over every match, so "how many …" questions don't need the model to read the whole list."""
    parts = [f"{n} {k}" for k in KINDS if (n := sum(c.kind == k for c in chats))]
    archived = sum(c.is_archived for c in chats)
    if archived:
        parts.append(f"{archived} archived")
    # Public = a group/channel with its own @username. Both numbers are printed because small models
    # get "total - public" wrong. People don't count either way.
    rooms = [c for c in chats if c.kind in ("group", "supergroup", "channel")]
    public = sum(bool(c.username) for c in rooms)
    if public:
        parts.append(f"{public} public")
    if len(rooms) - public:
        parts.append(f"{len(rooms) - public} private")
    return "Totals: " + " · ".join(parts)


def _kinds(kind: str | None) -> tuple[str, ...] | None:
    if kind is None or not kind.strip():
        return None
    wanted = kind.strip().casefold()
    if wanted == "group":  # users say "groups" for both basic groups and supergroups
        return ("group", "supergroup")
    if wanted in KINDS:
        return (wanted,)
    raise ToolError(f'Unknown kind "{kind}". Allowed: {", ".join(KINDS)}.')


async def get_chat_info(source: MessageSource, settings: Settings, chat: str) -> str:
    resolved = await resolve_chat_arg(source, chat, settings)
    with _tool_errors():
        info = await source.get_chat_info(resolved)
    return chat_info_block(info, settings.timezone)
