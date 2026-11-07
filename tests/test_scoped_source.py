from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes import FakeSource, make_chat, make_media_file, make_message, make_person, make_user_chat
from tg_bridge.errors import ChatNotFoundError
from tg_bridge.source import MessageQuery, Post, SearchOffset, SearchQuery
from tg_bridge.source import scoped as scoped_mod
from tg_bridge.source.allowlist import Allowlist
from tg_bridge.source.scoped import ScopedSource

DM = make_user_chat(make_person(id=42, first_name="Anna", last_name="K"))
PUB = make_chat(id=-1001, kind="channel", title="News", username="news")
PRIV_ON = make_chat(id=-1002, kind="supergroup", title="Club", username=None)
PRIV_OFF = make_chat(id=-1003, kind="group", title="Secret", username=None)

T0 = datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def mquery(**kw):
    base = dict(direction="backward", start=None, end=None, boundary_id=None, topic_id=None, from_me=False, limit=10)
    base.update(kw)
    return MessageQuery(**base)


def squery(text="hit", limit=3, **kw):
    base = dict(text=text, start=None, end=None, from_me=False, limit=limit, after=None)
    base.update(kw)
    return SearchQuery(**base)


def build(tmp_path, **fake_kw):
    inner = FakeSource(dialogs=[DM, PUB, PRIV_ON, PRIV_OFF], **fake_kw)
    allow = Allowlist(tmp_path / "a.json")
    allow.set(PRIV_ON.id, True)
    return ScopedSource(inner, allow), inner, allow


async def test_list_dialogs_filters(tmp_path):
    scoped, _, _ = build(tmp_path)
    assert [c.id for c in await scoped.list_dialogs()] == [PUB.id, PRIV_ON.id]


async def test_list_dialogs_passes_refresh_through(tmp_path):
    scoped, inner, _ = build(tmp_path)
    await scoped.list_dialogs(refresh=True)
    assert inner.list_dialogs_calls == [True]


async def test_allowlist_reread_between_calls(tmp_path):
    scoped, _, allow = build(tmp_path)
    assert PRIV_OFF.id not in [c.id for c in await scoped.list_dialogs()]
    allow.set(PRIV_OFF.id, True)
    assert PRIV_OFF.id in [c.id for c in await scoped.list_dialogs()]
    allow.set(PRIV_ON.id, False)
    assert PRIV_ON.id not in [c.id for c in await scoped.list_dialogs()]


async def test_list_folders_passthrough(tmp_path):
    from tests.fakes import make_folder

    scoped, _, _ = build(tmp_path, folders=[make_folder()])
    assert len(await scoped.list_folders()) == 1


async def test_whoami_and_close_passthrough(tmp_path):
    scoped, inner, _ = build(tmp_path)
    assert (await scoped.whoami()).id == inner.me.id
    await scoped.close()
    assert inner.closed


def test_dialogs_age_delegates_when_present(tmp_path):
    inner = FakeSource()
    inner.dialogs_age = lambda: 12.5  # type: ignore[attr-defined]
    assert ScopedSource(inner, Allowlist(tmp_path / "a.json")).dialogs_age() == 12.5


def test_dialogs_age_none_when_inner_lacks_it(tmp_path):
    assert ScopedSource(FakeSource(), Allowlist(tmp_path / "a.json")).dialogs_age() is None


@pytest.mark.parametrize("hidden", [DM, PRIV_OFF])
async def test_every_chat_method_refuses_hidden_chat(tmp_path, hidden):
    post = Post(message=make_message(id=1, chat_id=hidden.id), comments_enabled=True, comment_count=0, discussion_id=None)
    scoped, inner, _ = build(tmp_path, messages={hidden.id: [make_message(chat_id=hidden.id, text="hit")]})
    calls = [
        scoped.get_chat_info(hidden),
        scoped.get_messages(hidden, mquery()),
        scoped.get_post(hidden, 1),
        scoped.get_comments(hidden, post, mquery()),
        scoped.get_media_files(hidden, 1),
        scoped.search_messages(squery(), hidden),
    ]
    for call in calls:
        with pytest.raises(ChatNotFoundError) as exc:
            await call
        assert exc.value.suggestions == []
        assert exc.value.arg == str(hidden.id)
    with pytest.raises(ChatNotFoundError):
        async for _ in scoped.read_media(hidden, 1, 0):
            pass
    assert inner.message_queries == [] and inner.search_queries == [] and inner.post_lookups == []
    assert inner.comment_queries == [] and inner.read_offsets == []


