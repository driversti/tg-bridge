"""MCP bootstrap: stdio transport, launched by LM Studio."""

import logging
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from tg_bridge import tools
from tg_bridge.config import Settings, load_settings
from tg_bridge.errors import ConfigError
from tg_bridge.logging_setup import configure_logging
from tg_bridge.source import MessageSource
from tg_bridge.source.allowlist import Allowlist, allowlist_path
from tg_bridge.source.dialog_cache import DialogCache
from tg_bridge.source.live import LiveSource
from tg_bridge.source.scoped import ScopedSource
from tg_bridge.telegram.client import ReadOnlyTelegram
from tg_bridge.timerange import WEEKDAYS, iso_week, now_line

log = logging.getLogger(__name__)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
# Still read-only towards Telegram, but it creates a local file, so clients may ask before running it.
WRITES_LOCAL_FILE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)


def _instructions(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return (
        "Read-only access to the user's Telegram account. "
        f"Today is {WEEKDAYS[local.weekday()]} {local:%Y-%m-%d}, {local:%H:%M} {tz.key} "
        f"(ISO week {iso_week(local.date())}). "
        "Your own sense of the date is out of date: for anything relative to today "
        '("this week", "last month", "next year", ages, deadlines) use this date or call now(). '
        "For periods prefer the `period` keywords or `days` over computing dates yourself; "
        '"last 7 days" or "past week" is days=7, while previous_week is the previous calendar week.'
    )


def build_server(
    source: MessageSource,
    settings: Settings,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> MCPServer:
    tz = settings.timezone
    # The "Now:" line and its newline count towards the output budget, so the tools get what is left.
    budget = replace(settings, max_chars=settings.max_chars - len(now_line(now(), tz)) - 1)

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await source.close()

    server = MCPServer("tg-bridge", instructions=_instructions(now(), tz), lifespan=lifespan)

    async def stamped(call: Callable[[datetime], Awaitable[str]]) -> str:
        # One clock reading per call: the line and the period maths must agree. Errors get the date too.
        moment = now()
        line = now_line(moment, tz)
        try:
            return f"{line}\n{await call(moment)}"
        except ToolError as e:
            raise ToolError(f"{line}\n{e}") from e

    # Registered as "now": the function name would shadow the clock parameter.
    @server.tool(name="now", annotations=READ_ONLY, structured_output=False)
    async def now_tool() -> str:
        """Current date, weekday, time, time zone and ISO week, plus this/last week, month and year as dates.
        Call it first when the question depends on today ("today", "this week", "last month", "next year",
        how long ago): never guess the date. Every other tool's result also starts with a "Now:" line."""

        async def body(moment: datetime) -> str:
            return tools.now(budget, moment)

        return await stamped(body)

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def whoami() -> str:
        """Return the Telegram account this bridge is logged in as: name, @username and numeric id."""
        return await stamped(lambda _: tools.whoami(source))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_chats(
        query: str | None = None,
        kind: str | None = None,
        limit: int = 30,
        folder: str | int | None = None,
        min_folders: int | None = None,
    ) -> str:
        """List the user's Telegram chats (private, groups, channels, bots, incl. archived), newest first.
        `query`: words from the chat title or @username. `kind`: user, bot, group (incl. supergroups),
        supergroup, channel. The second line has totals per kind for all matches, so for "how many" questions
        use limit=1 and read the numbers as given ("public"/"private" = groups/channels with/without
        an @username). `folder`: folder id or title words (see list_folders): only that folder's chats, in the
        folder's order. `min_folders`: only chats that sit in at least this many folders, each line ending with
        its folders (e.g. min_folders=2 for "chats in two or more folders"; don't check chats one by one).
        Then one line per chat starting with its numeric id; pass that id to other tools."""
        return await stamped(
            lambda _: tools.list_chats(
                source, budget, query=query, kind=kind, limit=limit, folder=folder, min_folders=min_folders
            )
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def list_folders() -> str:
        """List the user's chat folders in app order: id, title, chat count, unread chats, and what the folder
        contains (rules and/or hand-picked chats). Use the id or title with list_chats(folder=…)."""
        return await stamped(lambda _: tools.list_folders(source, budget))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_chat_info(chat: str) -> str:
        """Details of one chat: type, description, member count, the other person's identity for private chats,
        forum topics, the folders the chat is in. `chat`: numeric id, @username, or words from the title."""
        return await stamped(lambda _: tools.get_chat_info(source, budget, chat))

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_messages(
        chat: str | None = None,
        folder: str | int | None = None,
        mode: str = "full",
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        period: str | None = None,
        topic: str | int | None = None,
        from_me: bool = False,
        cursor: str | None = None,
        around: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Read one chat (`chat`: numeric id, @username, or words from the title) or a whole chat folder (`folder`: id or
        name from list_folders) in time order, as compact lines grouped by day.
        `mode="headlines"`: one short line per post, the same news from several chats merged into one "×N" line; best
        for "what's new in <folder>" questions. Needs a period. Folder reads always need a period.
        Period: `period` = today, yesterday, this_week, previous_week, this_month, previous_month, this_year or
        previous_year; OR `days` (1 = today, 2 = since yesterday 00:00, …); OR `since`/`until` (YYYY-MM-DD,
        "YYYY-MM-DD HH:MM" or a keyword; `until` dates include that whole day). Prefer `period` or `days` over computing dates yourself.
        "The last/past N days/week/month", counted back from now, is `days`: "last 7 days" or "past week" → days=7,
        "the last month" or "past 30 days" → days=30. previous_week/previous_month are only the previous calendar
        week/month (Mon–Sun, 1st–end): use them only when the user means that.
        No period = the latest messages.
        `topic`: forum topic id or title words (see get_chat_info). `from_me`: only messages the user sent.
        If the result ends with "Next: call get_messages(...)", more messages exist: make exactly that call, copying
        chat or folder and cursor character for character (other arguments, including mode, come from the cursor).
        To read a whole period, keep calling until the result ends with "End of period."
        `around`: a message #id (e.g. from search_messages): shows the messages just before and after it;
        period, topic and from_me are then ignored.
        Comments under a channel post: use get_comments."""
        return await stamped(
            lambda m: tools.get_messages(
                source, budget, chat, folder=folder, mode=mode, days=days, since=since, until=until, period=period,
                topic=topic, from_me=from_me, cursor=cursor, around=around, limit=limit,
                now=lambda: m,
            )
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def get_comments(
        chat: str,
        post: int | None = None,
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        period: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Read the comments under a channel post (what people replied in the post's discussion), oldest first;
        works without joining the discussion group. `chat`: the channel (id, @username or title words).
        `post`: the post's #id (from get_messages or search_messages; a "💬37" on a post line means 37 comments).
        Each line starts with the comment's own #id and has the author's link and the comment's link; "↩#5008" at
        the end only adds that this comment answers comment #5008.
        Period (`period`, `days` or `since`/`until`, as in get_messages) filters by comment date; none = whole thread.
        If the result ends with "Next: call get_comments(...)", make exactly that call (copy chat and cursor
        character for character) and keep calling until the result ends with "End of comments".
        For a series of posts (e.g. weekly check-ins): first search_messages(query=<title words or #hashtag>,
        chat=<channel>, since=…) to list the posts, then get_comments(chat=<channel>, post=<id>) for each post."""
        return await stamped(
            lambda m: tools.get_comments(
                source, budget, chat, post, days=days, since=since, until=until, period=period, cursor=cursor,
                limit=limit, now=lambda: m,
            )
        )

    @server.tool(annotations=READ_ONLY, structured_output=False)
    async def search_messages(
        query: str,
        chat: str | None = None,
        folder: str | int | None = None,
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        period: str | None = None,
        from_me: bool = False,
        cursor: str | None = None,
        limit: int = 30,
    ) -> str:
        """Find messages containing words, in one chat (`chat`: id, @username or title words) or, without `chat`,
        in all the user's chats. Newest hits first, each with its date and #id.
        `folder`: search every chat of a folder (id or name from list_folders) instead of one chat; not with `chat`.
        Telegram matches whole words and word starts, NOT meaning: if nothing useful comes back, search again with
        other word forms, synonyms, and the other languages people use in that chat (e.g. Ukrainian, Russian,
        Polish, English). One query per call; prefer 1–2 distinctive words.
        Period: `period` (this_week, previous_month, … as in get_messages) OR `days` OR `since`/`until`.
        "The last/past N days/week/month", counted back from now, is `days`: "last 7 days" or "past week" → days=7,
        "the last month" or "past 30 days" → days=30. previous_week/previous_month are only the previous calendar
        week/month (Mon–Sun, 1st–end): use them only when the user means that.
        `from_me`: only messages the user sent.
        To see the conversation around a hit: get_messages(chat=<chat id>, around=<#id>).
        If the result ends with "Next: call search_messages(...)", copy that call exactly to get older hits."""
        return await stamped(
            lambda m: tools.search_messages(
                source, budget, query, chat, folder=folder, days=days, since=since, until=until, period=period,
                from_me=from_me, cursor=cursor, limit=limit, now=lambda: m,
            )
        )

    @server.tool(annotations=WRITES_LOCAL_FILE, structured_output=False)
    async def export_chat(
        chat: str,
        days: int | None = None,
        since: str | None = None,
        until: str | None = None,
        period: str | None = None,
        format: str = "md",
        resume: bool = False,
    ) -> str:
        """Save a chat (or a period of it) to a Markdown file and return only the file path, message count and date
        range, NOT the messages. Use it when the user wants a file, a full copy, or a whole long conversation for
        another tool or model. Without a period it exports the whole history. `chat`: id, @username or title words.
        Period: `period` (this_week, previous_month, … as in get_messages) OR `days` OR `since`/`until`.
        "The last/past N days/week/month", counted back from now, is `days`: "last 7 days" or "past week" → days=7,
        "the last month" or "past 30 days" → days=30. previous_week/previous_month are only the previous calendar
        week/month (Mon–Sun, 1st–end): use them only when the user means that.
        One call works for about 40 seconds. If the result says "Next: call export_chat(chat=..., resume=true)", make
        exactly that call, and repeat until the result no longer says so: every call adds to the same file."""
        return await stamped(
            lambda m: tools.export_chat(
                source, budget, chat, days=days, since=since, until=until, period=period, format=format,
                resume=resume, now=lambda: m,
            )
        )

    @server.tool(annotations=WRITES_LOCAL_FILE, structured_output=False)
    async def download_media(chat: str, message_id: int) -> str:
        """Save one message's photo, video, voice, document… to a local file and return only its path, size and
        type, NOT its content. Only when the user asks for the file. `chat`: id, @username or title words.
        `message_id`: the #id from get_messages or search_messages. A message in an album downloads the whole album.
        One call works for about 40 seconds: if the result says "Next: call download_media(...)", make exactly that
        call until it no longer says so."""
        return await stamped(lambda _: tools.download_media(source, budget, chat, message_id))

    return server


def build_source(settings: Settings) -> MessageSource:
    """The live source, wrapped in the scope filter unless the owner opted into `all`."""
    dialog_cache = DialogCache(settings.session_path.with_name(f"{settings.session_path.stem}.dialogs.json"))
    live = LiveSource(ReadOnlyTelegram(settings), dialog_cache=dialog_cache)
    if settings.scope == "all":
        return live
    return ScopedSource(live, Allowlist(allowlist_path(settings.session_path)))


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge: {e}", file=sys.stderr)
        sys.exit(2)
    log.info("starting tg-bridge (stdio, scope=%s)", settings.scope)
    build_server(build_source(settings), settings).run()
