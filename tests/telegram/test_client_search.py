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


async def test_per_chat_stops_at_start_skips_end_and_stops_at_limit(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages = [raw(9, 9), raw(8, 8), raw(7, 7), raw(6, 6), raw(5, 5), raw(4, 4)]
    page = await tg.search(q(start=T0 + timedelta(days=5), end=T0 + timedelta(days=9)), CHAT)
    assert [m.id for m in page.messages] == [8, 7, 6, 5] and page.resume is None
    page = await tg.search(q(limit=2), CHAT)
    assert [m.id for m in page.messages] == [9, 8]


# --- global ---


async def test_global_first_request(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(1, 1)])]
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
    assert [m.id for m in page.messages] == list(range(1, 102))
    assert [m.chat_id for m in page.messages][:3] == [-1000000000010, -1000000000011, -1000000000010]
    assert len(fake.requests) == 2 and fake.requests[0].limit == 100
    r2 = fake.requests[1]  # advanced from the last raw message of page 1
    assert (r2.offset_id, r2.offset_rate, r2.limit) == (100, int(first[-1].date.timestamp()), 1)
    assert r2.offset_peer == "input:-1000000000011"


async def test_global_stops_on_short_page_and_non_slice(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(1, 1)])]
    page = await tg.search(q(limit=5), None)
    assert [m.id for m in page.messages] == [1] and len(fake.requests) == 1  # short page
    fake.requests.clear()
    fake.result_queues["SearchGlobalRequest"] = [tl.messages.Messages(messages=[raw(i, i) for i in range(5, 0, -1)], chats=[], users=[], topics=[])]
    page = await tg.search(q(limit=10), None)
    assert len(page.messages) == 5 and len(fake.requests) == 1  # not a slice: asking again would repeat it


async def test_global_period_client_side(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(9, 9), raw(8, 8), raw(7, 7), raw(6, 6)])]
    page = await tg.search(q(limit=4, start=T0 + timedelta(days=7), end=T0 + timedelta(days=9)), None)
    assert [m.id for m in page.messages] == [8, 7]


async def test_global_from_me_filters_and_scan_cap_resumes(wrapper, monkeypatch):
    tg, fake = await connected(wrapper)
    monkeypatch.setattr(client_module, "GLOBAL_SCAN_CAP", 4)
    msgs = [raw(i, 20 - i, out=(i == 2)) for i in range(1, 11)]
    fake.result_queues["SearchGlobalRequest"] = [slice_(msgs[:5]), slice_(msgs[5:])]
    page = await tg.search(q(from_me=True, limit=3), None)
    assert [m.id for m in page.messages] == [2]
    assert page.resume == SearchOffset(-1000000000010, 4, msgs[3].date.astimezone(UTC))
    assert len(fake.requests) == 1


async def test_global_from_me_without_cap_reaches_limit(wrapper):
    tg, fake = await connected(wrapper)
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(i, 20 - i, out=True) for i in range(1, 4)])]
    page = await tg.search(q(from_me=True, limit=2), None)
    assert [m.id for m in page.messages] == [1, 2] and page.resume is None


async def test_flood_wait_inside_loop_is_rate_limited(wrapper):
    tg, fake = await connected(wrapper)
    fake.request_errors["SearchGlobalRequest"] = FloodWaitError(request=None, capture=120)
    with pytest.raises(RateLimitedError):
        await tg.search(q(), None)


async def test_logs_counts_without_query_text(wrapper, caplog):
    tg, fake = await connected(wrapper)
    fake.messages = [raw(1, 1)]
    fake.result_queues["SearchGlobalRequest"] = [slice_([raw(1, 1)])]
    with caplog.at_level(logging.INFO):
        await tg.search(q(text="секретнеслово"), CHAT)
        await tg.search(q(text="секретнеслово"), None)
    text = caplog.text
    assert "chat -1000000000010" in text and "global" in text and "1 hits" in text
    assert "секретнеслово" not in text and "адреса" not in text
