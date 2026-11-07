from dataclasses import replace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_message, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import ChatInfo, Topic
from tg_bridge.telegram.client import NOT_LOGGED_IN


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c")


class AgedSource(FakeSource):
    """FakeSource that also reports a dialog cache age and gains a chat on refresh."""

    def __init__(self, age, after_refresh=(), **kw):
        super().__init__(**kw)
        self.age = age
        self.after_refresh = list(after_refresh)

    def dialogs_age(self):
        return self.age

    async def list_dialogs(self, *, refresh=False):
        if refresh:
            self.dialogs = self.dialogs + self.after_refresh
        return await super().list_dialogs(refresh=refresh)


def family_dialogs():
    return [
        make_chat(id=-1, title="Family", username="famchat"),
        make_chat(id=-1002, kind="channel", title="Family news"),
        make_chat(id=-3, kind="group", title="Old group"),
        make_user_chat(make_person(id=7, first_name="Anna", last_name=None, username=None)),
        make_user_chat(make_person(id=8, first_name="Helper", last_name=None, username="help_bot", is_bot=True)),
    ]


async def test_whoami_with_username():
    assert await tools.whoami(FakeSource()) == "Anna K (@anna_k), id 42"


async def test_whoami_without_username():
    source = FakeSource(make_person(username=None))

    assert await tools.whoami(source) == "Anna K (no username), id 42"


async def test_not_logged_in_becomes_tool_error():
    with pytest.raises(ToolError, match="tg_bridge.login"):
        await tools.whoami(FakeSource(error=NotLoggedInError(NOT_LOGGED_IN)))


async def test_rate_limited_becomes_tool_error():
    with pytest.raises(ToolError, match="retry in 42 s"):
        await tools.whoami(FakeSource(error=RateLimitedError(42)))


async def test_list_chats_all(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings)

    lines = text.splitlines()
    assert lines[0] == "Chats (5 shown of 5)"
    assert lines[1] == "Totals: 1 user · 1 bot · 1 group · 1 supergroup · 1 channel · 1 public · 2 private"
    assert lines[2].startswith("-1 · supergroup · Family · @famchat")
    assert len(lines) == 7


async def test_list_chats_totals_cover_all_matches_not_just_shown(settings):
    dialogs = [make_chat(id=-i, kind="channel", title=f"Chat {i}", is_archived=i > 3) for i in range(1, 11)]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), settings, limit=1)

    assert text.splitlines()[:2] == ["Chats (1 shown of 10)", "Totals: 10 channel · 7 archived · 10 private"]


async def test_list_chats_public_counts_only_groups_and_channels_with_username(settings):
    dialogs = [
        make_chat(id=-1, kind="channel", title="Open", username="open"),
        make_chat(id=-2, kind="channel", title="Closed"),
        make_chat(id=-3, kind="group", title="Group"),
        make_user_chat(make_person(id=7, username="anna")),  # a person's @username doesn't make a chat public
    ]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), settings)

    assert text.splitlines()[1] == "Totals: 1 user · 1 group · 2 channel · 1 public · 2 private"


async def test_list_chats_totals_follow_filters(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings, kind="group")

    assert text.splitlines()[1] == "Totals: 1 group · 1 supergroup · 1 public · 1 private"


async def test_list_chats_puts_archived_last(settings):
    dialogs = [make_chat(id=-1, title="Old", is_archived=True), make_chat(id=-2, title="New")]

    lines = (await tools.list_chats(FakeSource(dialogs=dialogs), settings)).splitlines()

    assert lines[2].startswith("-2 ") and lines[3].startswith("-1 ")


async def test_list_chats_with_query(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings, query="family")

    assert text.splitlines()[0] == 'Chats matching "family" (2 shown of 2)'


async def test_list_chats_kind_group_includes_supergroups(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings, kind="group")

    assert text.splitlines()[0] == "Chats (2 shown of 2)"
    assert "supergroup" in text and "Old group" in text and "channel" not in text


async def test_list_chats_kind_supergroup_is_strict(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings, kind="supergroup")

    assert text.splitlines()[0] == "Chats (1 shown of 1)"


async def test_list_chats_invalid_kind(settings):
    with pytest.raises(ToolError) as exc:
        await tools.list_chats(FakeSource(), settings, kind="person")

    for allowed in ("user", "bot", "group", "supergroup", "channel"):
        assert allowed in str(exc.value)


