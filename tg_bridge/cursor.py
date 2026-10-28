"""Paging token the model copies back verbatim.

Short and readable on purpose (`f6782037_s1792969200_c795`): ~30B models garbled long base64 tokens, splicing
pieces of an earlier cursor into a new one. One letter per field, digits only:
`f`/`b` direction + boundary id, `s`/`e` period start/end (epoch seconds), `t` topic id, `m` from_me,
`c` the chat id's last three digits, so a cursor pasted into the wrong chat is caught and named.
"""

import re
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tg_bridge.errors import CursorError
from tg_bridge.resolve import normalize
from tg_bridge.source import SearchOffset

_HEAD = re.compile(r"([fb])(\d+)")
_FIELD = re.compile(r"([setc])(\d+)|m")


@dataclass(frozen=True)
class Cursor:
    chat_id: int
    direction: Literal["forward", "backward"]
    boundary_id: int  # forward: continue with ids > it; backward: ids < it
    start: datetime | None
    end: datetime | None
    topic_id: int | None
    from_me: bool


def encode_cursor(c: Cursor) -> str:
    parts = [f"{c.direction[0]}{c.boundary_id}"]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    if c.topic_id is not None:
        parts.append(f"t{c.topic_id}")
    if c.from_me:
        parts.append("m")
    parts.append(f"c{_tag(c.chat_id)}")
    return "_".join(parts)


def decode_cursor(s: str, chat_id: int) -> Cursor:
    """Parse a token for `chat_id`; CursorError if it is malformed or was issued for another chat."""
    head, *rest = s.strip().strip("\"'`").strip().split("_")
    if not (m := _HEAD.fullmatch(head)):
        raise CursorError()
    fields: dict[str, str] = {}
    for part in rest:
        f = _FIELD.fullmatch(part)
        key = "m" if f and f.group(1) is None else (f.group(1) if f else None)
        if key is None or key in fields:
            raise CursorError()
        fields[key] = f.group(2) or ""
    tag = fields.get("c")
    if tag is None or len(tag) != 3:
        raise CursorError()
    if tag != _tag(chat_id):
        raise CursorError(other_chat=True)
    return Cursor(
        chat_id=chat_id,
        direction="forward" if m.group(1) == "f" else "backward",
        boundary_id=int(m.group(2)),
        start=_moment(fields.get("s")),
        end=_moment(fields.get("e")),
        topic_id=int(fields["t"]) if "t" in fields else None,
        from_me="m" in fields,
    )


def _tag(chat_id: int) -> str:
    return f"{abs(chat_id) % 1000:03d}"


def _moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value), UTC)


# Search cursor: `{h|g}{message_id}_d{epoch}_{u|n}{abs(chat_id)}[_s{epoch}][_e{epoch}][_m]_q{tag}`.
# `h` per-chat, `g` global; `d` the boundary hit's date (SearchGlobal pages by it); `u`/`n` positive/negative chat id;
# `q` a 3-digit tag of the query, so a cursor re-sent with other words is caught.
_SEARCH_HEAD = re.compile(r"([hg])(\d+)")
_SEARCH_FIELD = re.compile(r"([dunseq])(\d+)|m")


@dataclass(frozen=True)
class SearchCursor:
    scope: Literal["chat", "global"]
    after: SearchOffset  # continue with hits older than this one
    start: datetime | None
    end: datetime | None
    from_me: bool


def query_tag(query: str) -> str:
    return f"{zlib.crc32(normalize(query).encode()) % 1000:03d}"


def encode_search_cursor(c: SearchCursor, query: str) -> str:
    parts = [
        f"{'h' if c.scope == 'chat' else 'g'}{c.after.message_id}",
        f"d{int(c.after.date.timestamp())}",
        f"{'n' if c.after.chat_id < 0 else 'u'}{abs(c.after.chat_id)}",
    ]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    if c.from_me:
        parts.append("m")
    parts.append(f"q{query_tag(query)}")
    return "_".join(parts)


def decode_search_cursor(s: str, query: str) -> SearchCursor:
    """Parse a search token; CursorError if malformed or issued for another query."""
    invalid = CursorError(tool="search_messages")
    head, *rest = s.strip().strip("\"'`").strip().split("_")
    if not (m := _SEARCH_HEAD.fullmatch(head)):
        raise invalid
    fields: dict[str, str] = {}
    for part in rest:
        f = _SEARCH_FIELD.fullmatch(part)
        key = "m" if f and f.group(1) is None else (f.group(1) if f else None)
        if key is None or key in fields:
            raise invalid
        fields[key] = f.group(2) or ""
    if "d" not in fields or ("u" in fields) == ("n" in fields):
        raise invalid
    tag = fields.get("q")
    if tag is None or len(tag) != 3:
        raise invalid
    if tag != query_tag(query):
        raise CursorError(other_query=True, tool="search_messages")
    chat_id = int(fields["u"]) if "u" in fields else -int(fields["n"])
    return SearchCursor(
        scope="chat" if m.group(1) == "h" else "global",
        after=SearchOffset(chat_id=chat_id, message_id=int(m.group(2)), date=_moment(fields["d"])),
        start=_moment(fields.get("s")),
        end=_moment(fields.get("e")),
        from_me="m" in fields,
    )
