"""Rendering of multi-chat pages: folder reads and headline digests. Same budget strategy as `render_page`."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from tg_bridge.format import (
    FOOTER_RESERVE,
    SEP,
    WEEKDAYS,
    _clamp,
    _message_line,
    chat_link_base,
    one_line,
)
from tg_bridge.headlines import Repost, headline
from tg_bridge.source import Chat, Message

LABEL_CHARS = 20

Item = Message | Repost  # full mode: Message, headlines: Repost


def chat_labels(chats: Sequence[Chat]) -> dict[int, str]:
    """Short unique names for the lines; two chats with the same name get the last digits of their ids."""
    labels = {c.id: _cut(one_line(c.title)) for c in chats}
    counts: dict[str, int] = {}
    for label in labels.values():
        counts[label] = counts.get(label, 0) + 1
    return {cid: f"{label} ~{abs(cid) % 1000:03d}" if counts[label] > 1 else label for cid, label in labels.items()}


def _cut(title: str) -> str:
    return title if len(title) <= LABEL_CHARS else title[: LABEL_CHARS - 1] + "…"


def legend_lines(chat_ids: Iterable[int], chats: Mapping[int, Chat], labels: Mapping[int, str]) -> list[str]:
    """`label = id · Links: …` per chat: how the model gets ids for around/download_media and links for answers."""
    lines = []
    for cid in chat_ids:
        line = f"{labels.get(cid, str(cid))} = {cid}"
        chat = chats.get(cid)
        base = chat_link_base(chat) if chat is not None else None
        lines.append(f"{line}{SEP}Links: {base}/<id>" if base else line)
    return lines


def render_many(
    head: str,
    totals: Callable[[int, int], str],
    items: Sequence[Item],
    *,
    offset: int,
    chats: Mapping[int, Chat],
    labels: Mapping[int, str] | None,
    tz: ZoneInfo,
    max_chars: int,
    limit: int,
    next_hint: Callable[[Message], str],
    empty_text: str,
) -> str:
    """Head + totals + legend + day-grouped lines + footer, always <= max_chars. `items` are everything after the
    cursor, chronological; `offset` is how many came before (for "showing a–b"). `labels=None` = one chat."""
    if not items:
        return _clamp(f"{head}\n{empty_text}", max_chars)
    total = len(items)
    for count in range(min(limit, total), 0, -1):
        text = _page_text(head, totals, items[:count], offset, chats, labels, tz, count < total, next_hint)
        if len(text) <= max_chars:
            return text
    # Not even one fits: cut that item's text.
    text_limit = max(max_chars - len(head) - FOOTER_RESERVE, 0)
    while True:
        text = _page_text(head, totals, items[:1], offset, chats, labels, tz, True, next_hint, text_limit)
        if len(text) <= max_chars or text_limit == 0:
            return _clamp(text, max_chars)
        text_limit = max(0, text_limit - max(1, len(text) - max_chars))


def _page_text(
    head: str,
    totals: Callable[[int, int], str],
    shown: Sequence[Item],
    offset: int,
    chats: Mapping[int, Chat],
    labels: Mapping[int, str] | None,
    tz: ZoneInfo,
    unfinished: bool,
    next_hint: Callable[[Message], str],
    text_limit: int | None = None,
) -> str:
    lines = [head, totals(offset + 1, offset + len(shown))]
    if labels is not None:
        on_page = dict.fromkeys(m.chat_id for item in shown for m in _posts(item))
        lines += legend_lines(on_page, chats, labels)
    seen_people: set[int] = set()
    day = None
    for item in shown:
        local = item.first.date.astimezone(tz) if isinstance(item, Repost) else item.date.astimezone(tz)
        if local.date() != day:
            day = local.date()
            lines.append(f"— {day.isoformat()} {WEEKDAYS[day.weekday()]} —")
        if isinstance(item, Repost):
            lines.append(_headline_line(item, local, chats, labels, text_limit))
        else:
            label = labels.get(item.chat_id, str(item.chat_id)) if labels is not None else None
            lines.append(_message_line(item, f"{local:%H:%M}", seen_people, text_limit, label=label))
    last = shown[-1]
    lines.append(f"More remain. Next: {next_hint(_boundary(last))}" if unfinished else "End of period.")
    return "\n".join(lines)


def _posts(item: Item) -> tuple[Message, ...]:
    return item.posts if isinstance(item, Repost) else (item,)


def _boundary(item: Item) -> Message:
    return item.first if isinstance(item, Repost) else item


def _headline_line(
    group: Repost, local: datetime, chats: Mapping[int, Chat], labels: Mapping[int, str] | None, text_limit: int | None
) -> str:
    text = headline(group.first.text) or ""
    if text_limit is not None and len(text) > text_limit:
        text = text[: max(text_limit - 1, 0)] + "…"

    def ref(msg: Message) -> str:
        # A full link per post: the model did not fill the legend's `Links: …/<id>` in itself (smoke 08).
        chat = chats.get(msg.chat_id)
        base = chat_link_base(chat) if chat is not None else None
        return f"{base}/{msg.id}" if base else f"#{msg.id}"

    refs = " ".join(ref(m) for m in group.posts)
    if labels is None:
        return f"{local:%H:%M} {text} {refs}"
    who = ", ".join(labels.get(m.chat_id, str(m.chat_id)) for m in group.posts)
    prefix = f"×{len(group.posts)} " if len(group.posts) > 1 else ""
    return f"{local:%H:%M} {prefix}{who}: {text} {refs}"
