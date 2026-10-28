import logging
from datetime import UTC, datetime, timedelta

import pytest
from telethon.errors import FloodWaitError
from telethon.tl import types as tl

from tests.fakes import make_chat
from tests.telegram.test_client import Factory, settings, wrapper  # noqa: F401  (fixtures)
from tests.telegram.test_mapping import tl_msg, tl_user
from tg_bridge.errors import RateLimitedError
from tg_bridge.source import SearchOffset, SearchQuery
from tg_bridge.telegram import client as client_module
from tg_bridge.telegram.client import ReadOnlyTelegram

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CHAT = make_chat(id=-1000000000010, title="Family")


def raw(id, day, text="адреса", peer=10, out=False):
    return tl_msg(id=id, text=text, date=T0 + timedelta(days=day), peer_id=tl.PeerChannel(peer), out=out)


def q(**kw):
    fields = dict(text="адреса", start=None, end=None, from_me=False, limit=10, after=None)
    fields.update(kw)
    return SearchQuery(**fields)


def slice_(messages, count=1000):
    return tl.messages.MessagesSlice(messages=messages, chats=[], users=[tl_user(id=5)], count=count, topics=[])


async def connected(wrapper):
    tg, fake = wrapper
    await tg.connect()
    return tg, fake


# --- per chat ---


async def test_per_chat_passes_search_arguments(wrapper):
    tg, fake = await connected(wrapper)
    after = SearchOffset(CHAT.id, 4790, T0)
    end = T0 + timedelta(days=30)
    await tg.search(q(end=end, after=after, from_me=True), CHAT)
    entity, kwargs = fake.iter_messages_calls[0]
    assert entity == f"input:{CHAT.id}"
    assert kwargs["search"] == "адреса" and kwargs["offset_date"] == end and kwargs["offset_id"] == 4790
    assert kwargs["from_user"] == "me"
    await tg.search(q(), CHAT)
    assert "from_user" not in fake.iter_messages_calls[1][1] and fake.iter_messages_calls[1][1]["offset_id"] == 0


async def test_per_chat_search_does_not_sleep_between_pages(wrapper):
    tg, fake = await connected(wrapper)
    await tg.search(q(limit=3000), CHAT)
    await tg.search(q(limit=3001), CHAT)
    small, big = (kw for _, kw in fake.iter_messages_calls)
    assert small["wait_time"] == 0
    assert "wait_time" not in big


async def test_per_chat_stops_at_start_skips_end_and_stops_at_limit(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages = [raw(9, 9), raw(8, 8), raw(7, 7), raw(6, 6), raw(5, 5), raw(4, 4)]
    page = await tg.search(q(start=T0 + timedelta(days=5), end=T0 + timedelta(days=9)), CHAT)
    assert [m.id for m in page] == [8, 7, 6, 5]
    page = await tg.search(q(limit=2), CHAT)
    assert [m.id for m in page] == [9, 8]


# --- global ---


async def test_global_first_request(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(1, 1)]), slice_([])]
    start, end = T0, T0 + timedelta(days=30)
    await tg.search(q(start=start, end=end), None)
    r = fake.requests[0]
    assert (r.q, r.min_date, r.max_date, r.offset_rate, r.offset_id) == ("адреса", start, end, 0, 0)
    assert isinstance(r.offset_peer, tl.InputPeerEmpty) and isinstance(r.filter, tl.InputMessagesFilterEmpty)
    assert r.limit == 10


async def test_global_after_resumes_from_boundary_hit(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([])]
    await tg.search(q(after=SearchOffset(-1000000000042, 77, T0 + timedelta(days=3))), None)
    r = fake.requests[0]
    assert r.offset_rate == int((T0 + timedelta(days=3)).timestamp())
    assert r.offset_peer == "input:-1000000000042" and r.offset_id == 77


async def test_global_pages_internally_and_maps_chat_ids(wrapper):
    tg, fake = await connected(wrapper)
    first = [raw(i, 200 - i, peer=10 if i % 2 else 11) for i in range(1, 101)]
    second = [raw(101, 99, peer=10)]
    fake.result_queues["SearchGlobalRequest"] = [slice_(first), slice_(second)]
    page = await tg.search(q(limit=101), None)  # the tool asks for limit + 1, so 101 is the largest real ask
    assert [m.id for m in page] == list(range(1, 102))
    assert [m.chat_id for m in page][:3] == [-1000000000010, -1000000000011, -1000000000010]
    assert len(fake.requests) == 2 and fake.requests[0].limit == 100
    r2 = fake.requests[1]  # advanced from the last raw message of page 1
    assert (r2.offset_id, r2.offset_rate, r2.limit) == (100, int(first[-1].date.timestamp()), 1)
    assert r2.offset_peer == "input:-1000000000011"