@pytest.mark.parametrize("limit,shown", [(0, 1), (-5, 1), (500, 205), (2, 2)])
async def test_list_chats_limit_clamped(settings, limit, shown):
    dialogs = [make_chat(id=-i, title=f"Chat {i}") for i in range(1, 206)]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), settings, limit=limit)

    expected = min(shown, 200)
    assert text.splitlines()[0] == f"Chats ({expected} shown of 205)"


async def test_list_chats_empty_match(settings):
    text = await tools.list_chats(FakeSource(dialogs=family_dialogs()), settings, query="zzz")

    assert text == 'No chats match "zzz".'


async def test_list_chats_truncates_to_max_chars(tmp_path):
    small = replace(load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c"), max_chars=200)
    dialogs = [make_chat(id=-i, title=f"Chat {i}") for i in range(1, 50)]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), small)

    assert len(text) <= 200
    assert text.endswith("(output truncated: narrow with query/kind)")


async def test_list_chats_header_counts_lines_actually_shown_when_truncated(tmp_path):
    small = replace(load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c"), max_chars=300)
    dialogs = [make_chat(id=-i, title=f"Chat {i}") for i in range(1, 50)]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), small)

    lines = text.splitlines()
    chat_lines = [line for line in lines[2:] if line.startswith("-")]
    assert lines[0] == f"Chats ({len(chat_lines)} shown of 49)"
    assert 0 < len(chat_lines) < 30
    assert len(text) <= 300


async def test_list_chats_source_error_becomes_tool_error(settings):
    with pytest.raises(ToolError, match="retry in 42 s"):
        await tools.list_chats(FakeSource(error=RateLimitedError(42)), settings)


async def test_get_chat_info_by_id(settings):
    info = ChatInfo(chat=family_dialogs()[0], description="Hi", members_count=3, topics=(), topics_total=None)
    source = FakeSource(dialogs=family_dialogs(), infos={-1: info})

    text = await tools.get_chat_info(source, settings, "-1")

    assert text.startswith("Family — supergroup")
    assert "Members: 3" in text and "Description: Hi" in text


async def test_get_chat_info_by_fragment(settings):
    text = await tools.get_chat_info(FakeSource(dialogs=family_dialogs()), settings, "old gro")

    assert text.startswith("Old group — group")


async def test_get_chat_info_ambiguous(settings):
    with pytest.raises(ToolError) as exc:
        await tools.get_chat_info(FakeSource(dialogs=family_dialogs()), settings, "fam")

    assert str(exc.value).startswith('Chat "fam" is ambiguous')


async def test_get_chat_info_not_found(settings):
    with pytest.raises(ToolError) as exc:
        await tools.get_chat_info(FakeSource(dialogs=family_dialogs()), settings, "Famly")

    assert "Closest:" in str(exc.value)


async def test_resolver_refreshes_once_when_cache_is_stale(settings):
    new = make_chat(id=-9, title="Brand new")
    source = AgedSource(age=120, after_refresh=[new], dialogs=family_dialogs())

    chat = await tools.resolve_chat_arg(source, "brand new", settings)

    assert chat is new
    assert source.list_dialogs_calls == [False, True]


async def test_resolver_does_not_refresh_when_cache_is_fresh(settings):
    source = AgedSource(age=5, after_refresh=[make_chat(id=-9, title="Brand new")], dialogs=family_dialogs())

    with pytest.raises(ToolError):
        await tools.resolve_chat_arg(source, "brand new", settings)

    assert source.list_dialogs_calls == [False]


async def test_resolver_refreshes_when_age_unknown(settings):
    source = AgedSource(age=None, dialogs=family_dialogs())

    with pytest.raises(ToolError):
        await tools.resolve_chat_arg(source, "nothing here", settings)

    assert source.list_dialogs_calls == [False, True]


async def test_resolver_without_dialogs_age_never_refreshes(settings):
    source = FakeSource(dialogs=family_dialogs())

    with pytest.raises(ToolError):
        await tools.resolve_chat_arg(source, "nothing here", settings)

    assert source.list_dialogs_calls == [False]


async def test_tools_do_not_log_queries(settings, caplog):
    source = FakeSource(dialogs=family_dialogs())

    with caplog.at_level("DEBUG"):
        await tools.list_chats(source, settings, query="SecretQuery")
        with pytest.raises(ToolError):
            await tools.get_chat_info(source, settings, "SecretQuery")

    assert "SecretQuery" not in caplog.text
    assert "Family" not in caplog.text


