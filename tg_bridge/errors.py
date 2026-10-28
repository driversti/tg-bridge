"""Bridge-level errors. Messages are shown to the user/model, so they never contain secrets or PII."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tg_bridge.source import Chat, Folder


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


class FolderNotFoundError(TgBridgeError):
    """No folder matches the argument. Carries all folders (there are few); the tools layer words the message."""

    def __init__(self, arg: str, folders: list["Folder"]):
        super().__init__("folder not found")  # no titles: safe if ever logged
        self.arg = arg
        self.folders = folders


class AmbiguousFolderError(TgBridgeError):
    """More than one folder matches; never guess."""

    def __init__(self, arg: str, candidates: list["Folder"]):
        super().__init__("folder is ambiguous")
        self.arg = arg
        self.candidates = candidates


class PeriodError(TgBridgeError):
    """The period arguments of a read tool are invalid. The message tells the model how to fix them."""


class CursorError(TgBridgeError):
    """The paging cursor is garbled, from another version, or otherwise unusable."""

    def __init__(
        self, other_chat: bool = False, other_query: bool = False, tool: str = "get_messages", other_folder: bool = False
    ) -> None:
        # Models mix up cursors between chats and queries; naming the mistake lets them fix it instead of starting over.
        if other_chat:
            super().__init__(
                f"This cursor belongs to another chat. Use the cursor from this chat's last result, "
                f"or call {tool} again without cursor."
            )
        elif other_folder:
            super().__init__(
                f"This cursor belongs to another folder. Use the cursor from this folder's last result, "
                f"or call {tool} again without cursor."
            )
        elif other_query:
            super().__init__(
                f"This cursor belongs to another query. Use the cursor from this query's last result, "
                f"or call {tool} again without cursor."
            )
        else:
            super().__init__(f"Invalid cursor. Call {tool} again without cursor.")
        self.other_chat = other_chat
        self.other_query = other_query
        self.other_folder = other_folder


class TooManyMessagesError(TgBridgeError):
    """A multi-chat read found more messages than one call may hold; the message tells the model to narrow the period."""


class ReadTimeoutError(TgBridgeError):
    """A multi-chat read ran out of time before every chat was read."""


class ExportFileError(TgBridgeError):
    """An export cursor points at a file that is gone or already finished."""


class MessageNotFoundError(TgBridgeError):
    def __init__(self, message_id: int):
        super().__init__(f"Message #{message_id} not found in this chat.")
        self.message_id = message_id


class MediaError(TgBridgeError):
    """A download_media request that cannot be served; the message tells the model why."""
