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
    ReadCursor,
    SearchCursor,
    decode_cursor,
    decode_read_cursor,
    decode_search_cursor,
    encode_cursor,
    encode_read_cursor,
    encode_search_cursor,
    is_read_cursor,
)
from tg_bridge.errors import (
    AmbiguousChatError,
    AmbiguousFolderError,
    ChatNotFoundError,
    CommentsUnavailableError,
    CursorError,
    FolderNotFoundError,
    MediaError,
    MessageNotFoundError,
    NotLoggedInError,
    TgBridgeError,
)
from tg_bridge.export import ExportResult, export_chat_file, period_text, when_text
from tg_bridge.format import (
    MAX_CANDIDATES,
    SEP,
    ambiguous_text,
    chat_info_block,
    chat_line,
    comment_line_of,
    comments_head,
    folder_ambiguous_text,
    folder_label,
    folder_not_found_text,
    folders_text,
    messages_head,
    not_found_text,
    one_line,
    quote_query,
    render_around,
    render_hits,
    render_counted,
    render_page,
    search_head,
    size_text,
)
from tg_bridge.folders import chat_folders, folder_chats, resolve_folder
from tg_bridge.format_many import Item, chat_labels, render_many
from tg_bridge.headlines import group_reposts, headline
from tg_bridge.many import read_chats, search_chats
from tg_bridge.media import DownloadResult, SavedFile, download_message_media, safe_mime
from tg_bridge.resolve import match_chats, normalize, resolve_chat
from tg_bridge.source import Chat, Folder, Message, MessageQuery, MessageSource, SearchOffset, SearchQuery, Topic
from tg_bridge.timerange import Period, describe_period, now_details, parse_period

KINDS = ("user", "bot", "group", "supergroup", "channel")
MODES = ("full", "headlines")
# Every page of a folder read costs one request stream per chat, so a rule-based "Unread" folder is refused.
MAX_FOLDER_CHATS = 50
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


def now(settings: Settings, moment: datetime) -> str:
    return now_details(moment, settings.timezone)


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
    folder: str | int | None = None,
    min_folders: int | None = None,
) -> str:
    kinds = _kinds(kind)
    limit = max(MIN_LIMIT, min(MAX_LIMIT, limit))
    query = (query or "").strip() or None
    folder = str(folder).strip() if folder is not None else None
    folder = folder or None
    if min_folders is not None and min_folders < 1:
        raise ToolError("min_folders must be at least 1.")
    with _tool_errors():
        chats = await source.list_dialogs()
        folders = await source.list_folders() if folder is not None or min_folders is not None else []
    scope = ""
    if folder is not None:
        chosen = _resolve_folder_arg(folder, folders, chats, settings)
        scope = f' in folder "{folder_label(chosen)}"'
        chats = folder_chats(chosen, chats)
        if not chats:
            return f'Folder "{folder_label(chosen)}" has no chats.'
    if kinds is not None:
        chats = [c for c in chats if c.kind in kinds]
    if query is not None:
        chats = match_chats(query, chats)
    if not chats:
        if scope:
            return f'No chats{scope} match "{query}".' if query else f"No chats{scope} of that kind."
        return f'No chats match "{query}".' if query else "No chats found."
    # Archived dialogs come after the non-archived ones; sorted() is stable so Telegram's order is kept inside each.
    # A folder keeps its own order: the app shows archived members in place.
    if folder is None:
        chats = sorted(chats, key=lambda c: c.is_archived)
    label = f"Chats{scope}" + (f' matching "{query}"' if query else "")
    memberships: dict[int, list[Folder]] = {}
    if min_folders is not None:
        # Overlaps are computed here: intersecting several list_chats(folder=) pages is beyond a small model.
        memberships = {c.id: chat_folders(c, folders) for c in chats}
        chats = sorted((c for c in chats if len(memberships[c.id]) >= min_folders), key=lambda c: -len(memberships[c.id]))
        label += f"{',' if scope or query else ''} in {min_folders}+ folders"
        if not chats:
            matching = f' matching "{query}"' if query else ""
            return f"No chats{scope}{matching} are in {min_folders}+ folders."
    shown = chats[:limit]
    totals = _totals(chats)
    lines = [chat_line(c, settings.timezone) for c in shown]
    if min_folders is not None:
        lines = [
            f"{line}{SEP}folders: {', '.join(one_line(f.title) for f in memberships[c.id])}" for line, c in zip(lines, shown)
        ]
    return render_counted(lambda n: f"{label} ({n} shown of {len(chats)})\n{totals}", lines, settings.max_chars)


