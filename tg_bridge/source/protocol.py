"""The interface tools depend on; stage 1 has LiveSource, stage 2 adds ArchiveSource."""

from typing import Protocol

from tg_bridge.source.models import Chat, ChatInfo, Person


class MessageSource(Protocol):
    async def whoami(self) -> Person:
        """The account the bridge is logged in as."""
        ...

    async def close(self) -> None:
        """Release connections; safe to call when nothing is open."""
        ...

    async def list_dialogs(self, *, refresh: bool = False) -> list[Chat]:
        """All the user's dialogs (incl. archived) in Telegram order; `refresh` bypasses any cache."""
        ...

    async def get_chat_info(self, chat: Chat) -> ChatInfo:
        """Details of one already-resolved chat."""
        ...
