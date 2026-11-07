"""Chat → Markdown file (design §6 export). Pure renderers first, then the streaming writer."""

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO
from zoneinfo import ZoneInfo

from tg_bridge.cursor import ExportCursor, decode_export_cursor, encode_export_cursor
from tg_bridge.errors import CursorError, ExportFileError, RateLimitedError, TgBridgeError
from tg_bridge.format import SEP, WEEKDAYS, chat_link_base, duration_text, one_line, size_text
from tg_bridge.resolve import normalize
from tg_bridge.source import Chat, ChatInfo, MediaInfo, Message, MessageQuery, MessageSource, Person
from tg_bridge.timerange import Period

log = logging.getLogger(__name__)

SLUG_CHARS = 40
EXPORT_PAGE = 500
# Telethon already sleeps through FloodWaits up to 30 s; beyond MAX_FLOOD_WAIT we stop rather than hang the tool call.
MAX_FLOOD_WAIT = 300
MAX_FLOOD_RETRIES = 3
# LM Studio gives up on a tool call after ~60 s; a call pauses after this many seconds and can be resumed.
EXPORT_TIME_BUDGET = 40.0
PAUSED = "paused, to be continued"
_FOOTER_TAIL = 4096  # bytes; the footer is two short lines
# Hidden in rendered Markdown. It carries what `resume` needs, so the model has nothing to copy (it garbled cursors).
_RESUME_NOTE = re.compile(rb"<!-- tg-bridge resume: (\S+) -->")
MAX_NAME_SUFFIX = 99
# Every control character but \n and \t (line breaks are normalised before this runs).
_STRIP = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_INLINE = re.compile(r"([\\*_\[\]`])")
_BLOCK_START = ("#", ">", "-", "+", "*", "=", "|", "```", "~~~")
_NUMBERED = re.compile(r"\d+[.)](\s|$)")


@dataclass
class RenderState:
    """What a page needs to know about the pages before it."""

    day: date | None = None
    seen_people: set[int] = field(default_factory=set)


