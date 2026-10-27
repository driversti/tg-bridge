import asyncio

import pytest

from tests.fakes import make_person
from tg_bridge.errors import NotLoggedInError
from tg_bridge.source.live import LiveSource


class StubTelegram:
    """Has the ReadOnlyTelegram surface; counts calls."""

    def __init__(self, connect_errors: list[Exception] | None = None):
        self.connect_errors = list(connect_errors or [])
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        await asyncio.sleep(0)
        if self.connect_errors:
            raise self.connect_errors.pop(0)

    async def get_me(self):
        return make_person()

    async def disconnect(self):
        self.disconnect_calls += 1


async def test_whoami_connects_lazily():
    tg = StubTelegram()
    source = LiveSource(tg)
    assert tg.connect_calls == 0

    assert (await source.whoami()).id == 42
    await source.whoami()

    assert tg.connect_calls == 1


async def test_concurrent_first_calls_connect_once():
    tg = StubTelegram()
    source = LiveSource(tg)

    await asyncio.gather(source.whoami(), source.whoami(), source.whoami())

    assert tg.connect_calls == 1


async def test_failed_connect_retries_next_call():
    tg = StubTelegram(connect_errors=[NotLoggedInError("run login")])
    source = LiveSource(tg)

    with pytest.raises(NotLoggedInError):
        await source.whoami()
    assert (await source.whoami()).id == 42

    assert tg.connect_calls == 2


async def test_close_without_connect_is_noop():
    tg = StubTelegram()

    await LiveSource(tg).close()

    assert tg.disconnect_calls == 0


async def test_close_disconnects():
    tg = StubTelegram()
    source = LiveSource(tg)
    await source.whoami()

    await source.close()

    assert tg.disconnect_calls == 1
