"""Read-only Telethon wrapper: the only door to the account.

Exposes read methods only. Never calls client.start() (it prompts on stdin, which
the MCP transport owns), never sends read receipts or status updates.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodError, RPCError
from telethon.tl import functions

from tg_bridge.config import Settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import Chat, ChatInfo, Person, Topic
from tg_bridge.telegram.identity import client_identity
from tg_bridge.telegram.mapping import chat_from_dialog, person_from_user, topic_from_forum_topic

log = logging.getLogger(__name__)

TOPICS_LIMIT = 100

NOT_LOGGED_IN = "Not logged in to Telegram. Run: uv run python -m tg_bridge.login"


class ReadOnlyTelegram:
    def __init__(self, settings: Settings, client_factory: Callable[..., Any] = TelegramClient):
        self._settings = settings
        self._client_factory = client_factory
        self._client: Any = None

    async def connect(self) -> None:
        session = self._settings.session_path
        # Telethon opens (and would create) the SQLite session in its constructor,
        # so check first: a missing session means "run login", not "make an empty one".
        if not session.is_file():
            raise NotLoggedInError(NOT_LOGGED_IN)
        if self._client is None:
            # receive_updates=False: we never need the update stream; it keeps the session passive.
            self._client = self._client_factory(
                str(session),
                self._settings.api_id,
                self._settings.api_hash,
                receive_updates=False,
                **client_identity(),
            )
        await self._call(self._client.connect)
        if not await self._call(self._client.is_user_authorized):
            await self._client.disconnect()
            # Drop it: Telethon saves its session on disconnect, and a stale client
            # reused after the user logs in would overwrite the fresh session file.
            self._client = None
            raise NotLoggedInError(NOT_LOGGED_IN)

    async def get_me(self) -> Person:
        return person_from_user(await self._call(self._connected().get_me))

    async def list_dialogs(self) -> list[Chat]:
        client = self._connected()
        started = time.monotonic()

        async def collect() -> list[Any]:
            # No archived=/folder= filter: Telegram then returns every folder. Migrated basic groups
            # would otherwise show up next to the supergroup they became.
            return [d async for d in client.iter_dialogs(ignore_migrated=True)]

        chats = [chat_from_dialog(d) for d in await self._call(collect)]
        log.info("fetched %d dialogs in %.2f s", len(chats), time.monotonic() - started)
        return chats

    async def get_chat_details(self, chat: Chat) -> ChatInfo:
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        description: str | None = None
        members: int | None = None
        topics: list[Topic] = []
        topics_total: int | None = None
        try:
            if chat.kind in ("user", "bot"):
                full = await self._call(lambda: client(functions.users.GetFullUserRequest(entity)))
                description = full.full_user.about
            elif chat.kind == "group":
                full = await self._call(lambda: client(functions.messages.GetFullChatRequest(-chat.id)))
                description = full.full_chat.about
                members = next((c.participants_count for c in full.chats if c.id == -chat.id), None)
            else:
                full = await self._call(lambda: client(functions.channels.GetFullChannelRequest(entity)))
                description = full.full_chat.about
                members = getattr(full.full_chat, "participants_count", None)
                if chat.is_forum:
                    topics, topics_total = await self._forum_topics(client, entity)
        except RPCError as e:
            # E.g. ChannelPrivateError for a chat we were removed from: the dialog data is still worth showing.
            log.warning("chat details unavailable for %d: %s", chat.id, type(e).__name__)
            description, members, topics, topics_total = None, None, [], None
        return ChatInfo(
            chat=chat,
            description=description or None,
            members_count=members,
            topics=tuple(topics),
            topics_total=topics_total,
        )

    async def _input_entity(self, client: Any, chat_id: int) -> Any:
        try:
            return await self._call(lambda: client.get_input_entity(chat_id))
        except ValueError:
            # The session's entity cache is cold (the dialog list came from our disk cache, not Telegram):
            # listing dialogs fills it, then the lookup works.
            await self.list_dialogs()
            return await self._call(lambda: client.get_input_entity(chat_id))

    async def _forum_topics(self, client: Any, entity: Any) -> tuple[list[Topic], int | None]:
        request = functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=TOPICS_LIMIT
        )
        result = await self._call(lambda: client(request))
        topics = [t for t in map(topic_from_forum_topic, result.topics) if t is not None]
        return topics, (result.count if result.count > len(topics) else None)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def _connected(self) -> Any:
        if self._client is None:
            raise RuntimeError("ReadOnlyTelegram.connect() must be called first")
        return self._client

    @staticmethod
    async def _call(fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await fn()
        except FloodError as e:
            seconds = int(getattr(e, "seconds", 0) or 0)
            log.warning("telegram flood wait: %d s", seconds)
            raise RateLimitedError(seconds) from e
