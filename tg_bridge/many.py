"""Reading and searching several chats (a folder) through the single-chat `MessageSource` calls.

No new Telegram requests: chats are read one after another, never concurrently, so FloodWait handling stays in the
client. Telethon-free; works on domain models.
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from tg_bridge.errors import ReadTimeoutError, TooManyMessagesError
from tg_bridge.source import Chat, Message, MessageQuery, MessageSource, SearchOffset, SearchQuery

log = logging.getLogger(__name__)

MAX_READ_MESSAGES = 2000
# LM Studio cuts a tool call at about 60 s; stop well before that and say how far we got.
READ_DEADLINE = 40.0


@dataclass(frozen=True)
class ReadResult:
    messages: list[Message]  # merged, key (date, chat_id, id) ascending
    chats_with_messages: int


def _key(msg: Message) -> tuple[datetime, int, int]:
    return (msg.date, msg.chat_id, msg.id)


async def read_chats(
    source: MessageSource,
    chats: Sequence[Chat],
    start: datetime | None,
    end: datetime | None,
    from_me: bool,
    *,
    scope_text: str,
    clock: Callable[[], float] = time.monotonic,
) -> ReadResult:
    """Every message of the period from all `chats`. `scope_text` ('folder "F1"' or '"Family"') only words the
    over-cap error. All or nothing: more than MAX_READ_MESSAGES is an error, never a partial list."""
    started = clock()
    collected: list[Message] = []
    with_messages = 0
    for done, chat in enumerate(chats):
        elapsed = clock() - started
        if elapsed > READ_DEADLINE:
            raise ReadTimeoutError(
                f"Reading took too long: {done} of {len(chats)} chats read in {elapsed:.0f} s. "
                "Pass a shorter period or use a smaller folder."
            )
        remaining = MAX_READ_MESSAGES - len(collected)
        # One more than fits tells us the cap was exceeded without reading further.
        found = await source.get_messages(chat, MessageQuery("forward", start, end, None, None, from_me, remaining + 1))
        if len(found) > remaining:
            where = f"across {len(chats)} chats of {scope_text}" if scope_text.startswith("folder ") else f"in {scope_text}"
            raise TooManyMessagesError(
                f"More than {MAX_READ_MESSAGES} messages in this period {where}. Pass a shorter period."
            )
        with_messages += bool(found)
        collected += found
    collected.sort(key=_key)
    log.info("read %d chats, %d messages in %.1f s", len(chats), len(collected), clock() - started)
    return ReadResult(collected, with_messages)


async def search_chats(
    source: MessageSource,
    chats: Sequence[Chat],
    text: str,
    start: datetime | None,
    end: datetime | None,
    from_me: bool,
    limit: int,
    after: SearchOffset | None,
) -> tuple[list[Message], bool]:
    """At most `limit` hits across `chats`, newest first, and whether older ones remain. `after` continues a
    previous page: hits at or newer than it (by date, chat id, message id) are dropped."""
    started = time.monotonic()
    boundary = None if after is None else (after.date, after.chat_id, after.message_id)
    until = end
    if after is not None:
        # Telegram's dates have second resolution: ask up to the end of the boundary's second so that hits of other
        # chats in that same second are not lost, then drop what was already shown by key.
        until = after.date + timedelta(seconds=1)
        if end is not None:
            until = min(end, until)
    merged: list[Message] = []
    horizon: tuple[datetime, int, int] | None = None
    for chat in chats:
        raw = await source.search_messages(SearchQuery(text, start, until, from_me, limit + 1, None), chat)
        if len(raw) == limit + 1:
            # This chat may hold more hits than it returned, all older than its last one: nothing older than the
            # newest such cut-off can be placed correctly yet.
            oldest = _key(raw[-1])
            horizon = oldest if horizon is None else max(horizon, oldest)
        merged += [msg for msg in raw if boundary is None or _key(msg) < boundary]
    merged.sort(key=_key, reverse=True)
    cut = False
    if horizon is not None:
        kept = [msg for msg in merged if _key(msg) >= horizon]
        cut = len(kept) < len(merged)
        merged = kept
    more = len(merged) > limit or cut
    log.info("searched %d chats: %d hits in %.1f s", len(chats), min(len(merged), limit), time.monotonic() - started)
    return merged[:limit], more