def _resolve_folder_arg(arg: str, folders: list[Folder], chats: list[Chat], settings: Settings) -> Folder:
    try:
        return resolve_folder(arg, folders)
    except FolderNotFoundError as e:
        raise ToolError(folder_not_found_text(e, chats, settings.max_chars)) from e
    except AmbiguousFolderError as e:
        raise ToolError(folder_ambiguous_text(e, chats, settings.max_chars)) from e


async def list_folders(source: MessageSource, settings: Settings) -> str:
    with _tool_errors():
        chats = await source.list_dialogs()
        folders = await source.list_folders()
    return folders_text(folders, chats, settings.max_chars)


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
    try:
        folders = chat_folders(resolved, await source.list_folders())
    except TgBridgeError:
        folders = None  # the folders line is a bonus: don't fail the call over it
    return chat_info_block(info, settings.timezone, folders)


async def get_messages(
    source: MessageSource,
    settings: Settings,
    chat: str | None = None,
    *,
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
    comments_of: int | None = None,
    limit: int | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[], float] = time.monotonic,
) -> str:
    chat = (chat or "").strip() or None
    folder = (str(folder).strip() if folder is not None else "") or None
    headlines = _headlines_mode(mode)
    has_cursor = cursor is not None and bool(cursor.strip())
    if comments_of is not None:
        _check_comments_args(chat, folder, topic, around, from_me, headlines, cursor if has_cursor else None)
    if has_cursor and is_read_cursor(cursor):
        return await _read_many(
            source, settings, chat=chat, folder=folder, headlines=headlines, days=days, since=since, until=until,
            period=period, from_me=from_me, cursor=cursor, limit=limit, now=now, clock=clock,
        )
    if has_cursor:
        if folder is not None:
            raise CursorError()  # plain cursors belong to one chat
    elif chat is not None and folder is not None:
        raise ToolError("Pass either chat or folder, not both.")
    elif folder is not None and (topic is not None or around is not None):
        raise ToolError("topic and around work with one chat, not with a folder.")
    if chat is None and folder is None:
        raise ToolError("Pass chat or folder.")
    wants_around = around is not None and not has_cursor
    if folder is not None or (headlines and not wants_around and not has_cursor):
        if headlines and topic is not None and folder is None:
            raise ToolError('mode="headlines" does not work with topic.')
        return await _read_many(
            source, settings, chat=chat, folder=folder, headlines=headlines, days=days, since=since, until=until,
            period=period, from_me=from_me, cursor=None, limit=limit, now=now, clock=clock,
        )
    resolved = await resolve_chat_arg(source, chat, settings)
    has_cursor = cursor is not None and bool(cursor.strip())
    around_mode = around is not None and not has_cursor  # a cursor continues a page; it wins over `around`
    if comments_of is not None or (has_cursor and _cursor_post_id(cursor, resolved.id) is not None):
        return await _read_comments(
            source, settings, resolved, comments_of, days=days, since=since, until=until, period=period,
            cursor=cursor if has_cursor else None, limit=limit, now=now,
        )
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
            period = parse_period(days, since, until, tz, now(), period)
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


def _check_comments_args(
    chat: str | None,
    folder: str | None,
    topic: str | int | None,
    around: int | None,
    from_me: bool,
    headlines: bool,
    cursor: str | None,
) -> None:
    """comments_of reads one channel's thread; arguments that mean something else are refused, naming the culprit."""
    for name, given in (
        ("folder", folder is not None),
        ("topic", topic is not None),
        ("around", around is not None),
        ("from_me", from_me),
        ('mode="headlines"', headlines),
    ):
        if given:
            raise ToolError(f"comments_of cannot be combined with {name}.")
    if chat is None:
        raise ToolError("comments_of needs chat: the channel the post belongs to.")
    if cursor is not None and is_read_cursor(cursor):
        raise ToolError(str(CursorError(other_post=True)))


