"""Paging token the model copies back verbatim.

Short and readable on purpose (`f6782037_s1792969200_c795`): ~30B models garbled long base64 tokens, splicing
pieces of an earlier cursor into a new one. One letter per field, digits only:
`f`/`b` direction + boundary id, `s`/`e` period start/end (epoch seconds), `t` topic id, `m` from_me,
`c` the chat id's last three digits, so a cursor pasted into the wrong chat is caught and named.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from tg_bridge.errors import CursorError

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