def when_text(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def period_text(period: Period, tz: ZoneInfo) -> str:
    if period.start is None:
        return "whole history" if period.end is None else f"everything before {when_text(period.end, tz)}"
    return f"{when_text(period.start, tz)} → {when_text(period.end, tz) if period.end else 'now'}"


def chat_slug(title: str) -> str:
    """Folder/file-name form of a chat title; has no underscores, so `_<chat id>` after it is unambiguous."""
    return normalize(title).replace(" ", "-")[:SLUG_CHARS].strip("-") or "chat"


def export_filename(chat: Chat, period: Period, first: datetime, tz: ZoneInfo, now: datetime, n: int = 1) -> str:
    slug = chat_slug(chat.title)
    start = (period.start or first).astimezone(tz).date()
    # `end` is exclusive: the day it falls on is only included when it is not exactly midnight.
    stop = ((period.end - timedelta(microseconds=1)) if period.end else now).astimezone(tz).date()
    suffix = f"_{n}" if n > 1 else ""
    return f"{slug}_{chat.id}_{start.isoformat()}_{stop.isoformat()}_{now.astimezone(tz):%Y%m%d-%H%M%S}{suffix}.md"


def render_header(
    info: ChatInfo, period: Period, tz: ZoneInfo, now: datetime, identity: Sequence[str] = ()
) -> str:
    chat = info.chat
    flags = [chat.kind, f"id {chat.id}"]
    if chat.username:
        flags.append(f"@{chat.username}")
    if chat.is_forum:
        flags.append("forum")
    if chat.is_archived:
        flags.append("archived")
    lines = [f"# {one_line(chat.title)}", "", f"- Chat: {SEP.join(flags)}"]
    if chat.peer is not None:
        lines.append(f"- Peer: {_peer_text(chat.peer)}")
        lines.extend(f"- Identity guess: {one_line(g)}" for g in identity)
    elif info.members_count is not None:
        lines.append(f"- Members: {info.members_count}")
    if info.description:
        lines.append(f"- Description: {one_line(info.description)}")
    span = period_text(period, tz)
    lines.append(f"- Period: {span if period.start is None and period.end is None else f'{span} ({tz.key})'}")
    lines.append(f"- Exported: {now.astimezone(tz):%Y-%m-%d %H:%M:%S} ({tz.key}) by tg-bridge")
    base = chat_link_base(chat)
    if base:
        lines.append(f"- Links: {base}/<id>")
    return "\n".join(lines) + "\n\n"


def _peer_text(peer: Person) -> str:
    parts = [one_line(peer.display_name)]
    # Raw fields too: the display name hides what Telegram actually stores (D9).
    if peer.first_name:
        parts.append(f"first name: {one_line(peer.first_name)}")
    if peer.last_name:
        parts.append(f"last name: {one_line(peer.last_name)}")
    if peer.username:
        parts.append(f"@{peer.username}")
    parts.append(f"id {peer.id}")
    if peer.phone:
        parts.append(f"phone: {peer.phone}")
    if peer.is_bot:
        parts.append("bot")
    if peer.is_deleted:
        parts.append("deleted")
    return SEP.join(parts)


def escape_block(line: str) -> str:
    """Stops a line of chat text from turning into a heading, list, quote, table or code fence. Inline markup stays."""
    body = line.lstrip()
    if body.startswith(_BLOCK_START) or _NUMBERED.match(body):
        indent = len(line) - len(body)
        return f"{line[:indent]}\\{body}"
    return line


def _inline(text: str) -> str:
    return _INLINE.sub(r"\\\1", one_line(text))


def render_messages(messages: Sequence[Message], chat: Chat, tz: ZoneInfo, state: RenderState) -> str:
    base = chat_link_base(chat)
    out: list[str] = []
    for msg in messages:
        local = msg.date.astimezone(tz)
        if local.date() != state.day:
            state.day = local.date()
            out.append(f"## {state.day.isoformat()} {WEEKDAYS[state.day.weekday()]}\n\n")
        out.append(_message_block(msg, chat, local, base, state))
    return "".join(out)


def _message_block(msg: Message, chat: Chat, local: datetime, base: str | None, state: RenderState) -> str:
    head = f"**{local:%H:%M} {_sender(msg, state)}** {_ref(msg.id, base)}"
    if chat.is_forum and msg.topic_id:
        head += f" topic #{msg.topic_id}"
    if msg.reply_to_id is not None:
        head += f" ↩{_ref(msg.reply_to_id, base)}"
    if msg.forward_from:
        head += f" ⤷fwd {_inline(msg.forward_from)}"
    if msg.edit_date is not None:
        head += " ✎"
    lines = [head]
    if msg.action:
        lines.append(f"[{one_line(msg.action)}]")
    elif msg.text:
        text = _STRIP.sub("", msg.text.replace("\r\n", "\n").replace("\r", "\n"))
        lines.extend(escape_block(line) for line in text.split("\n"))
    if msg.media is not None:
        lines.append(_media_line(msg.media, f"{base}/{msg.id}" if base else None))
    return "\n".join(lines) + "\n\n"


def _ref(message_id: int, base: str | None) -> str:
    # A clickable #id where Telegram has message links (supergroups, channels).
    return f"[#{message_id}]({base}/{message_id})" if base else f"#{message_id}"


def _sender(msg: Message, state: RenderState) -> str:
    if msg.out:
        return "Me"
    if msg.sender is not None:
        name = _inline(msg.sender.display_name)
        first_time = msg.sender.id not in state.seen_people
        state.seen_people.add(msg.sender.id)
        return f"{name} (@{msg.sender.username})" if first_time and msg.sender.username else name
    return _inline(msg.sender_title) if msg.sender_title else "?"


def _media_line(media: MediaInfo, link: str | None) -> str:
    parts = [media.kind]
    if media.kind in ("sticker", "poll") and media.label:
        parts.append(" ".join(one_line(media.label).replace("⏎", " ").split()))
    if media.file_name:
        parts.append(one_line(media.file_name))
    if media.duration is not None:
        parts.append(duration_text(media.duration))
    if media.size is not None:
        parts.append(size_text(media.size))
    if link:
        parts.append(link)
    return f"[{SEP.join(parts)}]"


def render_footer(
    count: int, first: datetime | None, last: datetime | None, tz: ZoneInfo, stopped: str | None
) -> str:
    noun = "message" if count == 1 else "messages"
    parts = [f"Exported {count} {noun}"]
    if first is not None and last is not None:
        parts.append(f"{when_text(first, tz)} → {when_text(last, tz)} ({tz.key})")
    parts.append("complete" if stopped is None else f"INCOMPLETE: {one_line(stopped)}")
    return "---\n" + SEP.join(parts) + "\n"


# --- streaming -------------------------------------------------------------


@dataclass(frozen=True)
class ExportResult:
    path: Path | None  # None: no messages, so no file
    count: int  # in the whole file, earlier calls included
    first: datetime | None  # aware UTC
    last: datetime | None
    stopped: str | None  # why it ended early; None = complete or paused
    paused: bool = False  # hit the time limit of one call


def _create_file(
    dest_dir: Path, chat: Chat, period: Period, first: datetime, tz: ZoneInfo, now: datetime
) -> tuple[Path, int, TextIO]:
    """Never overwrites: O_EXCL, and a numeric suffix when the name is taken (two exports within one second)."""
    dest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in range(1, MAX_NAME_SUFFIX + 1):
        path = dest_dir / export_filename(chat, period, first, tz, now, n)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        return path, n, os.fdopen(fd, "w", encoding="utf-8", newline="\n")
    raise FileExistsError(f"no free export file name after {MAX_NAME_SUFFIX} tries")


ExportState = Literal["complete", "unfinished"]


def _newest_file(dest_dir: Path, chat: Chat) -> Path | None:
    # The slug has no underscores, so `_<id>_` only matches this chat's files.
    files = list(dest_dir.glob(f"*_{chat.id}_*.md")) if dest_dir.is_dir() else []
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def _read_footer(path: Path, chat: Chat) -> tuple[ExportCursor | None, bool, int]:
    """What the file's tail says: its resume state (None without a valid note), whether it ends complete, and the
    byte size the file has without the footer (where appending continues)."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - _FOOTER_TAIL))
        tail = f.read()
    # Chat text never yields a bare "---" line (escape_block), so this is the footer.
    cut = tail.rfind(b"\n---\n")
    note = _RESUME_NOTE.search(tail, cut) if cut >= 0 else None
    state = None
    if note is not None:
        try:
            state = decode_export_cursor(note.group(1).decode("ascii"), chat.id)
        except CursorError:
            state = None
    complete = note is None and tail.rstrip().endswith(b" complete")
    return state, complete, size - len(tail) + cut + 1


def latest_export(dest_dir: Path, chat: Chat) -> tuple[Path, ExportState] | None:
    """The chat's newest export file and whether it is finished; None when there is none or it can't be continued."""
    path = _newest_file(dest_dir, chat)
    if path is None:
        return None
    state, complete, _ = _read_footer(path, chat)
    if complete:
        return path, "complete"
    return (path, "unfinished") if state is not None else None


def _reopen_latest(dest_dir: Path, chat: Chat) -> tuple[Path, ExportCursor, TextIO]:
    """The chat's newest export file, its resume state, and the file opened for appending with the footer cut off."""
    nothing = ExportFileError("No unfinished export of this chat to resume. Call export_chat without resume to start one.")
    path = _newest_file(dest_dir, chat)
    if path is None:
        raise nothing
    state, complete, keep = _read_footer(path, chat)
    if state is None:
        if complete:
            raise ExportFileError(f"The latest export of this chat is already complete: {path}")
        raise nothing
    os.truncate(path, keep)
    out = os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND), "w", encoding="utf-8", newline="\n")
    return path, state, out


