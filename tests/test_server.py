from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_media_file, make_message
from tests.test_config import CREDS
from tg_bridge.config import load_settings
from tg_bridge.server import build_server
from tg_bridge.source import Post


NOW_LINE = "Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)"


def fixed_now():
    return datetime(2026, 10, 28, 13, 3, 12, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path):
    env = {**CREDS, "TG_BRIDGE_SCOPE": "all", "TG_BRIDGE_EXPORT_DIR": str(tmp_path / "exports"), "TG_BRIDGE_DOWNLOAD_DIR": str(tmp_path / "dl")}
    return load_settings(env=env, config_file=tmp_path / "c")


def build(source, settings):
    return build_server(source, settings, now=fixed_now)


async def test_lists_tools(settings):
    tools = await build(FakeSource(), settings).list_tools()

    assert sorted(t.name for t in tools) == [
        "download_media", "export_chat", "get_chat_info", "get_comments", "get_messages", "list_chats",
        "list_folders", "now", "search_messages", "whoami",
    ]
    # export_chat and download_media write local files; every other tool changes nothing at all.
    assert all(t.annotations.read_only_hint is (t.name not in ("export_chat", "download_media")) for t in tools)


async def test_call_whoami_returns_plain_text(settings):
    result = await build(FakeSource(), settings).call_tool("whoami", {})

    assert result.is_error is False
    assert [c.text for c in result.content] == [f"{NOW_LINE}\nAnna K (@anna_k), id 42"]
    assert result.structured_content is None


async def test_call_list_chats_returns_tools_layer_text(settings):
    source = FakeSource(dialogs=[make_chat(id=-1, title="Family"), make_chat(id=-2, title="Work")])

    result = await build(source, settings).call_tool("list_chats", {"query": "family"})

    assert result.is_error is False
    assert [c.text for c in result.content] == [
        f"{NOW_LINE}\n"
        'Chats matching "family" (1 shown of 1)\n'
        "Totals: 1 supergroup · 1 private\n"
        "-1 · supergroup · Family · last 2026-10-25 13:03"
    ]


async def test_call_list_folders(settings):
    from tests.fakes import make_folder

    source = FakeSource(dialogs=[make_chat(id=-1)], folders=[make_folder(id=2, title="WAR", include_ids=(-1,))])
    server = build(source, settings)
    tool = next(t for t in await server.list_tools() if t.name == "list_folders")

    result = await server.call_tool("list_folders", {})

    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert result.content[0].text.startswith(f"{NOW_LINE}\nFolders (1)")


