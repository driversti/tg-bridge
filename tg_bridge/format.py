"""Compact plain-text rendering for tool output (design §6). All times are shown in the configured zone."""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError
from tg_bridge.source import Chat, ChatInfo, MediaInfo, Message, Person, Topic

TRUNCATED = "(output truncated: narrow with query/kind)"
MAX_CANDIDATES = 10
SEP = " · "
# Free space kept for the "Shown …" footer when a single message has to be cut to fit.
FOOTER_RESERVE = 300
MAX_TEXT_CHARS = 1500
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # not strftime: that follows the process locale


def _when(moment: datetime, tz: ZoneInfo) -> str:
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def chat_line(chat: Chat, tz: ZoneInfo) -> str:
    parts = [str(chat.id), chat.kind, chat.title]
    if chat.username:
        parts.append(f"@{chat.username}")
    if chat.deleted_peer:
        parts.append("deleted_peer")
    if chat.last_message_at:
        parts.append(f"last {_when(chat.last_message_at, tz)}")
    if chat.is_forum:
        parts.append("forum")
    if chat.is_archived:
        parts.append("archived")
    return SEP.join(parts)


def render_lines(header: str, lines: list[str], max_chars: int) -> str:
    """Header plus lines; drops trailing lines (and says so) rather than exceed max_chars."""
    full = "\n".join([header, *lines])
    if len(full) <= max_chars:
        return full
    out = header
    for line in lines:
        if len(out) + 1 + len(line) + 1 + len(TRUNCATED) > max_chars:
            break
        out += "\n" + line
    return f"{out}\n{TRUNCATED}"


def chat_info_block(info: ChatInfo, tz: ZoneInfo) -> str:
    chat = info.chat
    flags = [chat.kind]
    if chat.is_forum:
        flags.append("forum")
    if chat.is_archived:
        flags.append("archived")
    ident = [f"id {chat.id}"]
    if chat.username:
        ident.append(f"@{chat.username}")
    if chat.last_message_at:
        ident.append(f"last message {_when(chat.last_message_at, tz)}")
    lines = [f"{chat.title} — {', '.join(flags)}", SEP.join(ident)]

    if chat.peer is not None:
        lines.append(_peer_line(chat.peer))
    else:
        lines.append(f"Members: {info.members_count if info.members_count is not None else 'unknown'}")
    if info.description:
        lines.append(f"Description: {info.description}")
    if info.topics:
        shown = SEP.join(f"#{t.id} {t.title}{' (closed)' if t.is_closed else ''}" for t in info.topics)
        count = f"{len(info.topics)} of {info.topics_total}" if info.topics_total else str(len(info.topics))
        lines.append(f"Topics ({count}): {shown}")
    return "\n".join(lines)


def _peer_line(peer: Person) -> str:
    parts = [peer.display_name]
    if peer.username:
        parts.append(f"@{peer.username}")
    parts.append(f"id {peer.id}")
    if peer.phone:
        parts.append(f"phone {peer.phone}")
    if peer.is_bot:
        parts.append("bot")
    if peer.is_deleted:
        parts.append("deleted")
    return "Peer: " + SEP.join(parts)


def _candidate_lines(chats: list[Chat], tz: ZoneInfo) -> list[str]:
    lines = [chat_line(c, tz) for c in chats[:MAX_CANDIDATES]]
    if len(chats) > MAX_CANDIDATES:
        lines.append(f"…and {len(chats) - MAX_CANDIDATES} more; use the id")
    return lines


def ambiguous_text(e: AmbiguousChatError, tz: ZoneInfo, max_chars: int) -> str:
    header = f'Chat "{e.arg}" is ambiguous; pass one of these ids:'
    return render_lines(header, _candidate_lines(e.candidates, tz), max_chars)


def not_found_text(e: ChatNotFoundError, tz: ZoneInfo, max_chars: int) -> str:
    if not e.suggestions:
        return f'No chat matches "{e.arg}". Use list_chats with a query to search for it.'
    return render_lines(f'No chat matches "{e.arg}". Closest:', _candidate_lines(e.suggestions, tz), max_chars)


# --- messages --------------------------------------------------------------


def chat_link_base(chat: Chat) -> str | None:
    """Prefix for t.me message links, None when the chat has none (people, bots, basic groups)."""
    if chat.kind not in ("supergroup", "channel"):
        return None
    if chat.username:
        return f"https://t.me/{chat.username}"
    digits = str(-chat.id)
    if chat.id >= 0 or not digits.startswith("100"):
        return None
    return f"https://t.me/c/{int(digits[3:])}"  # -100N → N


