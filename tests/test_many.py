from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeSource, make_chat, make_message
from tg_bridge import many
from tg_bridge.errors import ReadTimeoutError, TooManyMessagesError
from tg_bridge.many import read_chats, search_chats
from tg_bridge.source import SearchOffset

DAY0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
A, B, C = -1001, -1002, -1003


def chats():
    return [make_chat(id=i, kind="channel", title=f"Chan {-i}") for i in (A, B, C)]


def m(id, chat, day=0, minutes=0, text="hello"):
    return make_message(id=id, chat_id=chat, text=text, date=DAY0 + timedelta(days=day, minutes=minutes))


def source_with(*messages):
    by_chat: dict[int, list] = {}
    for msg in messages:
        by_chat.setdefault(msg.chat_id, []).append(msg)
    return FakeSource(dialogs=chats(), messages=by_chat)


async def read(source, **kw):
    kw.setdefault("scope_text", 'folder "F1"')
    return await read_chats(source, chats(), DAY0 - timedelta(days=1), None, False, **kw)


async def test_read_merges_by_date_chat_id():
    source = source_with(m(1, A, 2), m(2, A, 1), m(1, B, 1), m(1, C, 3), m(2, B, 1, 5))
    result = await read(source)
    assert [(x.chat_id, x.id) for x in result.messages] == [(B, 1), (A, 2), (B, 2), (A, 1), (C, 1)]
    assert result.chats_with_messages == 3


async def test_read_queries_each_chat_once_in_order():
    source = source_with(m(1, A), m(2, A), m(1, C))
    result = await read(source)
    assert len(result.messages) == 3 and result.chats_with_messages == 2
    assert [q.direction for q in source.message_queries] == ["forward"] * 3
    assert [q.limit for q in source.message_queries] == [2001, 1999, 1999]


async def test_read_over_cap(monkeypatch):
    monkeypatch.setattr(many, "MAX_READ_MESSAGES", 5)
    source = source_with(*[m(i, A) for i in range(1, 4)], *[m(i, B) for i in range(1, 4)])
    with pytest.raises(TooManyMessagesError) as e:
        await read(source)
    assert str(e.value) == 'More than 5 messages in this period across 3 chats of folder "F1". Pass a shorter period.'


async def test_read_over_cap_single_chat(monkeypatch):
    monkeypatch.setattr(many, "MAX_READ_MESSAGES", 2)
    source = source_with(*[m(i, A) for i in range(1, 4)])
    with pytest.raises(TooManyMessagesError) as e:
        await read_chats(source, chats()[:1], DAY0, None, False, scope_text='"Family"')
    assert str(e.value) == 'More than 2 messages in this period in "Family". Pass a shorter period.'


async def test_read_exactly_at_cap_is_fine(monkeypatch):
    monkeypatch.setattr(many, "MAX_READ_MESSAGES", 3)
    source = source_with(*[m(i, A) for i in range(1, 4)])
    assert len((await read(source)).messages) == 3


async def test_read_deadline():
    source = source_with(m(1, A))
    times = iter([0.0, 0.0, 41.0, 41.0, 41.0])
    with pytest.raises(ReadTimeoutError) as e:
        await read(source, clock=lambda: next(times))
    assert str(e.value) == (
        "Reading took too long: 1 of 3 chats read in 41 s. Pass a shorter period or use a smaller folder."
    )


# --- search ---


async def search(source, *, limit=2, after=None, chat_list=None):
    return await search_chats(
        source, chat_list or chats(), "hello", DAY0 - timedelta(days=1), None, False, limit, after
    )


def key(msg):
    return (msg.date, msg.chat_id, msg.id)


async def test_search_newest_first_across_chats():
    source = source_with(m(1, A, 1), m(1, B, 3), m(1, C, 2))
    hits, more = await search(source, limit=5)
    assert [x.chat_id for x in hits] == [B, C, A] and not more
    assert [q.limit for q, _ in source.search_queries] == [6, 6, 6]
    assert [cid for _, cid in source.search_queries] == [A, B, C]


async def test_search_continuing_drops_boundary_and_newer():
    source = source_with(m(1, A, 1), m(2, A, 2), m(3, A, 3))
    boundary = SearchOffset(A, 3, DAY0 + timedelta(days=3))
    hits, more = await search(source, limit=5, after=boundary)
    assert [x.id for x in hits] == [2, 1] and not more


async def test_search_continuation_end_is_boundary_plus_one_second():
    source = source_with(m(1, A, 1))
    boundary = SearchOffset(A, 3, DAY0 + timedelta(days=3))
    await search(source, after=boundary)
    assert {q.end for q, _ in source.search_queries} == {boundary.date + timedelta(seconds=1)}
    capped = datetime(2026, 10, 1, tzinfo=UTC)
    source2 = source_with(m(1, A, 1))
    await search_chats(source2, chats(), "hello", None, capped, False, 2, boundary)
    assert {q.end for q, _ in source2.search_queries} == {capped}


async def test_search_chats_same_second_tie_on_boundary():
    source = source_with(m(1, A, 1), m(1, B, 1))
    page1, more1 = await search(source, limit=1)
    assert [x.chat_id for x in page1] == [A] and more1
    first = page1[0]
    page2, more2 = await search(source, limit=1, after=SearchOffset(first.chat_id, first.id, first.date))
    assert [x.chat_id for x in page2] == [B] and not more2


async def test_search_horizon_keeps_old_hits_behind_busy_chat():
    source = source_with(*[m(i, A, 7, i) for i in range(1, 6)], m(1, B, 1))
    page1, more = await search(source, limit=2)
    assert [(x.chat_id, x.id) for x in page1] == [(A, 5), (A, 4)] and more


async def test_search_chats_pages_cover_everything_once():
    source = source_with(*[m(i, A, 7, i) for i in range(1, 6)], m(1, B, 1))
    seen, after = [], None
    for _ in range(10):
        hits, more = await search(source, limit=2, after=after)
        seen += [(x.chat_id, x.id) for x in hits]
        if not more:
            break
        last = hits[-1]
        after = SearchOffset(last.chat_id, last.id, last.date)
    assert seen == [(A, 5), (A, 4), (A, 3), (A, 2), (A, 1), (B, 1)]
