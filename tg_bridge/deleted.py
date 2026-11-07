"""Chats whose other person deleted their Telegram account (design §2 scenario 3)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from tg_bridge.errors import RateLimitedError, TgBridgeError
from tg_bridge.export import EXPORT_TIME_BUDGET, export_chat_file, latest_export
from tg_bridge.format import SEP, one_line
from tg_bridge.source import Chat, ContactCard, HistoryStats, MessageSource
from tg_bridge.timerange import Period

log = logging.getLogger(__name__)

MAX_GUESSES = 3
DELETED_DIR = "deleted-users"  # under the export dir; fixed, so no caller-supplied path ever reaches the disk
MIN_START = 5.0  # seconds left that justify starting or resuming a chat
NONE_FOUND = "No chats with deleted accounts (1:1 chats, archived included)."


@dataclass(frozen=True)
class DeletedDialog:
    chat: Chat
    stats: HistoryStats | None  # None: Telegram refused the count for this chat
    guesses: tuple[str, ...]


def deleted_dialogs(chats: Iterable[Chat]) -> list[Chat]:
    """1:1 chats with a deleted peer (bots have their own kind), in id order so repeated calls walk the same list."""
    return sorted((c for c in chats if c.kind == "user" and c.deleted_peer), key=lambda c: c.id)


def identity_guesses(peer_id: int, cards: Sequence[ContactCard], tz: ZoneInfo) -> tuple[str, ...]:
    """Names from contact cards whose user is the deleted peer: the only identity that survives the deletion.
    Newest card first, distinct names, other people's cards ignored."""
    seen: set[str] = set()
    out: list[str] = []
    for card in sorted(cards, key=lambda c: (c.date, c.message_id), reverse=True):
        if card.user_id != peer_id:
            continue
        name = " ".join(p.strip() for p in (card.first_name, card.last_name) if p and p.strip())
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        day = card.date.astimezone(tz).date().isoformat()
        out.append(f"{one_line(name)} (name on a contact card, #{card.message_id}, {day}; a guess)")
        if len(out) == MAX_GUESSES:
            break
    return tuple(out)


async def scan_deleted(source: MessageSource, tz: ZoneInfo) -> list[DeletedDialog]:
    """Count and identity guess for every deleted-account chat. A rate limit aborts the scan (one retry later is
    cheaper than a half list); other refusals only blank that chat's count or guess."""
    found: list[DeletedDialog] = []
    for chat in deleted_dialogs(await source.list_dialogs()):
        try:
            stats: HistoryStats | None = await source.get_history_stats(chat)
        except RateLimitedError:
            raise
        except TgBridgeError:
            stats = None
        try:
            guesses = identity_guesses(chat.id, await source.get_contact_cards(chat), tz)
        except RateLimitedError:
            raise
        except TgBridgeError:
            guesses = ()
        found.append(DeletedDialog(chat, stats, guesses))
    log.info("deleted-account chats found=%d", len(found))
    return found


def _day(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).date().isoformat()


def deleted_line(item: DeletedDialog, tz: ZoneInfo) -> str:
    chat, stats = item.chat, item.stats
    parts = [one_line(chat.title), f"id {chat.id}"]
    if stats is None:
        parts.append("count unavailable")
        if chat.last_message_at:
            parts.append(f"last {_day(chat.last_message_at, tz)}")
    else:
        parts.append(f"{stats.count} {'message' if stats.count == 1 else 'messages'}")
        if stats.first_at and chat.last_message_at:
            parts.append(f"{_day(stats.first_at, tz)} → {_day(chat.last_message_at, tz)}")
    if chat.is_archived:
        parts.append("archived")
    if item.guesses:
        parts.append("guess: " + "; ".join(item.guesses))
    return SEP.join(parts)


OutcomeStatus = Literal["exported", "already", "paused", "stopped", "empty"]


@dataclass(frozen=True)
class ChatOutcome:
    chat: Chat
    status: OutcomeStatus
    path: Path | None
    count: int
    reason: str | None = None


async def export_deleted(
    source: MessageSource,
    dest_dir: Path,
    tz: ZoneInfo,
    now: datetime,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[ChatOutcome], int]:
    """Writes one Markdown file per deleted-account chat, sharing one time budget. Idempotent: complete files are
    skipped, an unfinished one is resumed. Returns the outcomes of the chats it reached and how many it never started."""
    chats = deleted_dialogs(await source.list_dialogs())
    deadline = clock() + EXPORT_TIME_BUDGET
    outcomes: list[ChatOutcome] = []
    for index, chat in enumerate(chats):
        latest = latest_export(dest_dir, chat)
        if latest is not None and latest[1] == "complete":
            outcomes.append(ChatOutcome(chat, "already", latest[0], 0))
            continue
        if clock() > deadline - MIN_START:
            return outcomes, len(chats) - index
        outcome = await _export_one(source, chat, latest is not None, dest_dir, tz, now, deadline, sleep, clock)
        outcomes.append(outcome)
        if outcome.status in ("paused", "stopped"):
            return outcomes, len(chats) - index - 1
    return outcomes, 0


async def _export_one(
    source: MessageSource,
    chat: Chat,
    resume: bool,
    dest_dir: Path,
    tz: ZoneInfo,
    now: datetime,
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> ChatOutcome:
    try:
        info = await source.get_chat_info(chat)
        try:
            guesses = identity_guesses(chat.id, await source.get_contact_cards(chat), tz)
        except RateLimitedError:
            raise
        except TgBridgeError:
            guesses = ()  # the guess is a bonus; the export is not
    except TgBridgeError as e:
        return ChatOutcome(chat, "stopped", None, 0, str(e))
    result = await export_chat_file(
        source, info, Period(None, None), dest_dir, tz, now,
        resume=resume, budget=deadline - clock(), identity=guesses, sleep=sleep, clock=clock,
    )
    if result.stopped is not None:
        return ChatOutcome(chat, "stopped", result.path, result.count, result.stopped)
    if result.path is None:
        return ChatOutcome(chat, "empty", None, 0)
    return ChatOutcome(chat, "paused" if result.paused else "exported", result.path, result.count)
