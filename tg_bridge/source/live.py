"""MessageSource backed by live Telegram reads."""

import asyncio
import logging
import time

from tg_bridge.source.models import Person
from tg_bridge.telegram.client import ReadOnlyTelegram

log = logging.getLogger(__name__)


class LiveSource:
    """Connects on first use, so listing tools never touches Telegram and the account stays offline while idle."""

    def __init__(self, telegram: ReadOnlyTelegram):
        self._telegram = telegram
        self._connected = False
        self._lock = asyncio.Lock()

    async def whoami(self) -> Person:
        await self._ensure_connected()
        return await self._telegram.get_me()

    async def close(self) -> None:
        async with self._lock:
            if self._connected:
                await self._telegram.disconnect()
                self._connected = False
                log.info("telegram disconnected")

    async def _ensure_connected(self) -> None:
        async with self._lock:
            if self._connected:
                return
            started = time.monotonic()
            await self._telegram.connect()
            self._connected = True
            log.info("telegram connected in %.2f s", time.monotonic() - started)
