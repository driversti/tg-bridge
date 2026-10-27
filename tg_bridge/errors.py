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