def _cursor_post_id(cursor: str, chat_id: int) -> int | None:
    """The post a plain cursor pages the comments of, None for a channel-history cursor."""
    with _tool_errors():
        return decode_cursor(cursor, chat_id).post_id


async def _read_comments(
    source: MessageSource,
    settings: Settings,
    channel: Chat,
    post_id: int | None,
    *,
    days: int | None,
    since: str | None,
    until: str | None,
    period: str | None,
    cursor: str | None,
    limit: int | None,
    now: Callable[[], datetime],
) -> str:
    """One channel post's comment thread, oldest first. The cursor carries the post id, so it resumes alone."""
    tz = settings.timezone
    limit = max(MIN_MESSAGES, min(MAX_MESSAGES, DEFAULT_MESSAGES if limit is None else limit))
    if channel.kind != "channel":
        raise ToolError(
            f'comments_of works only with channels. "{one_line(channel.title)}" is a {channel.kind}: '
            "its replies are ordinary messages, read them with get_messages."
        )
    with _tool_errors():
        boundary_id = None
        if cursor is not None:
            c = decode_cursor(cursor, channel.id)
            if c.post_id is None or (post_id is not None and post_id != c.post_id):
                raise CursorError(other_post=True)
            post_id, span, boundary_id = c.post_id, Period(c.start, c.end), c.boundary_id
        else:
            span = parse_period(days, since, until, tz, now(), period)
        assert post_id is not None  # comments_of or the cursor named it
        post = await source.get_post(channel, post_id)
        title = one_line(channel.title)
        if post is None:
            raise ToolError(
                f'Post #{post_id} not found in "{title}". '
                "Take post ids from get_messages or search_messages on this channel."
            )
        if not post.comments_enabled:
            raise ToolError(f"Post #{post_id} has no comments: comments are off for this channel or this post.")
        query = MessageQuery(
            direction="forward", start=span.start, end=span.end, boundary_id=boundary_id, topic_id=None,
            from_me=False, limit=limit + 1,  # the extra one only tells us whether more exist
        )
        try:
            fetched = await source.get_comments(channel, post, query)
        except CommentsUnavailableError as e:
            raise ToolError(f"Telegram does not show the comments of post #{post_id} to this account.") from e
    bounded = span.start is not None or span.end is not None

    def next_hint(last: Message) -> str:
        token = encode_cursor(Cursor(channel.id, "forward", last.id, span.start, span.end, None, False, post_id))
        return f'More comments remain. Next: call get_messages(chat="{channel.id}", cursor="{token}")'

    return render_page(
        comments_head(channel, post, describe_period(span, tz) if bounded else None, tz),
        fetched[:limit],
        tz=tz,
        max_chars=settings.max_chars,
        keep="oldest",
        more=len(fetched) > limit,
        next_hint=next_hint,
        end_text=f"End of comments on post #{post_id}.",
        empty_text="No comments in this period." if bounded else f"No comments on post #{post_id}.",
        line_of=comment_line_of(channel, post_id),
    )


def _headlines_mode(mode: str) -> bool:
    wanted = (mode or "").strip().casefold() or "full"
    if wanted not in MODES:
        raise ToolError(f'Unknown mode "{mode.strip()}". Allowed: {", ".join(MODES)}.')
    return wanted == "headlines"


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _read_key(msg: Message) -> tuple[int, int, int]:
    return (int(msg.date.timestamp()), msg.chat_id, msg.id)  # whole seconds: that is all a cursor keeps


