"""Which chats a `public`-scope bridge exposes. Pure policy, no I/O.

Rule: public channels and supergroups are always visible; every other chat (people, bots, Saved Messages, private
groups and channels) only when the owner switched it on. Fail closed: anything not positively recognised is hidden.
"""

from tg_bridge.source.models import Chat

_KINDS = ("user", "bot", "group", "channel", "supergroup")


def is_public(chat: Chat) -> bool:
    """Only a channel or supergroup with its own @username; a person's @username never makes a 1:1 chat public."""
    return chat.kind in ("channel", "supergroup") and bool(chat.username)


def chat_visible(chat: Chat, allowlist: frozenset[int]) -> bool:
    if is_public(chat):
        return True
    if chat.kind in _KINDS:
        return chat.id in allowlist
    return False  # unknown kind: not even the allowlist opens it


def needs_allowlist(chat: Chat) -> bool:
    """The chats the admin page lists: everything private that the owner can switch on."""
    return not is_public(chat) and chat.kind in _KINDS
