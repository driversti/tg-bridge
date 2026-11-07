"""The interface tools depend on; stage 1 has LiveSource, stage 2 adds ArchiveSource."""

from collections.abc import AsyncIterator
from typing import Protocol

from tg_bridge.source.models import Chat, ChatInfo, Folder, MediaFile, Message, MessageQuery, Person, Post, SearchQuery


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

    async def list_folders(self, *, refresh: bool = False) -> list[Folder]:
        """The user's chat folders in app order (no "All Chats"), from the same cache as list_dialogs."""
        ...

    async def get_chat_info(self, chat: Chat) -> ChatInfo:
        """Details of one already-resolved chat."""
        ...

    async def get_messages(self, chat: Chat, query: MessageQuery) -> list[Message]:
        """At most `query.limit` messages of one resolved chat, in the query's direction (forward = ascending ids)."""
        ...

    async def get_post(self, chat: Chat, post_id: int) -> Post | None:
        """A channel post with its comment-thread info; None for an unknown id or a service message."""
        ...

    async def get_comments(self, chat: Chat, post: Post, query: MessageQuery) -> list[Message]:
        """At most `query.limit` comments of `post`, oldest first (forward only), period and id boundary applied.
        Comments carry the discussion group's chat id. Raises TelegramRefusedError when Telegram refuses."""
        ...

    async def search_messages(self, query: SearchQuery, chat: Chat | None) -> list[Message]:
        """At most `query.limit` text hits, newest first (Message.chat_id is the hit's chat); `chat=None` searches all
        the user's dialogs."""
        ...

    async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
        """The message's media as one file, or every file of its album ordered by id; [] when it has no media.
        Raises MessageNotFoundError for an unknown id."""
        ...

    def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
        """That message's file content from byte `offset` to the end, in chunks."""
        ...
