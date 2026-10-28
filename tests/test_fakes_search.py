"""The fake's search behaviour is what later tool tests trust."""

from datetime import UTC, datetime, timedelta

from tests.fakes import FakeSource, make_chat, make_message
from tg_bridge.source import SearchOffset, SearchQuery

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
A = make_chat(id=-1001, title="A")
B = make_chat(id=-1002, title="B")


def msg(chat, id, days, text="адреса", **kw):
    return make_message(id=id, chat_id=chat.id, date=T0 + timedelta(days=days), text=text, **kw)


def q(text="адреса", **kw):
    fields = dict(text=text, start=None, end=None, from_me=False, limit=100, after=None)
    fields.update(kw)
    return SearchQuery(**fields)


def ids(page):
    return [(m.chat_id, m.id) for m in page.messages]


async def test_per_chat_matches_casefolded_substring_newest_first():
    src = FakeSource(dialogs=[A], messages={A.id: [msg(A, 1, 1, "Моя АДРЕСА"), msg(A, 2, 2, "ні"), msg(A, 3, 3, "адреси")]})
    page = await src.search_messages(q(), A)
    assert ids(page) == [(A.id, 1)]
    page = await src.search_messages(q("адрес"), A)
    assert ids(page) == [(A.id, 3), (A.id, 1)]
    assert page.resume is None
    assert src.search_queries[-1] == (q("адрес"), A.id)


async def test_global_spans_chats_sorted_by_date_desc():
    src = FakeSource(dialogs=[A, B], messages={A.id: [msg(A, 1, 1), msg(A, 2, 5)], B.id: [msg(B, 1, 3)]})
    page = await src.search_messages(q(), None)
    assert ids(page) == [(A.id, 2), (B.id, 1), (A.id, 1)]
    assert src.search_queries[-1][1] is None


async def test_after_offset_excludes_it_and_newer():
    src = FakeSource(dialogs=[A, B], messages={A.id: [msg(A, 1, 1), msg(A, 2, 5)], B.id: [msg(B, 1, 3)]})
    after = SearchOffset(chat_id=B.id, message_id=1, date=T0 + timedelta(days=3))
    page = await src.search_messages(q(after=after), None)
    assert ids(page) == [(A.id, 1)]
    # same date: ordered by (date, chat_id, message_id)
    src = FakeSource(dialogs=[A, B], messages={A.id: [msg(A, 1, 3)], B.id: [msg(B, 1, 3)]})
    full = ids(await src.search_messages(q(), None))
    first = full[0]
    after = SearchOffset(chat_id=first[0], message_id=first[1], date=T0 + timedelta(days=3))
    assert ids(await src.search_messages(q(after=after), None)) == full[1:]


async def test_period_and_from_me_filter():
    src = FakeSource(
        dialogs=[A],
        messages={A.id: [msg(A, 1, 1, out=True), msg(A, 2, 2), msg(A, 3, 3, out=True), msg(A, 4, 4, out=True)]},
    )
    page = await src.search_messages(q(start=T0 + timedelta(days=2), end=T0 + timedelta(days=4)), A)
    assert ids(page) == [(A.id, 3), (A.id, 2)]
    page = await src.search_messages(q(from_me=True), A)
    assert ids(page) == [(A.id, 4), (A.id, 3), (A.id, 1)]


async def test_scan_cap_sets_resume_to_last_scanned():
    msgs = [msg(A, i, i, out=(i == 9)) for i in range(1, 11)]  # newest first: 10, 9, 8, ...
    src = FakeSource(dialogs=[A], messages={A.id: msgs}, scan_cap=3)
    page = await src.search_messages(q(from_me=True, limit=5), None)
    assert ids(page) == [(A.id, 9)]
    assert page.resume == SearchOffset(chat_id=A.id, message_id=8, date=T0 + timedelta(days=8))
    # per-chat from_me is server-side: no cap
    page = await src.search_messages(q(from_me=True, limit=5), A)
    assert ids(page) == [(A.id, 9)] and page.resume is None