async def test_visible_chat_methods_pass_through(tmp_path):
    msg = make_message(id=5, chat_id=PUB.id, text="hit")
    media = make_media_file(5)
    post = Post(message=msg, comments_enabled=False, comment_count=None, discussion_id=None)
    scoped, _, _ = build(
        tmp_path, messages={PUB.id: [msg]}, media={(PUB.id, 5): (media, b"abcdef")}, posts={(PUB.id, 5): post}
    )
    assert (await scoped.get_chat_info(PUB)).chat == PUB
    assert [m.id for m in await scoped.get_messages(PUB, mquery())] == [5]
    assert await scoped.get_post(PUB, 5) == post
    assert await scoped.get_comments(PUB, post, mquery()) == []
    assert await scoped.get_media_files(PUB, 5) == [media]
    assert b"".join([c async for c in scoped.read_media(PUB, 5, 2)]) == b"cdef"
    assert [m.id for m in await scoped.search_messages(squery(), PUB)] == [5]  # chat-scoped search


async def test_hidden_check_uses_id_not_object_identity(tmp_path):
    # A chat object forged with a visible-looking username but a hidden id's real state must not pass.
    scoped, _, _ = build(tmp_path)
    forged = replace(PRIV_OFF, username="pretend_public")
    with pytest.raises(ChatNotFoundError):
        await scoped.get_messages(forged, mquery())


def hits(chat, n, start=0, text="hit"):
    # newer ids first in search order: later date = newer
    return [make_message(id=i, chat_id=chat.id, text=text, date=T0 + timedelta(minutes=i)) for i in range(start, start + n)]


async def test_global_search_drops_hidden_hits(tmp_path):
    scoped, _, _ = build(
        tmp_path,
        messages={DM.id: hits(DM, 1), PUB.id: hits(PUB, 1, start=10), PRIV_OFF.id: hits(PRIV_OFF, 1, start=20)},
    )
    found = await scoped.search_messages(squery(limit=10), None)
    assert [m.chat_id for m in found] == [PUB.id]


async def test_topup_fills_page_from_next_raw_pages(tmp_path):
    # raw newest-first: 6 hidden hits, then 3 visible
    scoped, inner, _ = build(
        tmp_path, messages={DM.id: hits(DM, 6, start=100), PUB.id: hits(PUB, 3)}
    )
    found = await scoped.search_messages(squery(limit=3), None)
    assert [m.chat_id for m in found] == [PUB.id] * 3
    assert len(inner.search_queries) == 3  # 3 hidden, 3 hidden, 3 visible
    assert inner.search_queries[1][0].after is not None


async def test_topup_returns_at_most_limit_and_never_duplicates(tmp_path):
    scoped, _, _ = build(tmp_path, messages={DM.id: hits(DM, 2, start=100), PUB.id: hits(PUB, 9)})
    found = await scoped.search_messages(squery(limit=4), None)
    assert len(found) == 4
    assert len({(m.chat_id, m.id) for m in found}) == 4


async def test_topup_stops_when_source_exhausted(tmp_path):
    scoped, inner, _ = build(tmp_path, messages={DM.id: hits(DM, 4, start=100), PUB.id: hits(PUB, 1)})
    found = await scoped.search_messages(squery(limit=3), None)
    assert [m.chat_id for m in found] == [PUB.id]
    # raw: 3 hidden (full) -> 1 hidden + 1 visible (short, stop)
    assert len(inner.search_queries) == 2


async def test_topup_bounded_by_max_rounds(tmp_path, monkeypatch):
    scoped, inner, _ = build(tmp_path, messages={DM.id: hits(DM, 100, start=100)})
    found = await scoped.search_messages(squery(limit=2), None)
    assert found == []
    assert len(inner.search_queries) == 1 + scoped_mod.MAX_TOPUP_ROUNDS


async def test_topup_continues_after_last_raw_hit_of_original_offset(tmp_path):
    scoped, inner, _ = build(tmp_path, messages={PUB.id: hits(PUB, 5)})
    after = SearchOffset(PUB.id, 3, T0 + timedelta(minutes=3))
    found = await scoped.search_messages(squery(limit=10, after=after), None)
    assert [m.id for m in found] == [2, 1, 0]
    assert inner.search_queries[0][0].after == after


async def test_chat_scoped_search_not_topped_up(tmp_path):
    scoped, inner, _ = build(tmp_path, messages={PUB.id: hits(PUB, 5)})
    found = await scoped.search_messages(squery(limit=3), PUB)
    assert len(found) == 3 and len(inner.search_queries) == 1


async def test_hidden_hits_logged_as_counts_only(tmp_path, caplog):
    import logging

    scoped, _, _ = build(tmp_path, messages={DM.id: hits(DM, 1, text="hit SECRETWORD"), PUB.id: hits(PUB, 1, start=10)})
    with caplog.at_level(logging.INFO):
        await scoped.search_messages(squery(limit=10), None)
    assert "SECRETWORD" not in caplog.text and "Anna" not in caplog.text
