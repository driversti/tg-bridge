import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import ChatInfo
from tg_bridge.telegram.client import NOT_LOGGED_IN


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


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
    small = load_settings(env={**CREDS, "TG_BRIDGE_MAX_CHARS": "200"}, config_file=tmp_path / "c")
    dialogs = [make_chat(id=-i, title=f"Chat {i}") for i in range(1, 50)]

    text = await tools.list_chats(FakeSource(dialogs=dialogs), small)

    assert len(text) <= 200
    assert text.endswith("(output truncated: narrow with query/kind)")


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