def messages_head(
    chat: Chat, period_text: str, tz: ZoneInfo, topic: Topic | None = None, from_me: bool = False
) -> str:
    lines = [f"Chat: {chat.title} ({chat.id}){SEP}{period_text} ({tz.key})"]
    flags = []
    if topic is not None:
        flags.append(f"Topic: #{topic.id} {topic.title}".rstrip())
    if from_me:
        flags.append("from me only" if topic is not None else "From me only")
    if flags:
        lines.append(SEP.join(flags))
    base = chat_link_base(chat)
    if base:
        lines.append(f"Links: {base}/<id>")
    return "\n".join(lines)


def render_page(
    head: str,
    messages: Sequence[Message],
    *,
    tz: ZoneInfo,
    max_chars: int,
    keep: Literal["oldest", "newest"],
    more: bool,
    next_hint: Callable[[Message], str],
    end_text: str,
    empty_text: str,
) -> str:
    """Head + day-grouped lines + footer, always <= max_chars. `messages` are chronological."""
    if not messages:
        return _clamp(f"{head}\n{empty_text}", max_chars)
    total = len(messages)
    for count in range(total, 0, -1):
        shown = messages[:count] if keep == "oldest" else messages[total - count :]
        text = _page_text(head, shown, tz, more or count < total, keep, next_hint, end_text)
        if len(text) <= max_chars:
            return text
    # Not even one fits: cut that message's text. Newest/oldest as the mode dictates.
    only = messages[0] if keep == "oldest" else messages[-1]
    budget = max_chars - len(head) - FOOTER_RESERVE
    text_limit = max(budget, 0)
    while True:
        text = _page_text(head, [only], tz, True, keep, next_hint, end_text, text_limit)
        if len(text) <= max_chars or text_limit == 0:
            return _clamp(text, max_chars)
        text_limit = max(0, text_limit - max(1, len(text) - max_chars))


def _clamp(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max(max_chars - 1, 0)] + "…"


def _page_text(
    head: str,
    shown: Sequence[Message],
    tz: ZoneInfo,
    unfinished: bool,
    keep: str,
    next_hint: Callable[[Message], str],
    end_text: str,
    text_limit: int | None = None,
) -> str:
    lines = [head]
    seen_people: set[int] = set()
    day = None
    for msg in shown:
        local = msg.date.astimezone(tz)
        if local.date() != day:
            day = local.date()
            lines.append(f"— {day.isoformat()} {WEEKDAYS[day.weekday()]} —")
        lines.append(_message_line(msg, local, seen_people, text_limit))
    first, last = shown[0].date, shown[-1].date
    noun = "message" if len(shown) == 1 else "messages"
    tail = next_hint(shown[-1] if keep == "oldest" else shown[0]) if unfinished else end_text
    lines.append(f"Shown {len(shown)} {noun} ({_when(first, tz)} → {_when(last, tz)}). {tail}")
    return "\n".join(lines)


def _message_line(msg: Message, local: datetime, seen_people: set[int], text_limit: int | None) -> str:
    parts = []
    if msg.forward_from:
        parts.append(f"⤷fwd {msg.forward_from}:")
    body = f"[{msg.action}]" if msg.action else _text(msg.text, text_limit)
    if body:
        parts.append(body)
    if msg.media is not None:
        parts.append(_media_text(msg.media))
    if msg.reply_to_id is not None:
        parts.append(f"↩#{msg.reply_to_id}")
    if msg.edit_date is not None:
        parts.append("✎")
    return f"#{msg.id} {local:%H:%M} {_sender(msg, seen_people)}: {' '.join(parts)}"


def _sender(msg: Message, seen_people: set[int]) -> str:
    if msg.out:
        return "Me"
    if msg.sender is not None:
        name = msg.sender.display_name
        first_time = msg.sender.id not in seen_people
        seen_people.add(msg.sender.id)
        return f"{name} (@{msg.sender.username})" if first_time and msg.sender.username else name
    return msg.sender_title or "?"


def _text(text: str, limit: int | None) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > MAX_TEXT_CHARS:
        text = f"{text[:MAX_TEXT_CHARS]}…(+{len(text) - MAX_TEXT_CHARS} chars)"
    text = text.replace("\n", " ⏎ ")
    if limit is not None and len(text) > limit:
        text = text[: max(limit - 1, 0)] + "…"
    return text


def _media_text(media: MediaInfo) -> str:
    kind = media.kind
    if kind == "poll":
        return f"[poll: {' '.join((media.label or '').split())}]"
    if kind in ("gif", "location", "contact"):
        return f"[{kind}]"
    if kind == "other":
        return "[media]"
    details = [kind]
    if kind == "sticker":
        details.append(media.label or "")
    if kind in ("audio", "document"):
        details.append(media.file_name or "")
    if kind in ("video", "voice", "video_note") and media.duration is not None:
        details.append(_duration(media.duration))
    if kind in ("photo", "video", "audio", "document") and media.size is not None:
        details.append(_size(media.size))
    return f"[{' '.join(d for d in details if d)}]"


def _size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size // 1024}KB"
    return f"{size / (1024 * 1024):.1f}MB"


def _duration(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"