async def test_global_short_page_is_not_the_end_empty_page_is(wrapper):
    # Telegram returns short pages when it hides some hits (e.g. "not displayable due to local laws"),
    # so only an empty page or a non-slice answer ends the search.
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(3, 3)]), slice_([raw(2, 2)]), slice_([])]
    page = await tg.search(q(limit=5), None)
    assert [m.id for m in page] == [3, 2] and len(fake.requests) == 3


async def test_global_stops_on_non_slice(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [tl.messages.Messages(messages=[raw(i, i) for i in range(5, 0, -1)], chats=[], users=[], topics=[])]
    page = await tg.search(q(limit=10), None)
    assert len(page) == 5 and len(fake.requests) == 1  # not a slice: asking again would repeat it


async def test_global_period_client_side(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(9, 9), raw(8, 8), raw(7, 7), raw(6, 6)])]
    page = await tg.search(q(limit=4, start=T0 + timedelta(days=7), end=T0 + timedelta(days=9)), None)
    assert [m.id for m in page] == [8, 7]


def mine(messages):
    return tl.messages.Messages(messages=messages, chats=[], users=[tl_user(id=5)], topics=[])


async def test_global_from_me_is_filtered_by_the_server(wrapper):
    # SearchGlobal has no sender filter; messages.Search on inputPeerEmpty with from_id=self searches only the
    # user's own messages across all dialogs (verified on the real account).
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchRequest"] = [mine([raw(2, 2, out=True, peer=11), raw(1, 1, out=True)])]
    start, end = T0, T0 + timedelta(days=30)
    page = await tg.search(q(from_me=True, start=start, end=end), None)
    assert [(m.chat_id, m.id) for m in page] == [(-1000000000011, 2), (-1000000000010, 1)]
    assert [type(r).__name__ for r in fake.requests] == ["SearchRequest"]
    r = fake.requests[0]
    assert isinstance(r.peer, tl.InputPeerEmpty) and isinstance(r.from_id, tl.InputPeerSelf)
    assert (r.q, r.min_date, r.max_date, r.offset_id, r.add_offset, r.limit) == ("адреса", start, end, 0, 0, 10)


async def test_global_from_me_resumes_from_boundary_hit(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchRequest"] = [mine([])]
    boundary = T0 + timedelta(days=3)
    await tg.search(q(from_me=True, end=T0 + timedelta(days=30), after=SearchOffset(-1000000000042, 77, boundary)), None)
    r = fake.requests[0]
    assert (r.offset_id, r.max_date) == (77, boundary)


async def test_global_from_me_pages_internally_until_empty_or_non_slice(wrapper):
    tg, fake = await connected(wrapper)
    first = [raw(i, 200 - i, out=True) for i in range(1, 101)]
    fake.result_queues["SearchRequest"] = [slice_(first), slice_([raw(101, 50, out=True)]), slice_([])]
    page = await tg.search(q(from_me=True, limit=150), None)
    assert [m.id for m in page] == list(range(1, 102))
    assert [(r.offset_id, r.max_date) for r in fake.requests[1:]] == [(100, first[-1].date), (101, T0 + timedelta(days=50))]
    fake.requests.clear()
    fake.result_queues["SearchRequest"] = [mine([raw(1, 1, out=True)])]
    await tg.search(q(from_me=True, limit=5), None)
    assert len(fake.requests) == 1  # not a slice: everything came back at once


async def test_flood_wait_inside_loop_is_rate_limited(wrapper):
    tg, fake = await connected(wrapper)
    fake.request_errors["SearchGlobalRequest"] = FloodWaitError(request=None, capture=120)
    with pytest.raises(RateLimitedError):
        await tg.search(q(), None)


async def test_logs_counts_without_query_text(wrapper, caplog):
    tg, fake = await connected(wrapper)
    fake.messages = [raw(1, 1)]
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(1, 1)]), slice_([])]
    with caplog.at_level(logging.INFO):
        await tg.search(q(text="секретнеслово"), CHAT)
        await tg.search(q(text="секретнеслово"), None)
    text = caplog.text
    assert "chat -1000000000010" in text and "global" in text and "1 hits" in text
    assert "секретнеслово" not in text and "адреса" not in text
