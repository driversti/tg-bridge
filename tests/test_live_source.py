import asyncio

import pytest

from tests.fakes import make_chat, make_folder, make_person
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import ChatInfo
from tg_bridge.source.live import LiveSource


class StubTelegram:
    """Has the ReadOnlyTelegram surface; counts calls."""

    def __init__(self, connect_errors: list[Exception] | None = None):
        self.connect_errors = list(connect_errors or [])
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.dialog_calls = 0
        self.dialog_errors: list[Exception] = []
        self.folder_calls = 0
        self.folder_errors: list[Exception] = []
        self.folder_title = "Folder 1"
        self.detail_calls = 0
        self.message_calls = []

    async def list_dialogs(self):
        self.dialog_calls += 1
        await asyncio.sleep(0)
        if self.dialog_errors:
            raise self.dialog_errors.pop(0)
        return [make_chat(title="Secret Title")]

    async def list_folders(self):
        self.folder_calls += 1
        await asyncio.sleep(0)
        if self.folder_errors:
            raise self.folder_errors.pop(0)
        return [make_folder(title=self.folder_title)]

    async def get_chat_details(self, chat):
        self.detail_calls += 1
        return ChatInfo(chat=chat, description=None, members_count=None, topics=(), topics_total=None)

    async def get_messages(self, chat, query):
        self.message_calls.append((chat, query))
        return []

    async def get_media_files(self, chat, message_id):
        self.message_calls.append(("media_files", chat, message_id))
        return []

    async def read_media(self, chat, message_id, offset):
        self.message_calls.append(("read_media", chat, message_id, offset))
        yield b"ab"

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


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_list_dialogs_connects_lazily():
    tg = StubTelegram()
    source = LiveSource(tg)
    assert tg.connect_calls == 0

    await source.list_dialogs()

    assert tg.connect_calls == 1


async def test_dialogs_cached_within_ttl():
    tg, clock = StubTelegram(), Clock()
    source = LiveSource(tg, clock=clock, dialogs_ttl=300)

    await source.list_dialogs()
    clock.now += 299
    await source.list_dialogs()

    assert tg.dialog_calls == 1


async def test_dialogs_refetched_after_ttl():
    tg, clock = StubTelegram(), Clock()
    source = LiveSource(tg, clock=clock, dialogs_ttl=300)

    await source.list_dialogs()
    clock.now += 301
    await source.list_dialogs()

    assert tg.dialog_calls == 2


async def test_refresh_bypasses_cache():
    tg = StubTelegram()
    source = LiveSource(tg)

    await source.list_dialogs()
    await source.list_dialogs(refresh=True)

    assert tg.dialog_calls == 2


async def test_concurrent_first_calls_fetch_once():
    tg = StubTelegram()
    source = LiveSource(tg)

    await asyncio.gather(source.list_dialogs(), source.list_dialogs(), source.list_dialogs())

    assert tg.dialog_calls == 1
    assert tg.connect_calls == 1


async def test_failed_fetch_is_not_cached():
    tg = StubTelegram()
    tg.dialog_errors = [RateLimitedError(5)]
    source = LiveSource(tg)

    with pytest.raises(RateLimitedError):
        await source.list_dialogs()
    assert len(await source.list_dialogs()) == 1

    assert tg.dialog_calls == 2


async def test_dialogs_age():
    tg, clock = StubTelegram(), Clock()
    source = LiveSource(tg, clock=clock)
    assert source.dialogs_age() is None

    await source.list_dialogs()
    clock.now += 42

    assert source.dialogs_age() == 42


async def test_returned_list_is_a_copy():
    source = LiveSource(StubTelegram())

    (await source.list_dialogs()).clear()

    assert len(await source.list_dialogs()) == 1


async def test_get_chat_info_not_cached():
    tg = StubTelegram()
    source = LiveSource(tg)
    chat = make_chat()

    await source.get_chat_info(chat)
    info = await source.get_chat_info(chat)

    assert info.chat == chat
    assert tg.detail_calls == 2


async def test_logs_have_no_titles(caplog):
    source = LiveSource(StubTelegram())

    with caplog.at_level("DEBUG"):
        await source.list_dialogs()
        await source.get_chat_info(make_chat(title="Secret Title"))

    assert "Secret Title" not in caplog.text


class MemoryCache:
    """DialogCache stand-in."""

    def __init__(self, chats=None, saved_at=0.0, save_error: Exception | None = None, folders=None):
        self.chats, self.saved_at, self.save_error = chats, saved_at, save_error
        self.folders = list(folders or [])
        self.saves = 0

    def load(self, now, max_age):
        if self.chats is None or not 0 <= now - self.saved_at < max_age:
            return None
        return list(self.chats), list(self.folders), self.saved_at

    def save(self, chats, folders, now):
        self.saves += 1
        if self.save_error:
            raise self.save_error
        self.chats, self.folders, self.saved_at = list(chats), list(folders), now


async def test_fresh_disk_cache_skips_telegram():
    tg, clock = StubTelegram(), Clock()
    cache = MemoryCache([make_chat(title="From disk")], saved_at=clock.now - 100)
    source = LiveSource(tg, clock=clock, dialog_cache=cache)

    chats = await source.list_dialogs()

    assert [c.title for c in chats] == ["From disk"]
    assert (tg.connect_calls, tg.dialog_calls) == (0, 0)
    assert source.dialogs_age() == 100


