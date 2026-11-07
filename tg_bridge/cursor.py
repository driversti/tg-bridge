"""Paging token the model copies back verbatim.

Short and readable on purpose (`f6782037_s1792969200_c795`): ~30B models garbled long base64 tokens, splicing
pieces of an earlier cursor into a new one. One letter per field, digits only:
`f`/`b` direction + boundary id, `s`/`e` period start/end (epoch seconds), `t` topic id, `p` channel post id (a comment thread; never together with `t`), `m` from_me,
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
_FIELD = re.compile(r"([setcp])(\d+)|m")


@dataclass(frozen=True)
class Cursor:
    chat_id: int
    direction: Literal["forward", "backward"]
    boundary_id: int  # forward: continue with ids > it; backward: ids < it
    start: datetime | None
    end: datetime | None
    topic_id: int | None
    from_me: bool
    post_id: int | None = None  # set: this cursor pages the comments of that channel post


def encode_cursor(c: Cursor) -> str:
    parts = [f"{c.direction[0]}{c.boundary_id}"]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    if c.topic_id is not None:
        parts.append(f"t{c.topic_id}")
    if c.post_id is not None:
        parts.append(f"p{c.post_id}")
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
    if "t" in fields and "p" in fields:
        raise CursorError()
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
        post_id=int(fields["p"]) if "p" in fields else None,
    )


def _tag(chat_id: int) -> str:
    return f"{abs(chat_id) % 1000:03d}"


def _moment(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(int(value), UTC)


# Search cursor: `{h|g|o}{message_id}_d{epoch}_{u|n}{abs(chat_id)}[_s{epoch}][_e{epoch}][_k{folder_id}][_m]_q{tag}`.
# `h` per-chat, `g` global, `o` folder (then `k` names the folder, and only then); `d` the boundary hit's date (SearchGlobal pages by it); `u`/`n` positive/negative chat id;
# `q` a 3-digit tag of the query, so a cursor re-sent with other words is caught.
_SEARCH_HEAD = re.compile(r"([hgo])(\d+)")
_SEARCH_FIELD = re.compile(r"([dunseqk])(\d+)|m")
_SEARCH_SCOPES = {"h": "chat", "g": "global", "o": "folder"}
_SCOPE_HEADS = {scope: head for head, scope in _SEARCH_SCOPES.items()}


@dataclass(frozen=True)
class SearchCursor:
    scope: Literal["chat", "global", "folder"]
    after: SearchOffset  # continue with hits older than this one
    start: datetime | None
    end: datetime | None
    from_me: bool
    folder_id: int | None = None  # set exactly when scope == "folder"


def query_tag(query: str) -> str:
    return f"{zlib.crc32(normalize(query).encode()) % 1000:03d}"


def encode_search_cursor(c: SearchCursor, query: str) -> str:
    parts = [
        f"{_SCOPE_HEADS[c.scope]}{c.after.message_id}",
        f"d{int(c.after.date.timestamp())}",
        f"{'n' if c.after.chat_id < 0 else 'u'}{abs(c.after.chat_id)}",
    ]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    if c.folder_id is not None:
        parts.append(f"k{c.folder_id}")
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
    if (m.group(1) == "o") != ("k" in fields):
        raise invalid
    tag = fields.get("q")
    if tag is None or len(tag) != 3:
        raise invalid
    if tag != query_tag(query):
        raise CursorError(other_query=True, tool="search_messages")
    chat_id = int(fields["u"]) if "u" in fields else -int(fields["n"])
    return SearchCursor(
        scope=_SEARCH_SCOPES[m.group(1)],  # type: ignore[arg-type]
        after=SearchOffset(chat_id=chat_id, message_id=int(m.group(2)), date=_moment(fields["d"])),
        start=_moment(fields.get("s")),
        end=_moment(fields.get("e")),
        from_me="m" in fields,
        folder_id=int(fields["k"]) if "k" in fields else None,
    )


# Read cursor: `r{message_id}_d{epoch}_{u|n}{abs(chat_id)}[_s{epoch}][_e{epoch}][_k{folder_id}][_h][_m]`.
# Pages a merged multi-chat read (folder reads, headlines). The first three fields are the last shown item (for a repost
# group: its first post), `s`/`e` the period, `k` the folder (absent = one chat, the boundary's), `h` headlines mode.
# Every page re-reads the whole period, so the cursor only says where the previous page stopped.
_READ_HEAD = re.compile(r"r(\d+)")
_READ_FIELD = re.compile(r"([dunsek])(\d+)|[hm]")
_READ_START = re.compile(r"r\d")


@dataclass(frozen=True)
class ReadCursor:
    after: SearchOffset  # boundary: the last shown item (chat_id, message_id, date)
    start: datetime | None
    end: datetime | None
    folder_id: int | None  # None = single chat, after.chat_id
    headlines: bool
    from_me: bool


def is_read_cursor(s: str) -> bool:
    return _READ_START.match(s.strip().strip("\"'`").strip()) is not None


def encode_read_cursor(c: ReadCursor) -> str:
    parts = [
        f"r{c.after.message_id}",
        f"d{int(c.after.date.timestamp())}",
        f"{'n' if c.after.chat_id < 0 else 'u'}{abs(c.after.chat_id)}",
    ]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    if c.folder_id is not None:
        parts.append(f"k{c.folder_id}")
    if c.headlines:
        parts.append("h")
    if c.from_me:
        parts.append("m")
    return "_".join(parts)


def decode_read_cursor(s: str) -> ReadCursor:
    """Parse a read token; CursorError if it is malformed."""
    invalid = CursorError()
    head, *rest = s.strip().strip("\"'`").strip().split("_")
    if not (m := _READ_HEAD.fullmatch(head)):
        raise invalid
    fields: dict[str, str] = {}
    for part in rest:
        f = _READ_FIELD.fullmatch(part)
        key = part if f and f.group(1) is None else (f.group(1) if f else None)
        if key is None or key in fields:
            raise invalid
        fields[key] = f.group(2) or ""
    if "d" not in fields or ("u" in fields) == ("n" in fields):
        raise invalid
    chat_id = int(fields["u"]) if "u" in fields else -int(fields["n"])
    return ReadCursor(
        after=SearchOffset(chat_id=chat_id, message_id=int(m.group(1)), date=_moment(fields["d"])),
        start=_moment(fields.get("s")),
        end=_moment(fields.get("e")),
        folder_id=int(fields["k"]) if "k" in fields else None,
        headlines="h" in fields,
        from_me="m" in fields,
    )


# Export cursor: `x{boundary_id}_f{epoch}[_n{n}]_k{count}_a{epoch}_d{epoch}[_s{epoch}][_e{epoch}]_c{tag}`.
# One export call has a time limit; the cursor continues it into the same file. `f`/`n` name that file (its creation
# time and name suffix), `k`/`a`/`d` carry the count and first/last dates so far for the footer and day headings.
_EXPORT_HEAD = re.compile(r"x(\d+)")
_EXPORT_FIELD = re.compile(r"([fnkadsec])(\d+)")


@dataclass(frozen=True)
class ExportCursor:
    chat_id: int
    file_stamp: datetime  # when the file was created, to the second; part of its name
    file_n: int  # the file name's suffix, 1 = none
    boundary_id: int  # continue with ids > it
    start: datetime | None
    end: datetime | None
    count: int  # messages written so far
    first: datetime
    last: datetime


def encode_export_cursor(c: ExportCursor) -> str:
    parts = [f"x{c.boundary_id}", f"f{int(c.file_stamp.timestamp())}"]
    if c.file_n > 1:
        parts.append(f"n{c.file_n}")
    parts += [f"k{c.count}", f"a{int(c.first.timestamp())}", f"d{int(c.last.timestamp())}"]
    if c.start is not None:
        parts.append(f"s{int(c.start.timestamp())}")
    if c.end is not None:
        parts.append(f"e{int(c.end.timestamp())}")
    parts.append(f"c{_tag(c.chat_id)}")
    return "_".join(parts)


def decode_export_cursor(s: str, chat_id: int) -> ExportCursor:
    """Parse an export token for `chat_id`; CursorError if it is malformed or was issued for another chat."""
    invalid = CursorError(tool="export_chat")
    head, *rest = s.strip().strip("\"'`").strip().split("_")
    if not (m := _EXPORT_HEAD.fullmatch(head)):
        raise invalid
    fields: dict[str, str] = {}
    for part in rest:
        f = _EXPORT_FIELD.fullmatch(part)
        if f is None or f.group(1) in fields:
            raise invalid
        fields[f.group(1)] = f.group(2)
    if any(key not in fields for key in "fkadc") or len(fields["c"]) != 3:
        raise invalid
    if fields["c"] != _tag(chat_id):
        raise CursorError(other_chat=True, tool="export_chat")
    return ExportCursor(
        chat_id=chat_id,
        file_stamp=_moment(fields["f"]),
        file_n=int(fields.get("n", "1")),
        boundary_id=int(m.group(1)),
        start=_moment(fields.get("s")),
        end=_moment(fields.get("e")),
        count=int(fields["k"]),
        first=_moment(fields["a"]),
        last=_moment(fields["d"]),
    )
