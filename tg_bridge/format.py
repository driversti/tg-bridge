"""Compact plain-text rendering for tool output (design §6). All times are shown in the configured zone."""

from datetime import datetime
from zoneinfo import ZoneInfo

from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError
from tg_bridge.source import Chat, ChatInfo, Person

TRUNCATED = "(output truncated: narrow with query/kind)"
MAX_CANDIDATES = 10
SEP = " · "


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