# --- get_messages --------------------------------------------------------------

import re  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from tg_bridge.cursor import Cursor, encode_cursor  # noqa: E402

W = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=W)
GROUP = make_chat(id=-1001234567890, kind="supergroup", title="Dev Talk")
FIRST = datetime(2026, 10, 25, 6, 0, tzinfo=UTC)


def fixed_now():
    return NOW


def history(n=250, chat=GROUP, **kw):
    """n messages, 12 minutes apart, from 2026-10-25 06:00Z (local days Oct 25, 26 and 27)."""
    return [
        make_message(id=i, chat_id=chat.id, date=FIRST + timedelta(minutes=12 * (i - 1)), text=f"msg {i}", **kw)
        for i in range(1, n + 1)
    ]


def reader(**kw):
    kw.setdefault("dialogs", [GROUP])
    kw.setdefault("messages", {GROUP.id: history()})
    return FakeSource(**kw)


async def read(source, settings, **kw):
    return await tools.get_messages(source, settings, "dev talk", now=fixed_now, **kw)


def shown_ids(text):
    return [int(m) for m in re.findall(r"^#(\d+) ", text, re.MULTILINE)]


def cursor_of(text):
    return re.search(r'cursor="([^"]+)"', text).group(1)


async def test_days_2_reads_forward_from_yesterday_midnight(settings):
    source = reader()

    text = await read(source, settings, days=2)

    ids = shown_ids(text)
    assert ids == sorted(ids) and len(ids) == 100
    expected_first = next(m.id for m in source.messages[GROUP.id] if m.date >= datetime(2026, 10, 25, 23, tzinfo=UTC))
    assert ids[0] == expected_first
    assert (source.message_queries[0].direction, source.message_queries[0].limit) == ("forward", 101)
    assert "2026-10-26 00:00 → now (Europe/Warsaw)" in text.splitlines()[0]


async def test_forward_paging_round_trip(settings):
    source = reader()
    seen, text = [], await read(source, settings, days=3, limit=60)
    added = False
    while True:
        seen += shown_ids(text)
        if not added:  # a message arrives between page 1 and page 2
            source.messages[GROUP.id].append(
                make_message(id=251, chat_id=GROUP.id, date=FIRST + timedelta(days=3), text="late")
            )
            added = True
        if "End of period." in text:
            break
        assert "More messages remain; this is not the end of the period. Next: call get_messages(" in text
        text = await read(source, settings, cursor=cursor_of(text), limit=60)

    assert seen == list(range(1, 252))


async def test_latest_mode_pages_backward(settings):
    source = reader()

    page1 = await read(source, settings)
    page2 = await read(source, settings, cursor=cursor_of(page1))
    page3 = await read(source, settings, cursor=cursor_of(page2))

    assert shown_ids(page1) == list(range(151, 251)) and "Older messages remain. Next: call get_messages(" in page1
    assert shown_ids(page2) == list(range(51, 151))
    assert shown_ids(page3) == list(range(1, 51)) and page3.endswith("No older messages.")
    assert source.message_queries[0].direction == "backward"
    assert "latest messages" in page1.splitlines()[0]


async def test_period_args_ignored_with_cursor(settings):
    source = reader()
    first = await read(source, settings, days=1, limit=20)
    cursor = cursor_of(first)

    plain = await read(source, settings, cursor=cursor, limit=20)
    resent = await read(source, settings, cursor=cursor, limit=20, days=2, from_me=True, topic="zzz")

    assert resent == plain


async def test_cursor_for_other_chat_rejected(settings):
    other = make_chat(id=-1009999999999, title="Other")
    source = reader(dialogs=[GROUP, other])
    cursor = encode_cursor(Cursor(other.id, "forward", 5, None, None, None, False))

    with pytest.raises(ToolError, match="This cursor belongs to another chat"):
        await read(source, settings, cursor=cursor)


async def test_invalid_cursor_message(settings):
    with pytest.raises(ToolError, match="Invalid cursor. Call get_messages again without cursor."):
        await read(reader(), settings, cursor="garbage!")


async def test_limit_clamped(settings):
    source = reader()

    await read(source, settings, limit=0)
    await read(source, settings, limit=1000)

    assert [q.limit for q in source.message_queries] == [2, 301]


async def test_period_error_is_tool_error(settings):
    with pytest.raises(ToolError, match="days"):
        await read(reader(), settings, days=2, since="today")


