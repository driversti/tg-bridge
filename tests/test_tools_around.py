import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import FakeSource, make_chat, make_message
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.format import render_around

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CHAT = make_chat(id=-1, title="Family", username="fam")
W = ZoneInfo("Europe/Warsaw")


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


def source(ids=range(1, 51)):
    msgs = [make_message(id=i, chat_id=-1, date=T0 + timedelta(minutes=i), text=f"msg {i}") for i in ids]
    return FakeSource(dialogs=[CHAT], messages={-1: msgs})


def shown_ids(out):
    return [int(m) for m in re.findall(r"^▶?#(\d+) ", out, re.M)]


def cursors(out):
    return dict(re.findall(r'(Older|Newer): call get_messages\(chat="-1", cursor="([^"]+)"\)', out))


async def test_around_centres_on_target(settings):
    src = source()
    out = await tools.get_messages(src, settings, "family", around=25)
    assert shown_ids(out) == list(range(16, 36))
    assert "▶#25 " in out and "▶#24" not in out
    assert "around #25" in out and "Shown 20 messages around #25" in out
    assert set(cursors(out)) == {"Older", "Newer"}
    back, fwd = src.message_queries
    assert (back.direction, back.boundary_id, back.limit) == ("backward", 26, 11)
    assert (fwd.direction, fwd.boundary_id, fwd.limit) == ("forward", 25, 11)


async def test_newer_cursor_continues_forward_and_older_backward(settings):
    src = source()
    out = await tools.get_messages(src, settings, "family", around=25)
    c = cursors(out)
    newer = await tools.get_messages(src, settings, "family", cursor=c["Newer"], limit=3)
    assert shown_ids(newer) == [36, 37, 38]
    older = await tools.get_messages(src, settings, "family", cursor=c["Older"], limit=3)
    assert shown_ids(older) == [13, 14, 15]


async def test_around_first_message_has_no_older(settings):
    out = await tools.get_messages(source(), settings, "family", around=1)
    assert shown_ids(out)[0] == 1 and "▶#1 " in out
    assert set(cursors(out)) == {"Newer"}


async def test_around_last_message_has_no_newer(settings):
    out = await tools.get_messages(source(), settings, "family", around=50)
    assert shown_ids(out)[-1] == 50 and set(cursors(out)) == {"Older"}


async def test_around_missing_id_says_not_found(settings):
    out = await tools.get_messages(source([i for i in range(1, 51) if i != 25]), settings, "family", around=25)
    assert "around #25 (not found; nearest shown)" in out and "▶" not in out
    assert 24 in shown_ids(out) and 26 in shown_ids(out)


async def test_around_ignores_period_and_topic(settings):
    src = source()
    out = await tools.get_messages(src, settings, "family", around=25, days=1, since="2020-01-01", topic="nope", from_me=True)
    assert "▶#25 " in out
    assert all(q.start is None and q.end is None and q.topic_id is None and not q.from_me for q in src.message_queries)


async def test_around_empty_chat(settings):
    out = await tools.get_messages(source([]), settings, "family", around=5)
    assert out.endswith("No messages.")


async def test_around_budget_trims_far_ends_keeps_target(settings):
    src = source()
    full = await tools.get_messages(src, settings, "family", around=25)
    s = replace(settings, max_chars=len(full) // 2)
    out = await tools.get_messages(source(), s, "family", around=25)
    ids = shown_ids(out)
    assert len(out) <= len(full) // 2 and 25 in ids and len(ids) < 20
    assert abs((ids.index(25)) - (len(ids) - 1 - ids.index(25))) <= 1  # trimmed evenly
    assert set(cursors(out)) == {"Older", "Newer"}


async def test_cursor_wins_over_around(settings):
    src = source()
    out = await tools.get_messages(src, settings, "family", around=25, limit=5)
    cur = cursors(out)["Newer"]
    src2 = source()
    res = await tools.get_messages(src2, settings, "family", around=1, cursor=cur, limit=2)
    assert "▶" not in res and len(src2.message_queries) == 1


async def test_default_limit_still_100_without_around(settings):
    src = source(range(1, 151))
    out = await tools.get_messages(src, settings, "family")
    assert src.message_queries[0].limit == 101 and len(shown_ids(out)) == 100


def test_render_around_single_huge_target_is_cut():
    msg = make_message(id=5, chat_id=-1, date=T0, text="я" * 5000)
    out = render_around(
        "HEAD", [msg], 5, tz=W, max_chars=300, more_older=True, more_newer=True,
        older_hint=lambda m: "call get_messages(older)", newer_hint=lambda m: "call get_messages(newer)",
    )
    assert len(out) <= 300 and "▶#5 " in out
