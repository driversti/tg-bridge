"""MessageSource wrapper for `public` scope: hidden chats do not exist for anything above it.

Every tool resolves chats from `list_dialogs`, so filtering that list hides them from resolution, listings and folder
counts. The chat-taking methods re-check anyway (cursors carry chat ids), and global search needs its own filtering.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import replace

from tg_bridge.errors import ChatNotFoundError
from tg_bridge.scope import chat_visible
from tg_bridge.source.allowlist import Allowlist
from tg_bridge.source.models import Chat, ChatInfo, Folder, MediaFile, Message, MessageQuery, Person, Post, SearchOffset, SearchQuery
from tg_bridge.source.protocol import MessageSource

log = logging.getLogger(__name__)

# Extra raw pages a global search may fetch to top up a page that hidden hits ate into.
MAX_TOPUP_ROUNDS = 5


class ScopedSource:
    def __init__(self, inner: MessageSource, allowlist: Allowlist):
        self._inner = inner
        self._allowlist = allowlist

    async def whoami(self) -> Person:
        return await self._inner.whoami()

    async def close(self) -> None:
        await self._inner.close()

    async def list_dialogs(self, *, refresh: bool = False) -> list[Chat]:
        dialogs = await self._inner.list_dialogs(refresh=refresh)
        allowed = self._allowlist.load()
        return [c for c in dialogs if chat_visible(c, allowed)]

    async def list_folders(self, *, refresh: bool = False) -> list[Folder]:
        return await self._inner.list_folders(refresh=refresh)

    def dialogs_age(self) -> float | None:
        age = getattr(self._inner, "dialogs_age", None)
        return age() if callable(age) else None

    async def _require_visible(self, chat: Chat) -> None:
        # The id decides, never the passed object: its kind/username could be stale or forged.
        if chat.id not in {c.id for c in await self.list_dialogs()}:
            raise ChatNotFoundError(str(chat.id), suggestions=[])

    async def get_chat_info(self, chat: Chat) -> ChatInfo:
        await self._require_visible(chat)
        return await self._inner.get_chat_info(chat)

    async def get_messages(self, chat: Chat, query: MessageQuery) -> list[Message]:
        await self._require_visible(chat)
        return await self._inner.get_messages(chat, query)

    async def get_post(self, chat: Chat, post_id: int) -> Post | None:
        await self._require_visible(chat)
        return await self._inner.get_post(chat, post_id)

    async def get_comments(self, chat: Chat, post: Post, query: MessageQuery) -> list[Message]:
        # Only the channel is checked: comments of a public channel stay readable when its discussion group is private.
        await self._require_visible(chat)
        return await self._inner.get_comments(chat, post, query)

    async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
        await self._require_visible(chat)
        return await self._inner.get_media_files(chat, message_id)

    async def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
        await self._require_visible(chat)
        async for chunk in self._inner.read_media(chat, message_id, offset):
            yield chunk

    async def search_messages(self, query: SearchQuery, chat: Chat | None) -> list[Message]:
        if chat is not None:
            await self._require_visible(chat)
            return await self._inner.search_messages(query, chat)

        visible_ids = {c.id for c in await self.list_dialogs()}
        shown: list[Message] = []
        dropped = 0
        rounds = 0
        current = query
        while True:
            raw = await self._inner.search_messages(current, None)
            for m in raw:
                if m.chat_id in visible_ids:
                    shown.append(m)
                else:
                    dropped += 1
            if len(shown) >= query.limit or len(raw) < query.limit or rounds >= MAX_TOPUP_ROUNDS:
                break
            last = raw[-1]
            current = replace(query, after=SearchOffset(last.chat_id, last.id, last.date))
            rounds += 1
        log.info("global search: dropped %d hidden hits, %d top-up rounds", dropped, rounds)
        return shown[: query.limit]
