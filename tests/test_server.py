import pytest

from tests.fakes import FakeSource, make_chat
from tests.test_config import CREDS
from tg_bridge.config import load_settings
from tg_bridge.server import build_server


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


async def test_lists_tools(settings):
    tools = await build_server(FakeSource(), settings).list_tools()

    assert sorted(t.name for t in tools) == ["download_media", "export_chat", "get_chat_info", "get_messages", "list_chats", "search_messages", "whoami"]
    # export_chat and download_media write local files; every other tool changes nothing at all.
    assert all(t.annotations.read_only_hint is (t.name not in ("export_chat", "download_media")) for t in tools)


async def test_call_whoami_returns_plain_text(settings):
    result = await build_server(FakeSource(), settings).call_tool("whoami", {})

    assert result.is_error is False
    assert [c.text for c in result.content] == ["Anna K (@anna_k), id 42"]
    assert result.structured_content is None


async def test_call_list_chats_returns_tools_layer_text(settings):
    source = FakeSource(dialogs=[make_chat(id=-1, title="Family"), make_chat(id=-2, title="Work")])

    result = await build_server(source, settings).call_tool("list_chats", {"query": "family"})

    assert result.is_error is False
    assert [c.text for c in result.content] == [
        'Chats matching "family" (1 shown of 1)\n'
        "Totals: 1 supergroup · 1 private\n"
        "-1 · supergroup · Family · last 2026-10-25 13:03"
    ]


async def test_call_get_chat_info(settings):
    source = FakeSource(dialogs=[make_chat(id=-1, title="Family")])

    result = await build_server(source, settings).call_tool("get_chat_info", {"chat": "family"})

    assert result.content[0].text.startswith("Family — supergroup")


async def test_get_messages_tool_schema(settings):
    tools = {t.name: t for t in await build_server(FakeSource(), settings).list_tools()}

    props = tools["get_messages"].input_schema["properties"]
    assert set(props) == {"chat", "days", "since", "until", "topic", "from_me", "cursor", "around", "limit"}
    assert tools["get_messages"].input_schema["required"] == ["chat"]


async def test_call_get_messages(settings):
    from tests.fakes import make_message

    chat = make_chat(id=-1, title="Family")
    source = FakeSource(dialogs=[chat], messages={-1: [make_message(id=7, chat_id=-1, text="hi there")]})

    result = await build_server(source, settings).call_tool("get_messages", {"chat": "family"})

    assert result.is_error is False
    assert "#7 " in result.content[0].text and "hi there" in result.content[0].text


async def test_search_messages_registered_read_only(settings):
    tools = {t.name: t for t in await build_server(FakeSource(), settings).list_tools()}

    t = tools["search_messages"]
    assert t.annotations.read_only_hint is True
    assert set(t.input_schema["properties"]) == {"query", "chat", "days", "since", "until", "from_me", "cursor", "limit"}
    assert t.input_schema["required"] == ["query"]
    assert "NOT meaning" in t.description and "around=<#id>" in t.description


async def test_export_chat_registered_as_local_file_writer(settings):
    tools = {t.name: t for t in await build_server(FakeSource(), settings).list_tools()}

    t = tools["export_chat"]
    a = t.annotations
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (False, False, False, False)
    assert set(t.input_schema["properties"]) == {"chat", "days", "since", "until", "format", "resume"}
    assert t.input_schema["required"] == ["chat"]
    assert "NOT the messages" in t.description


async def test_download_media_registered_as_local_file_writer(settings):
    tools = {t.name: t for t in await build_server(FakeSource(), settings).list_tools()}

    t = tools["download_media"]
    a = t.annotations
    assert (a.read_only_hint, a.destructive_hint, a.idempotent_hint, a.open_world_hint) == (False, False, False, False)
    assert set(t.input_schema["properties"]) == {"chat", "message_id"}
    assert sorted(t.input_schema["required"]) == ["chat", "message_id"]
    assert "NOT its content" in t.description and "Next: call download_media(" in t.description