async def _read_many(
    source: MessageSource,
    settings: Settings,
    *,
    chat: str | None,
    folder: str | None,
    headlines: bool,
    days: int | None,
    since: str | None,
    until: str | None,
    period: str | None,
    from_me: bool,
    cursor: str | None,
    limit: int | None,
    now: Callable[[], datetime],
    clock: Callable[[], float],
) -> str:
    """Folder reads and headline digests: read the whole period of every chat, merge, and page the merged list.
    Every page re-reads the period; the read cursor only says where the previous page stopped."""
    tz = settings.timezone
    limit = max(MIN_MESSAGES, min(MAX_MESSAGES, DEFAULT_MESSAGES if limit is None else limit))
    c: ReadCursor | None = None
    with _tool_errors():
        if cursor is not None:
            c = decode_read_cursor(cursor)
            if (c.folder_id is not None and chat is not None) or (c.folder_id is None and folder is not None):
                raise CursorError()
            span, from_me, headlines = Period(c.start, c.end), c.from_me, c.headlines
        else:
            span = parse_period(days, since, until, tz, now(), period)
            if span.start is None and span.end is None:
                raise ToolError(
                    'Reading a folder needs a period, e.g. period="today", period="this_week" or days=3.'
                    if folder is not None
                    else 'mode="headlines" needs a period, e.g. period="this_week" or days=7.'
                )
    folder_id = c.folder_id if c is not None else None
    chosen: Folder | None = None
    single: Chat | None = None
    with _tool_errors():
        if folder is not None or folder_id is not None:
            folders = await source.list_folders()
            dialogs = await source.list_dialogs()
            if folder_id is not None:
                chosen = _folder_by_id(folder_id, folders, dialogs, settings)
                if folder is not None and _resolve_folder_arg(folder, folders, dialogs, settings).id != folder_id:
                    raise CursorError(other_folder=True)
            else:
                chosen = _resolve_folder_arg(folder, folders, dialogs, settings)
            members = _folder_members(chosen, dialogs)
            scope_text = f'folder "{folder_label(chosen)}"'
        else:
            arg = chat if chat is not None else str(c.after.chat_id)  # type: ignore[union-attr]  # a cursor knows its chat
            single = await resolve_chat_arg(source, arg, settings)
            if c is not None and single.id != c.after.chat_id:
                raise CursorError(other_chat=True)
            members = [single]
            scope_text = f'"{one_line(single.title)}"'
    with _tool_errors():
        result = await read_chats(source, members, span.start, span.end, from_me, scope_text=scope_text, clock=clock)

    posts = [m for m in result.messages if m.action is None] if headlines else result.messages
    items: list[Item]
    skipped = 0
    if headlines:
        with_text = [m for m in posts if headline(m.text) is not None]
        skipped = len(posts) - len(with_text)
        items = list(group_reposts(with_text))
        merged = len(with_text) - len(items)
    else:
        items = list(result.messages)
        merged = 0
    first_of = (lambda i: i.first) if headlines else (lambda i: i)  # noqa: E731
    boundary = _read_key_of(c)
    seen = 0 if boundary is None else sum(_read_key(first_of(i)) <= boundary for i in items)
    remaining = items[seen:]
    chats_with_posts = len({m.chat_id for m in posts})
    where = f" in {chats_with_posts} of {len(members)} chats" if single is None else ""

    def totals(a: int, b: int) -> str:
        if headlines:
            extras = [f"{_plural(merged, 'repeat')} merged"] * bool(merged) + [f"{skipped} without text skipped"] * bool(skipped)
            detail = f" ({', '.join(extras)})" if extras else ""
            return f"{_plural(len(posts), 'post')}{where} → {_plural(len(items), 'headline')}{detail}; showing {a}–{b}."
        return f"{_plural(len(posts), 'message')}{where}; showing {a}–{b}."

    def next_hint(last: Message) -> str:
        token = encode_read_cursor(
            ReadCursor(
                SearchOffset(last.chat_id, last.id, last.date), span.start, span.end,
                chosen.id if chosen is not None else None, headlines, from_me,
            )
        )
        scope = f'folder="{chosen.id}"' if chosen is not None else f'chat="{single.id}"'  # type: ignore[union-attr]
        return f'call get_messages({scope}, cursor="{token}")'

    when = describe_period(span, tz)
    if chosen is not None:
        head = f"Folder: {folder_label(chosen)} ({chosen.id}){SEP}{_plural(len(members), 'chat')}{SEP}{when} ({tz.key})"
        if from_me:
            head += "\nFrom me only"
    else:
        head = messages_head(single, when, tz, None, from_me) + "\nMode: headlines"  # type: ignore[arg-type]
    return render_many(
        head,
        totals,
        remaining,
        offset=seen,
        chats={m.id: m for m in members},
        labels=chat_labels(members) if chosen is not None else None,
        tz=tz,
        max_chars=settings.max_chars,
        limit=limit,
        next_hint=next_hint,
        empty_text="No posts with text in this period." if headlines else "No messages in this period.",
    )


