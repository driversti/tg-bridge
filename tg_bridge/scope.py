"""Which chats a `public`-scope bridge exposes. Pure policy, no I/O.

Fail closed: anything not positively recognised as public or allowlisted is hidden.
"""

from tg_bridge.source.models import Chat


def chat_visible(chat: Chat, allowlist: frozenset[int]) -> bool:
    if chat.kind in ("user", "bot"):  # incl. Saved Messages (a "user" chat); never exposed, whatever the allowlist says
        return False
    if chat.kind in ("channel", "supergroup") and chat.username:
        return True
    if chat.kind in ("group", "channel", "supergroup"):
        return chat.id in allowlist
    return False


def needs_allowlist(chat: Chat) -> bool:
    """The chats the admin page lists: private groups and channels, the only ones the owner can switch on."""
    return chat.kind == "group" or (chat.kind in ("channel", "supergroup") and not chat.username)