async def export_chat_file(
    source: MessageSource,
    info: ChatInfo,
    period: Period,
    dest_dir: Path,
    tz: ZoneInfo,
    now: datetime,
    *,
    resume: bool = False,
    budget: float = EXPORT_TIME_BUDGET,
    identity: Sequence[str] = (),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ExportResult:
    """Pages through the chat oldest first and appends each page to a new Markdown file in `dest_dir`, or, with
    `resume`, to the chat's latest unfinished file (its period wins over `period`). After EXPORT_TIME_BUDGET seconds
    it (or `budget` seconds) pauses; the file then ends with a hidden note that `resume` continues from.
    A rate limit or other bridge error mid-way ends the file with an INCOMPLETE footer and is reported in the result;
    anything else also leaves that footer but propagates."""
    chat = info.chat
    state = RenderState()
    started, budget_start = time.monotonic(), clock()
    out: TextIO | None = None
    path: Path | None = None
    file_n = 1
    file_stamp = now.replace(microsecond=0)
    count = pages = retries = 0
    first = last = None
    stopped: str | None = None
    paused = False
    boundary: int | None = None
    if resume:
        path, note, out = _reopen_latest(dest_dir, chat)
        period = Period(note.start, note.end)
        file_n, file_stamp, boundary = note.file_n, note.file_stamp, note.boundary_id
        count, first, last = note.count, note.first, note.last
        state.day = last.astimezone(tz).date()

    def resume_note() -> str:
        if boundary is None or first is None or last is None:
            return ""
        token = encode_export_cursor(
            ExportCursor(chat.id, file_stamp, file_n, boundary, period.start, period.end, count, first, last)
        )
        return f"<!-- tg-bridge resume: {token} -->\n"

    try:
        while True:
            query = MessageQuery("forward", period.start, period.end, boundary, None, False, EXPORT_PAGE)
            try:
                page = await source.get_messages(chat, query)
            except RateLimitedError as e:
                if e.seconds > MAX_FLOOD_WAIT or retries >= MAX_FLOOD_RETRIES:
                    stopped = str(e)
                    break
                retries += 1
                await sleep(e.seconds + 1)
                continue
            except TgBridgeError as e:
                stopped = str(e)
                break
            pages += 1
            if not page:
                break
            if out is None:  # the file only exists once there is something to put in it
                path, file_n, out = _create_file(dest_dir, chat, period, page[0].date, tz, now)
                out.write(render_header(info, period, tz, now, identity))
            out.write(render_messages(page, chat, tz, state))
            out.flush()
            count += len(page)
            first = first or page[0].date
            last = page[-1].date
            boundary = page[-1].id
            if len(page) < EXPORT_PAGE:
                break
            if clock() - budget_start >= budget:
                paused = True
                break
        if out is not None:
            out.write(render_footer(count, first, last, tz, PAUSED if paused else stopped))
            if paused or stopped is not None:
                out.write(resume_note())
    except BaseException:
        if out is not None:
            out.write(render_footer(count, first, last, tz, "interrupted") + resume_note())
        raise
    finally:
        if out is not None:
            out.close()
    # Chat id and counts only: the path holds the chat title.
    log.info(
        "export chat=%s messages=%d pages=%d seconds=%.1f %s",
        chat.id, count, pages, time.monotonic() - started,
        "paused" if paused else "complete" if stopped is None else "stopped",
    )
    return ExportResult(path, count, first, last, stopped, paused)