def _folder_members(folder: Folder, dialogs: list[Chat]) -> list[Chat]:
    members = folder_chats(folder, dialogs)
    name = folder_label(folder)
    if not members:
        raise ToolError(f'Folder "{name}" has no chats.')
    if len(members) > MAX_FOLDER_CHATS:
        raise ToolError(
            f'Folder "{name}" has {len(members)} chats; folder queries handle up to {MAX_FOLDER_CHATS}. '
            "Use a smaller folder, or search without folder."
        )
    return members


def _folder_by_id(folder_id: int, folders: list[Folder], dialogs: list[Chat], settings: Settings) -> Folder:
    """The folder a cursor was issued for; by id only, since a title match could hit another folder."""
    found = next((f for f in folders if f.id == folder_id), None)
    if found is None:
        missing = FolderNotFoundError(str(folder_id), folders)
        raise ToolError(folder_not_found_text(missing, dialogs, settings.max_chars))
    return found


def _read_key_of(c: ReadCursor | None) -> tuple[int, int, int] | None:
    return None if c is None else (int(c.after.date.timestamp()), c.after.chat_id, c.after.message_id)


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
    folder: str | int | None = None,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    period: str | None = None,
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
    folder = (str(folder).strip() if folder is not None else "") or None
    with _tool_errors():
        parsed = decode_search_cursor(cursor, query) if cursor is not None and cursor.strip() else None
    if parsed is not None and parsed.scope == "folder" and chat is not None and chat.strip():
        raise ToolError("This cursor is from a folder search; call search_messages with folder or only the cursor.")
    if parsed is not None and parsed.scope == "global" and chat is not None and chat.strip():
        raise ToolError("This cursor is from a search in all chats; call search_messages without chat.")
    if parsed is not None and parsed.scope == "global" and folder is not None:
        raise ToolError("This cursor is from a search in all chats; call search_messages without folder.")
    if parsed is not None and parsed.scope == "chat" and folder is not None:
        raise ToolError("This cursor is from a search in one chat; call search_messages with chat or only the cursor.")
    if chat is not None and chat.strip() and folder is not None:
        raise ToolError("Pass either chat or folder, not both.")
    if parsed is not None and parsed.scope == "folder":
        return await _search_folder(
            source, settings, query, folder, parsed, limit=limit, tz=tz, from_me=from_me, days=days, since=since,
            until=until, period=period, now=now,
        )
    if folder is not None:
        return await _search_folder(
            source, settings, query, folder, None, limit=limit, tz=tz, from_me=from_me, days=days, since=since,
            until=until, period=period, now=now,
        )
    if parsed is not None and parsed.scope == "chat" and not (chat and chat.strip()):
        chat = str(parsed.after.chat_id)  # models drop arguments; the cursor knows its chat
    resolved = await resolve_chat_arg(source, chat, settings) if chat and chat.strip() else None
    with _tool_errors():
        if parsed is not None:
            if resolved is not None and resolved.id != parsed.after.chat_id:
                raise CursorError(other_chat=True, tool="search_messages")
            period, from_me = Period(parsed.start, parsed.end), parsed.from_me
        else:
            period = parse_period(days, since, until, tz, now(), period)
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


async def _search_folder(
    source: MessageSource,
    settings: Settings,
    query: str,
    folder: str | None,
    parsed: SearchCursor | None,
    *,
    limit: int,
    tz: ZoneInfo,
    from_me: bool,
    days: int | None,
    since: str | None,
    until: str | None,
    period: str | None,
    now: Callable[[], datetime],
) -> str:
    """Search every chat of a folder with the per-chat search and merge the hits newest first."""
    with _tool_errors():
        folders = await source.list_folders()
        dialogs = await source.list_dialogs()
        if parsed is not None:
            chosen = _folder_by_id(parsed.folder_id, folders, dialogs, settings)  # type: ignore[arg-type]
            if folder is not None and _resolve_folder_arg(folder, folders, dialogs, settings).id != chosen.id:
                raise CursorError(other_folder=True, tool="search_messages")
            span, from_me = Period(parsed.start, parsed.end), parsed.from_me
        else:
            chosen = _resolve_folder_arg(folder, folders, dialogs, settings)  # type: ignore[arg-type]
            span = parse_period(days, since, until, tz, now(), period)
        members = _folder_members(chosen, dialogs)
        found, more = await search_chats(
            source, members, query, span.start, span.end, from_me, limit, parsed.after if parsed else None
        )
    when = "all time" if span.start is None and span.end is None else describe_period(span, tz)
    head = search_head(query, None, when, tz, from_me, folder=chosen, folder_size=len(members))

    def next_hint(boundary: Message) -> str:
        after = SearchOffset(boundary.chat_id, boundary.id, boundary.date)
        return _search_call(query, None, after, "folder", span, from_me, chosen)

    return render_hits(
        head,
        found,
        chats={c.id: c for c in members},
        query=query,
        tz=tz,
        max_chars=settings.max_chars,
        more=more,
        next_hint=next_hint,
    )