async def test_list_chats_tool_has_folder_param(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    assert "folder" in tools["list_chats"].input_schema["properties"]


async def test_list_chats_accepts_folder_id_as_int(settings):
    from tests.fakes import make_folder

    source = FakeSource(dialogs=[make_chat(id=-1)], folders=[make_folder(id=242, title="F1", include_ids=(-1,))])

    result = await build(source, settings).call_tool("list_chats", {"folder": 242})

    assert 'Chats in folder "F1"' in result.content[0].text


async def test_call_get_chat_info(settings):
    source = FakeSource(dialogs=[make_chat(id=-1, title="Family")])

    result = await build(source, settings).call_tool("get_chat_info", {"chat": "family"})

    assert result.content[0].text.startswith(f"{NOW_LINE}\nFamily — supergroup")


async def test_get_messages_tool_schema(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    props = tools["get_messages"].input_schema["properties"]
    assert set(props) == {
        "chat", "folder", "mode", "days", "since", "until", "period", "topic", "from_me", "cursor", "around", "limit",
    }
    assert "comments_of" not in tools["get_messages"].description
    assert "required" not in tools["get_messages"].input_schema or tools["get_messages"].input_schema["required"] == []


async def test_get_comments_tool_schema(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    tool = tools["get_comments"]
    props = tool.input_schema["properties"]
    assert set(props) == {"chat", "post", "days", "since", "until", "period", "cursor", "limit"}
    assert tool.input_schema["required"] == ["chat"]
    assert props["post"]["anyOf"][0]["type"] == "integer"
    assert tool.description.startswith("Read the comments under a channel post")
    assert "End of comments" in tool.description and "search_messages" in tool.description
    assert tool.annotations.read_only_hint is True


async def test_call_get_comments(settings):
    from datetime import UTC, datetime

    from tests.fakes import make_chat, make_message
    from tg_bridge.source import Post

    channel = make_chat(id=-1001234567890, kind="channel", title="imatrofAI", username="imatrofAI")
    when = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    post = Post(make_message(id=3186, chat_id=channel.id, date=when, text="check-in"), True, 1, -1000000000555)
    comment = make_message(id=5012, chat_id=-1000000000555, date=when, text="my bot")
    src = FakeSource(dialogs=[channel], posts={(channel.id, 3186): post}, comments={(channel.id, 3186): [comment]})

    result = await build(src, settings).call_tool("get_comments", {"chat": "imatrofAI", "post": 3186})

    text = result.content[0].text
    assert "Comments on post #3186" in text and "#5012 " in text and "End of comments on post #3186." in text


async def test_call_get_messages_folder_headlines(settings):
    from tests.fakes import make_folder, make_message

    chat = make_chat(id=-2001, kind="channel", title="F1 News")
    msg = make_message(
        id=7, chat_id=-2001, text="Norris wins the Mexican GP", sender=None, sender_title="F1 News",
        date=datetime(2026, 10, 27, 9, 0, tzinfo=UTC),
    )
    source = FakeSource(dialogs=[chat], folders=[make_folder(id=5, title="F1", include_ids=(-2001,))], messages={-2001: [msg]})

    result = await build(source, settings).call_tool(
        "get_messages", {"folder": "F1", "period": "this_week", "mode": "headlines"}
    )

    assert result.is_error is False
    text = result.content[0].text
    assert text.startswith(f"{NOW_LINE}\nFolder: F1 (5)")
    assert "F1 News: Norris wins the Mexican GP #7" in text  # no link: that chat has no username


async def test_call_get_messages(settings):
    from tests.fakes import make_message

    chat = make_chat(id=-1, title="Family")
    source = FakeSource(dialogs=[chat], messages={-1: [make_message(id=7, chat_id=-1, text="hi there")]})

    result = await build(source, settings).call_tool("get_messages", {"chat": "family"})

    assert result.is_error is False
    assert "#7 " in result.content[0].text and "hi there" in result.content[0].text


async def test_search_messages_registered_read_only(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    t = tools["search_messages"]
    assert t.annotations.read_only_hint is True
    assert set(t.input_schema["properties"]) == {
        "query", "chat", "folder", "days", "since", "until", "period", "from_me", "cursor", "limit"
    }
    assert t.input_schema["required"] == ["query"]
    assert "NOT meaning" in t.description and "around=<#id>" in t.description


async def test_export_chat_registered_as_local_file_writer(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    t = tools["export_chat"]
    a = t.annotations
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (False, False, False, False)
    assert set(t.input_schema["properties"]) == {"chat", "days", "since", "until", "period", "format", "resume"}
    assert t.input_schema["required"] == ["chat"]
    assert "NOT the messages" in t.description


async def test_download_media_registered_as_local_file_writer(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    t = tools["download_media"]
    a = t.annotations
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (False, False, False, False)
    assert set(t.input_schema["properties"]) == {"chat", "message_id"}
    assert sorted(t.input_schema["required"]) == ["chat", "message_id"]
    assert "NOT its content" in t.description and "Next: call download_media(" in t.description


async def test_now_tool_registered_read_only(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    assert tools["now"].annotations.read_only_hint is True
    assert tools["now"].input_schema["properties"] == {}


async def test_period_keywords_are_documented(settings):
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    for name in ("get_messages", "search_messages", "export_chat"):
        assert "period" in tools[name].input_schema["properties"]
        assert "this_week" in tools[name].description


MINIMAL_ARGS: dict[str, dict] = {
    "whoami": {},
    "now": {},
    "list_chats": {},
    "get_chat_info": {"chat": "family"},
    "get_messages": {"chat": "family"},
    "search_messages": {"query": "hi"},
    "export_chat": {"chat": "family"},
    "download_media": {"chat": "family", "message_id": 7},
    "list_folders": {},
    "get_comments": {"chat": "news", "post": 3},
}


def family_source(n=7, text="hi there"):
    chat = make_chat(id=-1, title="Family")
    base = datetime(2026, 10, 27, 10, 0, tzinfo=UTC)
    msgs = [make_message(id=i, chat_id=-1, date=base + timedelta(minutes=i), text=text) for i in range(1, n + 1)]
    media = {(-1, 7): (make_media_file(7, size=3), b"abc")}
    channel = make_chat(id=-1002, kind="channel", title="News")
    post = Post(make_message(id=3, chat_id=-1002, date=base, text="post"), True, 1, -1003)
    return FakeSource(
        dialogs=[chat, channel], messages={-1: msgs}, media=media, posts={(-1002, 3): post},
        comments={(-1002, 3): [make_message(id=9, chat_id=-1003, date=base, text="c")]},
    )


async def test_minimal_args_cover_every_tool(settings):
    registered = {t.name for t in await build(FakeSource(), settings).list_tools()}

    assert set(MINIMAL_ARGS) == registered


@pytest.mark.parametrize("name", sorted(MINIMAL_ARGS))
async def test_every_tool_result_starts_with_now_line(settings, name):
    result = await build(family_source(), settings).call_tool(name, MINIMAL_ARGS[name])

    assert result.content[0].text.startswith(f"{NOW_LINE}\n")


async def test_tool_error_is_stamped(settings):
    # Called directly the server raises; over the protocol the SDK turns it into an is_error result.
    with pytest.raises(ToolError, match=NOW_LINE.replace("(", r"\(").replace(")", r"\)")) as e:
        await build(family_source(), settings).call_tool("get_chat_info", {"chat": "nope"})

    assert "No chat matches" in str(e.value)


async def test_stamped_output_within_budget(settings):
    small = replace(settings, max_chars=500)
    source = family_source(300, text="long message " * 5)

    result = await build(source, small).call_tool("get_messages", {"chat": "family", "days": 3})

    text = result.content[0].text
    assert text.startswith(NOW_LINE) and len(text) <= 500


async def test_instructions_contain_today(settings):
    instructions = build(FakeSource(), settings).instructions

    assert "2026-10-28" in instructions and "Wednesday" in instructions and "now()" in instructions


async def test_rolling_periods_map_to_days(settings):
    # Smoke 08: for "last 7 days" the model picked calendar last_week and missed this week's posts.
    tools = {t.name: t for t in await build(FakeSource(), settings).list_tools()}

    for name in ("get_messages", "search_messages", "export_chat"):
        description = " ".join(tools[name].description.split())
        assert '"last 7 days" or "past week" → days=7' in description
        assert "previous calendar week" in description
        assert "previous_month" in description and "last_month" not in description


def test_build_source_public_wraps_in_scoped_source(tmp_path):
    from tg_bridge.server import build_source
    from tg_bridge.source.live import LiveSource
    from tg_bridge.source.scoped import ScopedSource

    env = {**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "s.session"), "TG_BRIDGE_SCOPE": "public"}
    source = build_source(load_settings(env=env, config_file=tmp_path / "c"))

    assert isinstance(source, ScopedSource)
    assert isinstance(source._inner, LiveSource)
    assert source._allowlist._path == tmp_path / "s.allowlist.json"


def test_build_source_all_is_the_bare_live_source(tmp_path):
    from tg_bridge.server import build_source
    from tg_bridge.source.live import LiveSource

    env = {**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "s.session"), "TG_BRIDGE_SCOPE": "all"}

    assert type(build_source(load_settings(env=env, config_file=tmp_path / "c"))) is LiveSource