async def test_get_messages_empty_period(settings):
    text = await read(reader(), settings, since="2026-11-01")

    assert text.splitlines()[-1] == "No messages in this period."
    assert "cursor" not in text


async def test_empty_latest_mode(settings):
    text = await read(reader(messages={}), settings)

    assert text.splitlines()[-1] == "No messages."


async def test_from_me_passed_through(settings):
    source = reader()

    text = await read(source, settings, from_me=True)

    assert source.message_queries[0].from_me is True
    assert "From me only" in text


FORUM = make_chat(id=-1001111111111, kind="supergroup", title="Forum", is_forum=True)
TOPICS = (Topic(12, "Releases", False), Topic(13, "Release notes", False), Topic(14, "Trips", False))


def forum_source():
    info = ChatInfo(chat=FORUM, description=None, members_count=None, topics=TOPICS, topics_total=None)
    msgs = [make_message(id=i, chat_id=FORUM.id, topic_id=14 if i % 2 else 12, text=f"t{i}") for i in range(1, 11)]
    return FakeSource(dialogs=[FORUM], infos={FORUM.id: info}, messages={FORUM.id: msgs})


async def read_forum(source, settings, **kw):
    return await tools.get_messages(source, settings, "forum", now=fixed_now, **kw)


async def test_topic_numeric(settings):
    source = forum_source()

    for topic in (14, "14", "#14"):
        await read_forum(source, settings, topic=topic)

    assert [q.topic_id for q in source.message_queries] == [14, 14, 14]


async def test_topic_by_title_fragment(settings):
    source = forum_source()

    text = await read_forum(source, settings, topic="trips")

    assert source.message_queries[0].topic_id == 14
    assert "Topic: #14 Trips" in text


async def test_topic_exact_title_wins_over_longer_match(settings):
    source = forum_source()

    await read_forum(source, settings, topic="releases")

    assert source.message_queries[0].topic_id == 12


async def test_topic_ambiguous_lists_candidates(settings):
    with pytest.raises(ToolError) as exc:
        await read_forum(forum_source(), settings, topic="release")

    assert "#12 Releases" in str(exc.value) and "#13 Release notes" in str(exc.value)


async def test_topic_unknown_lists_topics(settings):
    with pytest.raises(ToolError) as exc:
        await read_forum(forum_source(), settings, topic="nothing")

    assert "#14 Trips" in str(exc.value)


async def test_topic_on_non_forum_rejected(settings):
    with pytest.raises(ToolError, match='"Dev Talk" is not a forum; omit topic.'):
        await read(reader(), settings, topic="x")


class FailingReads(FakeSource):
    async def get_messages(self, chat, query):
        raise RateLimitedError(45)


async def test_rate_limited_is_readable(settings):
    source = FailingReads(dialogs=[GROUP])

    with pytest.raises(ToolError, match=r"Telegram rate limit: retry in 45 s"):
        await read(source, settings)


@pytest.mark.parametrize("max_chars", [300, 2000, 12000])
@pytest.mark.parametrize("period", [{"days": 3}, {}])
async def test_output_never_exceeds_budget(tmp_path, max_chars, period):
    small = replace(load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c"), max_chars=max_chars)
    source = reader(messages={GROUP.id: history()})
    first = source.messages[GROUP.id][0]  # a huge message at the start of the forward read
    source.messages[GROUP.id][0] = make_message(id=1, chat_id=GROUP.id, date=first.date, text="x" * 1400)

    text = await read(source, small, limit=300, **period)

    assert len(text) <= max_chars
    assert shown_ids(text)


async def test_get_messages_period_keyword(settings):
    source = reader()

    await read(source, settings, period="last_week")

    q = source.message_queries[0]
    assert (q.start, q.end) == (datetime(2026, 10, 18, 22, tzinfo=UTC), datetime(2026, 10, 25, 23, tzinfo=UTC))


async def test_period_and_days_is_tool_error(settings):
    with pytest.raises(ToolError, match="Use one of `period`"):
        await read(reader(), settings, period="today", days=2)


def test_now_tool_body(settings):
    out = tools.now(settings, datetime(2026, 10, 28, 13, 3, tzinfo=UTC))

    assert out.splitlines()[0] == "Today: Wednesday 2026-10-28 · ISO week 2026-W44 · UTC+01:00"
    assert len(out.splitlines()) == 5