def _search_call(
    query: str,
    chat: Chat | None,
    after: SearchOffset,
    scope: str,
    period: Period,
    from_me: bool,
    folder: Folder | None = None,
) -> str:
    folder_id = folder.id if folder is not None else None
    cursor = SearchCursor(scope, after, period.start, period.end, from_me, folder_id)  # type: ignore[arg-type]
    token = encode_search_cursor(cursor, query)
    where = f', chat="{chat.id}"' if chat is not None else f', folder="{folder.id}"' if folder is not None else ""
    return f'call search_messages(query={quote_query(query)}{where}, cursor="{token}")'


async def export_chat(
    source: MessageSource,
    settings: Settings,
    chat: str,
    *,
    days: int | None = None,
    since: str | None = None,
    until: str | None = None,
    period: str | None = None,
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
        period = Period(None, None) if resume else parse_period(days, since, until, tz, moment, period)
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


async def download_media(
    source: MessageSource,
    settings: Settings,
    chat: str,
    message_id: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    resolved = await resolve_chat_arg(source, chat, settings)
    call = f'download_media(chat="{resolved.id}", message_id={message_id})'
    try:
        result = await download_message_media(
            source, resolved, message_id, settings.download_dir, settings.max_download_bytes, clock=clock
        )
    except (MediaError, MessageNotFoundError, NotLoggedInError) as e:
        raise ToolError(str(e)) from e
    except TgBridgeError as e:
        # The .part file stays, so the same call carries on from it.
        raise ToolError(f"{e}. Progress is kept: call {call} again to continue.") from e
    except OSError as e:
        # strerror only: str(e) would carry the path.
        raise ToolError(f"Could not write the file: {e.strerror}.") from e
    who = f"{one_line(resolved.title)} ({resolved.id})"
    text = _download_text(result, who, message_id, call)
    return text if len(text) <= settings.max_chars else text[: settings.max_chars - 1] + "…"


def _download_text(result: DownloadResult, who: str, message_id: int, call: str) -> str:
    if result.paused:
        so_far = size_text(result.done_bytes)
        of_total = "" if result.total_bytes is None else f" of {size_text(result.total_bytes)}"
        count = f" ({len(result.saved)}/{result.file_count} files)" if result.album else ""
        # Spelled out, as for export_chat: models stop early when a footer only hints at more.
        return (
            f"Downloaded {so_far}{of_total} so far from {who} #{message_id}{count}. "
            f"Not finished: one call has a time limit. Next: call {call}"
        )
    if result.album:
        total = sum(s.path.stat().st_size for s in result.saved)
        lines = [
            f"Downloaded album of {len(result.saved)} files ({size_text(total)}) from {who} #{message_id} to folder:",
            str(result.folder),
            *(_album_line(s) for s in result.saved),
        ]
    else:
        saved = result.saved[0]
        head = f"{saved.file.media.kind} ({_size_and_mime(saved)}) from {who} #{message_id}"
        lines = [f"Already downloaded: {head}:" if saved.existed else f"Downloaded {head} to:", str(saved.path)]
    lines.append("The file was not read into this conversation.")
    return "\n".join(lines)


def _size_and_mime(saved: SavedFile) -> str:
    return f"{size_text(saved.path.stat().st_size)}, {safe_mime(saved.file.media.mime_type) or 'unknown type'}"


def _album_line(saved: SavedFile) -> str:
    size = size_text(saved.path.stat().st_size)
    line = f"{saved.path}{SEP}{size}{SEP}{safe_mime(saved.file.media.mime_type) or 'unknown type'}"
    return line + " (already there)" if saved.existed else line