async def test_stale_disk_cache_fetches_and_saves():
    tg, clock = StubTelegram(), Clock()
    cache = MemoryCache([make_chat(title="Old")], saved_at=clock.now - 301)
    source = LiveSource(tg, clock=clock, dialogs_ttl=300, dialog_cache=cache)

    chats = await source.list_dialogs()

    assert [c.title for c in chats] == ["Secret Title"]
    assert tg.dialog_calls == 1
    assert (cache.saves, cache.saved_at) == (1, clock.now)


async def test_refresh_ignores_disk_cache():
    tg, clock = StubTelegram(), Clock()
    cache = MemoryCache([make_chat(title="From disk")], saved_at=clock.now)
    source = LiveSource(tg, clock=clock, dialog_cache=cache)

    await source.list_dialogs(refresh=True)

    assert tg.dialog_calls == 1
    assert cache.saves == 1


async def test_cache_write_failure_does_not_fail_the_call():
    tg = StubTelegram()
    source = LiveSource(tg, dialog_cache=MemoryCache(save_error=OSError("disk full")))

    assert len(await source.list_dialogs()) == 1


async def test_folders_share_the_dialog_refresh():
    tg, clock = StubTelegram(), Clock()
    source = LiveSource(tg, clock=clock, dialogs_ttl=300)

    await source.list_dialogs()
    folders = await source.list_folders()

    assert [f.title for f in folders] == ["Folder 1"]
    assert (tg.dialog_calls, tg.folder_calls) == (1, 1)

    clock.now += 301
    await source.list_folders()
    await source.list_dialogs()

    assert (tg.dialog_calls, tg.folder_calls) == (2, 2)


async def test_folders_first_also_fetches_dialogs_once():
    tg = StubTelegram()
    source = LiveSource(tg)

    await source.list_folders()
    await source.list_dialogs()

    assert (tg.dialog_calls, tg.folder_calls) == (1, 1)


async def test_failed_refresh_keeps_old_dialogs_and_folders():
    tg, clock = StubTelegram(), Clock()
    source = LiveSource(tg, clock=clock, dialogs_ttl=300)
    await source.list_dialogs()
    tg.folder_errors = [RateLimitedError(5)]
    tg.folder_title = "New"
    clock.now += 301

    with pytest.raises(RateLimitedError):
        await source.list_folders()

    assert [f.title for f in await source.list_folders(refresh=False)] == ["New"]  # next call retries cleanly
    assert tg.folder_calls == 3  # fail, then retry succeeded (the first call was the initial refresh)


async def test_failed_folder_refresh_stores_nothing_of_it():
    tg, clock = StubTelegram(), Clock()
    cache = MemoryCache()
    source = LiveSource(tg, clock=clock, dialogs_ttl=300, dialog_cache=cache)
    await source.list_dialogs()
    old_dialogs_at = source.dialogs_age()
    tg.folder_errors = [RateLimitedError(5)]
    clock.now += 100

    with pytest.raises(RateLimitedError):
        await source.list_dialogs(refresh=True)

    assert source.dialogs_age() == old_dialogs_at + 100  # still the old snapshot's timestamp
    assert cache.saves == 1


async def test_concurrent_dialogs_and_folders_fetch_once_each():
    tg = StubTelegram()
    source = LiveSource(tg)

    await asyncio.gather(source.list_dialogs(), source.list_folders(), source.list_folders())

    assert (tg.connect_calls, tg.dialog_calls, tg.folder_calls) == (1, 1, 1)


async def test_fresh_disk_cache_serves_folders():
    tg, clock = StubTelegram(), Clock()
    cache = MemoryCache([make_chat()], saved_at=clock.now - 10, folders=[make_folder(title="Disk")])
    source = LiveSource(tg, clock=clock, dialog_cache=cache)

    assert [f.title for f in await source.list_folders()] == ["Disk"]
    assert (tg.dialog_calls, tg.folder_calls) == (0, 0)


async def test_logs_have_no_folder_titles(caplog):
    tg = StubTelegram()
    tg.folder_title = "Secret Folder"
    source = LiveSource(tg)

    with caplog.at_level("DEBUG"):
        await source.list_folders()

    assert "Secret Folder" not in caplog.text


async def test_get_messages_connects_and_delegates():
    from tg_bridge.source import MessageQuery

    tg = StubTelegram()
    source = LiveSource(tg)
    chat = make_chat()
    query = MessageQuery("forward", None, None, None, None, False, 10)

    assert await source.get_messages(chat, query) == []

    assert tg.connect_calls == 1
    assert tg.message_calls == [(chat, query)]


async def test_search_messages_passes_through():
    from tg_bridge.source import SearchQuery

    class T(StubTelegram):
        async def search(self, query, chat):
            self.searched = (query, chat)
            return []

    tg = T()
    source = LiveSource(tg)
    query = SearchQuery("x", None, None, False, 5, None)
    page = await source.search_messages(query, None)
    assert page == [] and tg.searched == (query, None)


async def test_media_methods_connect_and_delegate():
    tg = StubTelegram()
    source = LiveSource(tg)
    chat = make_chat()

    assert await source.get_media_files(chat, 7) == []
    chunks = [c async for c in source.read_media(chat, 7, 1024)]

    assert chunks == [b"ab"]
    assert tg.connect_calls == 1
    assert tg.message_calls == [("media_files", chat, 7), ("read_media", chat, 7, 1024)]
