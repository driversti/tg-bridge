"""MCP tool bodies: thin, plain-text, Telethon-free."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mcp.server.mcpserver.exceptions import ToolError

from tg_bridge.config import Settings
from tg_bridge.cursor import (
    Cursor,
    SearchCursor,
    decode_cursor,
    decode_search_cursor,
    encode_cursor,
    encode_search_cursor,
)
from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError, CursorError, TgBridgeError
from tg_bridge.export import ExportResult, export_chat_file, period_text, when_text
from tg_bridge.format import (
    MAX_CANDIDATES,
    ambiguous_text,
    chat_info_block,
    chat_line,
    messages_head,
    not_found_text,
    one_line,
    quote_query,
    render_around,
    render_hits,
    render_lines,
    render_page,
    search_head,
)
from tg_bridge.resolve import match_chats, normalize, resolve_chat
from tg_bridge.source import Chat, Message, MessageQuery, MessageSource, SearchOffset, SearchQuery, Topic
from tg_bridge.timerange import Period, describe_period, parse_period

KINDS = ("user", "bot", "group", "supergroup", "channel")
MIN_LIMIT, MAX_LIMIT = 1, 200
MIN_MESSAGES, MAX_MESSAGES = 1, 300
MIN_HITS, MAX_HITS = 1, 100
DEFAULT_MESSAGES, AROUND_LIMIT = 100, 20
MAX_QUERY_CHARS = 256
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


async def get_messages(
    source: MessageSource,
    settings: Settings,
    chat: str,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    topic: str | int | None = None,
    from_me: bool = False,
    cursor: str | None = None,
    around: int | None = None,
    limit: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    resolved = await resolve_chat_arg(source, chat, settings)
    has_cursor = cursor is not None and bool(cursor.strip())
    around_mode = around is not None and not has_cursor  # a cursor continues a page; it wins over `around`
    if limit is None:
        limit = AROUND_LIMIT if around_mode else DEFAULT_MESSAGES
    limit = max(MIN_MESSAGES, min(MAX_MESSAGES, limit))
    tz = settings.timezone
    if around_mode:
        return await _messages_around(source, settings, resolved, around, limit)
    with _tool_errors():
        if cursor is not None and cursor.strip():
            c = decode_cursor(cursor, resolved.id)
            direction, period, boundary_id, from_me = c.direction, Period(c.start, c.end), c.boundary_id, c.from_me
            topic_obj = None if c.topic_id is None else Topic(c.topic_id, "", False)
        else:
            period = parse_period(days, since, until, tz, now())
            topic_obj = await _resolve_topic(source, resolved, topic)
            # Period reads start at the beginning and move forward; "latest" reads move back from the end.
            direction = "forward" if period.start is not None else "backward"
            boundary_id = None
        query = MessageQuery(
            direction=direction,
            start=period.start,
            end=period.end,
            boundary_id=boundary_id,
            topic_id=topic_obj.id if topic_obj else None,
            from_me=from_me,
            limit=limit + 1,  # the extra one only tells us whether more exist
        )
        fetched = await source.get_messages(resolved, query)
    more = len(fetched) > limit
    page = fetched[:limit]
    forward = direction == "forward"
    chronological = page if forward else page[::-1]

    def next_hint(boundary: Message) -> str:
        token = encode_cursor(
            Cursor(resolved.id, direction, boundary.id, period.start, period.end, query.topic_id, from_me)
        )
        # Spelled out: models stopped after page 1 when the footer only said "More:".
        remain = "More messages remain; this is not the end of the period." if forward else "Older messages remain."
        return f'{remain} Next: call get_messages(chat="{resolved.id}", cursor="{token}")'

    return render_page(
        messages_head(resolved, describe_period(period, tz), tz, topic_obj, from_me),
        chronological,
        tz=tz,
        max_chars=settings.max_chars,
        keep="oldest" if forward else "newest",
        more=more,
        next_hint=next_hint,
        end_text="End of period." if forward else "No older messages.",
        empty_text="No messages in this period." if forward else "No messages.",
    )


async def _messages_around(source: MessageSource, settings: Settings, chat: Chat, target: int, limit: int) -> str:
    tz = settings.timezone
    before = (limit - 1) // 2
    after = limit - 1 - before

    def query(direction: str, boundary: int, count: int) -> MessageQuery:
        return MessageQuery(direction, None, None, boundary, None, False, count)  # type: ignore[arg-type]

    with _tool_errors():
        # Backward: the target (when it exists) + `before` + one more to learn whether older messages remain.
        back = await source.get_messages(chat, query("backward", target + 1, before + 2))
        ahead = await source.get_messages(chat, query("forward", target, after + 1))
    found = bool(back) and back[0].id == target
    older = back[1:] if found else back
    more_older = len(older) > before
    more_newer = len(ahead) > after
    messages = [*older[:before][::-1], *back[:1] * found, *ahead[:after]]
    label = f"around #{target}" if found else f"around #{target} (not found; nearest shown)"

    def hint(direction: str, boundary: Message) -> str:
        token = encode_cursor(Cursor(chat.id, direction, boundary.id, None, None, None, False))  # type: ignore[arg-type]
        return f'call get_messages(chat="{chat.id}", cursor="{token}")'

    return render_around(
        messages_head(chat, label, tz),
        messages,
        target,
        tz=tz,
        max_chars=settings.max_chars,
        more_older=more_older,
        more_newer=more_newer,
        older_hint=lambda m: hint("backward", m),
        newer_hint=lambda m: hint("forward", m),
    )


async def _resolve_topic(source: MessageSource, chat: Chat, topic: str | int | None) -> Topic | None:
    if topic is None or (isinstance(topic, str) and not topic.strip()):
        return None
    if not chat.is_forum:
        raise ToolError(f'"{one_line(chat.title)}" is not a forum; omit topic.')
    raw = str(topic).strip().lstrip("#")
    if raw.isascii() and raw.isdigit():
        return Topic(int(raw), "", False)
    topics = (await source.get_chat_info(chat)).topics
    wanted = normalize(raw)
    tokens = wanted.split()
    hits = [t for t in topics if tokens and all(tok in normalize(t.title) for tok in tokens)]
    exact = [t for t in hits if normalize(t.title) == wanted]
    hits = exact or hits
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ToolError(f'Topic "{raw}" is ambiguous; pass one of these ids: {_topic_list(hits)}')
    shown = _topic_list(list(topics))
    raise ToolError(f'No topic matches "{raw}". Topics: {shown}' if topics else f'No topic matches "{raw}".')


def _topic_list(topics: list[Topic]) -> str:
    shown = "; ".join(f"#{t.id} {one_line(t.title)}" for t in topics[:MAX_CANDIDATES])
    return shown + (f"; …and {len(topics) - MAX_CANDIDATES} more" if len(topics) > MAX_CANDIDATES else "")


async def search_messages(
    source: MessageSource,
    settings: Settings,
    query: str,
    chat: str | None = None,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    from_me: bool = False,
    cursor: str | None = None,
    limit: int = 30,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> str:
    query = query.strip()
    if not query:
        raise ToolError("query is empty: pass the words to search for.")
    if len(query) > MAX_QUERY_CHARS:
        raise ToolError(f"query is too long (max {MAX_QUERY_CHARS} chars).")
    limit = max(MIN_HITS, min(MAX_HITS, limit))
    tz = settings.timezone
    with _tool_errors():
        parsed = decode_search_cursor(cursor, query) if cursor is not None and cursor.strip() else None
    if parsed is not None and parsed.scope == "global" and chat is not None and chat.strip():
        raise ToolError("This cursor is from a search in all chats; call search_messages without chat.")
    if parsed is not None and parsed.scope == "chat" and not (chat and chat.strip()):
        chat = str(parsed.after.chat_id)  # models drop arguments; the cursor knows its chat
    resolved = await resolve_chat_arg(source, chat, settings) if chat and chat.strip() else None
    with _tool_errors():
        if parsed is not None:
            if resolved is not None and resolved.id != parsed.after.chat_id:
                raise CursorError(other_chat=True, tool="search_messages")
            period, from_me = Period(parsed.start, parsed.end), parsed.from_me
        else:
            period = parse_period(days, since, until, tz, now())
        sq = SearchQuery(
            text=query,
            start=period.start,
            end=period.end,
            from_me=from_me,
            limit=limit + 1,  # the extra one only tells us whether more exist
            after=parsed.after if parsed else None,
        )
        found = await source.search_messages(sq, resolved)
        chats = None if resolved is not None else {c.id: c for c in await source.list_dialogs()}
    more = len(found) > limit
    shown = found[:limit]
    scope = "chat" if resolved is not None else "global"
    period_text = "all time" if period.start is None and period.end is None else describe_period(period, tz)
    head = search_head(query, resolved, period_text, tz, from_me)

    def next_hint(boundary: Message) -> str:
        after = SearchOffset(boundary.chat_id, boundary.id, boundary.date)
        return _search_call(query, resolved, after, scope, period, from_me)

    return render_hits(
        head,
        shown,
        chats=chats,
        query=query,
        tz=tz,
        max_chars=settings.max_chars,
        more=more,
        next_hint=next_hint,
    )


def _search_call(
    query: str, chat: Chat | None, after: SearchOffset, scope: str, period: Period, from_me: bool
) -> str:
    token = encode_search_cursor(SearchCursor(scope, after, period.start, period.end, from_me), query)  # type: ignore[arg-type]
    where = f', chat="{chat.id}"' if chat is not None else ""
    return f'call search_messages(query={quote_query(query)}{where}, cursor="{token}")'


async def export_chat(
    source: MessageSource,
    settings: Settings,
    chat: str,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    format: str = "md",
    resume: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    if format.strip().casefold() != "md":
        raise ToolError('Only format="md" is supported.')
    resolved = await resolve_chat_arg(source, chat, settings)
    tz = settings.timezone
    moment = now()
    with _tool_errors():
        # No period = the whole history, unlike get_messages. A resumed export keeps the period it started with.
        period = Period(None, None) if resume else parse_period(days, since, until, tz, moment)
        info = await source.get_chat_info(resolved)
        result = await export_chat_file(
            source, info, period, settings.export_dir, tz, moment, resume=resume, sleep=sleep, clock=clock
        )
    who = f"{one_line(resolved.title)} ({resolved.id})"
    if result.path is None and result.stopped is not None:
        # Stopped before the first page: saying "no messages" would be wrong.
        raise ToolError(f"Could not export: {one_line(result.stopped)}. No file written.")
    if result.path is None:
        text = f"No messages from {who} in this period ({period_text(period, tz)}); no file written."
    else:
        text = _export_text(result, who, resolved.id, period, tz)
    return text if len(text) <= settings.max_chars else text[: settings.max_chars - 1] + "…"


def _export_text(result: ExportResult, who: str, chat_id: int, period: Period, tz: ZoneInfo) -> str:
    noun = "message" if result.count == 1 else "messages"
    so_far = " so far" if result.paused else ""
    lines = [f"Exported {result.count} {noun}{so_far} from {who} to:", str(result.path)]
    first, last = result.first, result.last
    span = f"{when_text(first, tz)} → {when_text(last, tz)} ({tz.key})"
    call = f'export_chat(chat="{chat_id}", resume=true)'
    if result.paused:
        # Spelled out, as for get_messages: models stop early when a footer only hints at more.
        lines.append(f"So far: {span}. Not finished: one call has a time limit. Next: call {call}")
    elif result.stopped is not None:
        lines.append(
            f"Stopped early: {one_line(result.stopped)}. The file ends at {when_text(last, tz)}. "
            f"To continue this file later, call {call}."
        )
    else:
        lines.append(f"Range: {span}. The file has the full text; it was not read into this conversation.")
    return "\n".join(lines)
