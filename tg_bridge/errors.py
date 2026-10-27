"""Bridge-level errors. Messages are shown to the user/model, so they never contain secrets or PII."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tg_bridge.source import Chat


class TgBridgeError(Exception):
    """Base class for anticipated failures."""


class ConfigError(TgBridgeError):
    """Settings are missing or invalid."""


class NotLoggedInError(TgBridgeError):
    """The session file is missing or not authorized."""


class RateLimitedError(TgBridgeError):
    """Telegram asked us to wait (FloodWait)."""

    def __init__(self, seconds: int):
        super().__init__(f"Telegram rate limit: retry in {seconds} s")
        self.seconds = seconds


class LoginError(TgBridgeError):
    """Telegram refused the interactive login (bad phone number, banned number, …)."""


class ChatNotFoundError(TgBridgeError):
    """No dialog matches the chat argument. Carries chats; the tools layer words the message."""

    def __init__(self, arg: str, suggestions: list["Chat"]):
        super().__init__("chat not found")  # no names: safe if ever logged
        self.arg = arg
        self.suggestions = suggestions


class AmbiguousChatError(TgBridgeError):
    """More than one dialog matches; never guess."""

    def __init__(self, arg: str, candidates: list["Chat"]):
        super().__init__("chat is ambiguous")
        self.arg = arg
        self.candidates = candidates


class PeriodError(TgBridgeError):
    """The period arguments of a read tool are invalid. The message tells the model how to fix them."""


class CursorError(TgBridgeError):
    """The paging cursor is garbled, from another version, or otherwise unusable."""

    def __init__(self, other_chat: bool = False) -> None:
        if other_chat:
            # Models mix up cursors between chats; naming the mistake lets them fix it instead of starting over.
            super().__init__(
                "This cursor belongs to another chat. Use the cursor from this chat's last result, "
                "or call get_messages again without cursor."
            )
        else:
            super().__init__("Invalid cursor. Call get_messages again without cursor.")
        self.other_chat = other_chat
