"""MessageSource backed by live Telegram reads."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Protocol

from tg_bridge.source.models import Chat, ChatInfo, ContactCard, Folder, HistoryStats, MediaFile, Message, MessageQuery, Person, Post, SearchQuery
from tg_bridge.telegram.client import ReadOnlyTelegram

log = logging.getLogger(__name__)


class DialogStore(Protocol):
    def load(self, now: float, max_age: float) -> tuple[list[Chat], list[Folder], float] | None: ...

    def save(self, chats: list[Chat], folders: list[Folder], now: float) -> None: ...


class LiveSource:
    """Connects on first use, so listing tools never touches Telegram and the account stays offline while idle."""

    def __init__(
        self,
        telegram: ReadOnlyTelegram,
        clock: Callable[[], float] = time.time,
        dialogs_ttl: float = 300.0,
        dialog_cache: DialogStore | None = None,
    ):
        # Wall clock, not monotonic: the disk cache's timestamp has to mean something to the next process.
        self._telegram = telegram
        self._clock = clock
        self._dialogs_ttl = dialogs_ttl
        self._dialog_cache = dialog_cache
        self._connected = False
        # One lock for connect + dialog cache: concurrent first calls trigger one connect and one fetch.
        self._lock = asyncio.Lock()
        self._dialogs: list[Chat] | None = None
        self._folders: list[Folder] = []
        self._dialogs_at = 0.0

    async def whoami(self) -> Person:
        await self._ensure_connected()
        return await self._telegram.get_me()

    async def list_dialogs(self, *, refresh: bool = False) -> list[Chat]:
        return (await self._snapshot(refresh))[0]

    async def list_folders(self, *, refresh: bool = False) -> list[Folder]:
        return (await self._snapshot(refresh))[1]

    async def _snapshot(self, refresh: bool) -> tuple[list[Chat], list[Folder]]:
        """Dialogs and folders always come from the same fetch: folder rules are evaluated over the dialog list."""
        async with self._lock:
            fresh = self._dialogs is not None and self._clock() - self._dialogs_at < self._dialogs_ttl
            if not fresh and not refresh:
                fresh = self._load_from_disk()
            if refresh or not fresh:
                await self._connect_locked()
                # Fetch both before storing either: a failure leaves the old snapshot untouched.
                dialogs = await self._telegram.list_dialogs()
                folders = await self._telegram.list_folders()
                self._dialogs, self._folders, self._dialogs_at = dialogs, folders, self._clock()
                self._save_to_disk()
            return list(self._dialogs or []), list(self._folders)

    def _load_from_disk(self) -> bool:
        if self._dialog_cache is None:
            return False
        hit = self._dialog_cache.load(self._clock(), self._dialogs_ttl)
        if hit is None:
            return False
        self._dialogs, self._folders, self._dialogs_at = hit
        log.info("loaded %d dialogs and %d folders from disk cache", len(self._dialogs), len(self._folders))
        return True

    def _save_to_disk(self) -> None:
        if self._dialog_cache is None or self._dialogs is None:
            return
        try:
            self._dialog_cache.save(self._dialogs, self._folders, self._dialogs_at)
        except OSError as e:
            # The cache is an optimisation; the fetched list is still good.
            log.warning("could not write dialog cache: %s", type(e).__name__)

    def dialogs_age(self) -> float | None:
        """Seconds since the last dialog fetch, None if never fetched."""
        return None if self._dialogs is None else self._clock() - self._dialogs_at

    async def get_chat_info(self, chat: Chat) -> ChatInfo:
        await self._ensure_connected()
        return await self._telegram.get_chat_details(chat)

    async def get_messages(self, chat: Chat, query: MessageQuery) -> list[Message]:
        await self._ensure_connected()
        return await self._telegram.get_messages(chat, query)

    async def get_history_stats(self, chat: Chat) -> HistoryStats:
        await self._ensure_connected()
        return await self._telegram.get_history_stats(chat)

    async def get_contact_cards(self, chat: Chat, limit: int = 20) -> list[ContactCard]:
        await self._ensure_connected()
        return await self._telegram.get_contact_cards(chat, limit)

    async def get_post(self, chat: Chat, post_id: int) -> Post | None:
        await self._ensure_connected()
        return await self._telegram.get_post(chat, post_id)

    async def get_comments(self, chat: Chat, post: Post, query: MessageQuery) -> list[Message]:
        await self._ensure_connected()
        return await self._telegram.get_comments(chat, post, query)

    async def search_messages(self, query: SearchQuery, chat: Chat | None) -> list[Message]:
        await self._ensure_connected()
        return await self._telegram.search(query, chat)

    async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
        await self._ensure_connected()
        return await self._telegram.get_media_files(chat, message_id)

    async def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
        await self._ensure_connected()
        async for chunk in self._telegram.read_media(chat, message_id, offset):
            yield chunk

    async def close(self) -> None:
        async with self._lock:
            if self._connected:
                await self._telegram.disconnect()
                self._connected = False
                log.info("telegram disconnected")

    async def download_chat_photo(self, chat_id: int) -> bytes | None:
        """The admin page's chat icons, through the same client as everything else."""
        await self._ensure_connected()
        return await self._telegram.download_chat_photo(chat_id)

    async def _ensure_connected(self) -> None:
        async with self._lock:
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._connected:
            return
        started = time.monotonic()
        await self._telegram.connect()
        self._connected = True
        log.info("telegram connected in %.2f s", time.monotonic() - started)
