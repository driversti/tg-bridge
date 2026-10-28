import re
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_message
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOW = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # noqa: E731
A = make_chat(id=-1001234567890, title="Family")
B = make_chat(id=-77, kind="group", title="Chess")


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


def hit(chat, id, day, text="моя адреса тут", **kw):
    return make_message(id=id, chat_id=chat.id, date=T0 + timedelta(days=day), text=text, **kw)


def source_with(n=7, **kw):
    return FakeSource(dialogs=[A, B], messages={A.id: [hit(A, i, i) for i in range(1, n + 1)]}, **kw)


def hit_ids(text):
    return [int(m) for m in re.findall(r"^#(\d+) ", text, re.M)]


def cursor_of(text):
    return re.search(r'cursor="([^"]+)"', text).group(1)


async def test_per_chat_hits_with_dates(settings):
    out = await tools.search_messages(source_with(3), settings, "адреса", "family")
    assert out.splitlines()[0].startswith('Search "адреса" in Family (-1001234567890) · all time')
    assert hit_ids(out) == [3, 2, 1]
    assert "#3 2026-09-04 14:00" in out and "No more hits." in out and "Context:" in out


async def test_global_grouped(settings):
    src = FakeSource(dialogs=[A, B], messages={A.id: [hit(A, 1, 1)], B.id: [hit(B, 5, 3)]})
    out = await tools.search_messages(src, settings, "адреса")
    assert "in all chats" in out
    assert out.index("— Chess (-77) —") < out.index("— Family (-1001234567890)")
    assert src.search_queries[0][1] is None


async def test_from_me_and_period_passed_through(settings):
    src = source_with(3)
    await tools.search_messages(src, settings, "адреса", "family", since="2026-09-02", until="2026-09-10", from_me=True, now=NOW)
    q, chat_id = src.search_queries[0]
    assert q.from_me and chat_id == A.id and q.start is not None and q.end is not None and q.after is None


async def test_asks_limit_plus_one_and_detects_more(settings):
    src = source_with(7)
    out = await tools.search_messages(src, settings, "адреса", "family", limit=3)
    assert src.search_queries[0][0].limit == 4
    assert hit_ids(out) == [7, 6, 5] and "Older hits remain." in out and 'chat="-1001234567890"' in out


async def test_limit_is_clamped(settings):
    src = source_with(1)
    await tools.search_messages(src, settings, "адреса", "family", limit=10_000)
    assert src.search_queries[0][0].limit == 101


@pytest.mark.parametrize("chat", ["family", None])
async def test_cursor_round_trip_three_pages(settings, chat):
    src = source_with(7)
    seen, cursor = [], None
    for _ in range(3):
        out = await tools.search_messages(src, settings, "адреса", chat, cursor=cursor, limit=3)
        seen += hit_ids(out)
        cursor = cursor_of(out) if "Next:" in out else None
    assert seen == [7, 6, 5, 4, 3, 2, 1] and cursor is None
    assert "No more hits." in out


async def test_cursor_with_changed_query(settings):
    out = await tools.search_messages(source_with(7), settings, "адреса", "family", limit=3)
    with pytest.raises(ToolError, match="another query"):
        await tools.search_messages(source_with(7), settings, "паспорт", "family", cursor=cursor_of(out))


async def test_chat_cursor_without_chat_searches_cursor_chat(settings):
    src = source_with(7)
    out = await tools.search_messages(src, settings, "адреса", "family", limit=3)
    out2 = await tools.search_messages(src, settings, "адреса", cursor=cursor_of(out), limit=3)
    assert hit_ids(out2) == [4, 3, 2] and src.search_queries[-1][1] == A.id


async def test_chat_cursor_into_another_chat(settings):
    out = await tools.search_messages(source_with(7), settings, "адреса", "family", limit=3)
    with pytest.raises(ToolError, match="another chat"):
        await tools.search_messages(source_with(7), settings, "адреса", "chess", cursor=cursor_of(out))


async def test_global_cursor_with_chat(settings):
    out = await tools.search_messages(source_with(7), settings, "адреса", limit=3)
    with pytest.raises(ToolError, match="all chats; call search_messages without chat"):
        await tools.search_messages(source_with(7), settings, "адреса", "family", cursor=cursor_of(out))


async def test_garbage_cursor(settings):
    with pytest.raises(ToolError, match="Invalid cursor. Call search_messages"):
        await tools.search_messages(source_with(1), settings, "адреса", "family", cursor="nope")


async def test_query_errors(settings):
    with pytest.raises(ToolError, match="query is empty"):
        await tools.search_messages(source_with(1), settings, "   ")
    with pytest.raises(ToolError, match="too long"):
        await tools.search_messages(source_with(1), settings, "я" * 257)


async def test_global_hit_in_unknown_chat(settings):
    src = FakeSource(dialogs=[A], messages={555: [hit(make_chat(id=555), 1, 1)]})
    out = await tools.search_messages(src, settings, "адреса")
    assert "— chat 555 —" in out and hit_ids(out) == [1]


async def test_global_from_me_pages_with_from_me_kept_in_cursor(settings):
    msgs = [hit(A, i, i, out=(i % 2 == 0)) for i in range(1, 11)]  # own: 10, 8, 6, 4, 2
    src = FakeSource(dialogs=[A], messages={A.id: msgs})
    out = await tools.search_messages(src, settings, "адреса", from_me=True, limit=3)
    assert hit_ids(out) == [10, 8, 6] and "Scanned" not in out
    out2 = await tools.search_messages(src, settings, "адреса", cursor=cursor_of(out), limit=3)
    assert hit_ids(out2) == [4, 2] and src.search_queries[-1][0].from_me  # from_me comes from the cursor


async def test_no_hits(settings):
    out = await tools.search_messages(source_with(1), settings, "паспорт", "family")
    assert "No hits." in out and "Next" not in out


async def test_ambiguous_chat(settings):
    src = FakeSource(dialogs=[make_chat(id=-1, title="Family chat"), make_chat(id=-2, title="Family news")])
    with pytest.raises(ToolError, match="ambiguous"):
        await tools.search_messages(src, settings, "адреса", "family")
