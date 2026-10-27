"""Read-only Telethon wrapper: the only door to the account.

Exposes read methods only. Never calls client.start() (it prompts on stdin, which
the MCP transport owns), never sends read receipts or status updates.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from telethon import TelegramClient
from telethon.errors import FloodError

from tg_bridge.config import Settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import Person
from tg_bridge.telegram.identity import client_identity
from tg_bridge.telegram.mapping import person_from_user

log = logging.getLogger(__name__)

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
