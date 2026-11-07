"""Real tools over ScopedSource(FakeSource): a hidden chat must not exist for any of them."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_folder, make_media_file, make_message, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.cursor import Cursor, ReadCursor, encode_cursor, encode_read_cursor
from tg_bridge.errors import TgBridgeError
from tg_bridge.source import Post, SearchOffset
from tg_bridge.source.allowlist import Allowlist
from tg_bridge.source.scoped import ScopedSource

NOTE = (
    "This bridge runs in public scope: private chats (people, bots, private groups and channels) are available "
    "only when the owner has switched them on."
)
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOW = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # noqa: E731

ANNA = make_person(id=42, first_name="Anna", last_name="K", username="anna_k")
DM = make_user_chat(ANNA)
BOT = make_user_chat(make_person(id=77, first_name="Helper", last_name="Bot", username="helper_bot", is_bot=True))
PUB = make_chat(id=-1001000000001, kind="channel", title="News Channel", username="newschan")
OFF = make_chat(id=-1003, kind="group", title="Secret Club", username=None)
ON = make_chat(id=-1004, kind="group", title="Open Club", username=None)
DUP_HIDDEN = make_chat(id=-1005, kind="group", title="Weekly Sync", username=None)
DUP_VISIBLE = make_chat(id=-1001000000006, kind="supergroup", title="Weekly Sync", username="weekly")
ALL = [DM, BOT, PUB, OFF, ON, DUP_HIDDEN, DUP_VISIBLE]


def msg(chat, id, day=0, text="needle here", **kw):
    # No sender: the default sender is "Anna K", which would make the no-leak checks meaningless.
    fields = dict(sender=None, sender_title="poster")
    fields.update(kw)
    return make_message(id=id, chat_id=chat.id, date=T0 + timedelta(days=day, minutes=id), text=text, **fields)


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")  # scope defaults to public


@pytest.fixture
def inner():
    folder = make_folder(id=5, title="Mixed", include_ids=tuple(c.id for c in ALL))
    messages = {c.id: [msg(c, 1, text=f"needle {c.id}", out=True), msg(c, 2, text="other")] for c in ALL}
    comments_post = Post(message=msg(PUB, 9), comments_enabled=True, comment_count=1, discussion_id=-1006)
    return FakeSource(
        dialogs=ALL,
        folders=[folder],
        messages={**messages, PUB.id: messages[PUB.id] + [msg(PUB, 9)]},
        media={(DM.id, 1): (make_media_file(1), b"abcd")},
        posts={(PUB.id, 9): comments_post},
        comments={(PUB.id, 9): [make_message(id=100, chat_id=-1006, text="a comment", date=T0, sender=None, sender_title="x")]},
    )


@pytest.fixture
def source(inner, tmp_path):
    allow = Allowlist(tmp_path / "a.json")
    allow.set(ON.id, True)
    return ScopedSource(inner, allow)


def no_leak(text):
    for secret in ("Anna", "anna_k", "Helper", "helper_bot", "Secret Club", "needle 42", "needle 77", "needle -1003"):
        assert secret not in text, secret


async def test_list_chats_shows_only_visible_and_note(source, settings):
    out = await tools.list_chats(source, settings)
    assert "News Channel" in out and "Open Club" in out and "Weekly Sync" in out
    no_leak(out)
    assert out.rstrip().endswith(NOTE)
    assert out.count("Weekly Sync") == 1


async def test_list_chats_kind_user_is_empty_with_note(source, settings):
    out = await tools.list_chats(source, settings, kind="user")
    assert out.startswith("No chats") and out.rstrip().endswith(NOTE)


async def test_scope_all_has_no_note(inner, tmp_path):
    s = load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c")
    out = await tools.list_chats(inner, s)
    assert "public scope" not in out and "Anna K" in out


@pytest.mark.parametrize("arg", ["Anna", "42", "@anna_k", "Anna K", "Helper", "77", "Secret", "-1003"])
async def test_hidden_chat_is_not_found_with_note_and_no_suggestions(source, settings, arg):
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source, settings, chat=arg, period="this_week", now=NOW)
    text = str(e.value)
    assert text.startswith(f'No chat matches "{arg}".')
    assert NOTE in text
    echoed = text.replace(f'"{arg}"', "")
    no_leak(echoed)


@pytest.mark.parametrize("arg", ["Anna", "Secret Club", "@anna_k", "-1003", "77"])
async def test_not_found_for_hidden_equals_not_found_for_nonexistent(source, settings, tmp_path, arg):
    # Same dialogs minus the hidden ones: what the model sees must be identical.
    visible_only = FakeSource(dialogs=[PUB, ON, DUP_VISIBLE])
    plain = ScopedSource(visible_only, Allowlist(tmp_path / "other.json"))
    plain._allowlist.set(ON.id, True)

    async def text_of(src):
        with pytest.raises(ToolError) as e:
            await tools.get_chat_info(src, settings, arg)
        return str(e.value)

    assert await text_of(source) == await text_of(plain)


async def test_suggestions_never_name_hidden_chats(source, settings):
    with pytest.raises(ToolError) as e:
        await tools.get_chat_info(source, settings, "Secret Clubs")
    no_leak(str(e.value).replace("Secret Clubs", ""))


async def test_ambiguous_name_resolves_to_visible(source, settings):
    out = await tools.get_chat_info(source, settings, "Weekly Sync")
    assert str(DUP_VISIBLE.id) in out


async def test_folder_counts_only_visible(source, settings):
    out = await tools.list_folders(source, settings)
    assert "3 chats" in out


async def test_list_chats_folder_counts_only_visible(source, settings):
    out = await tools.list_chats(source, settings, folder="Mixed")
    assert "(3 shown of 3)" in out
    no_leak(out)


async def test_get_messages_folder_never_shows_hidden(source, settings):
    out = await tools.get_messages(source, settings, folder="Mixed", since="2026-01-01", now=NOW)
    assert "3 chats" in out
    no_leak(out)
    assert "needle -1001000000001" in out


async def test_search_folder_never_shows_hidden(source, settings):
    out = await tools.search_messages(source, settings, "needle", folder="Mixed", since="2026-01-01", now=NOW)
    no_leak(out)
    assert "needle" in out


async def test_global_search_only_visible_hits(source, settings):
    out = await tools.search_messages(source, settings, "needle", since="2026-01-01", now=NOW)
    no_leak(out)
    assert f"needle {PUB.id}" in out and f"needle {ON.id}" in out


async def test_global_search_from_me_only_visible_hits(source, settings):
    out = await tools.search_messages(source, settings, "needle", from_me=True, since="2026-01-01", now=NOW)
    no_leak(out)
    assert f"needle {PUB.id}" in out


async def test_global_search_page_is_topped_up_past_hidden_hits(inner, tmp_path, settings):
    # Many newer hits in the hidden DM must not push the visible ones off the first page.
    inner.messages[DM.id] = [msg(DM, i, day=5, text="needle dm") for i in range(1, 10)]
    scoped = ScopedSource(inner, Allowlist(tmp_path / "a.json"))
    out = await tools.search_messages(scoped, settings, "needle", limit=3, since="2026-01-01", now=NOW)
    assert "needle dm" not in out
    assert out.count("needle -100") >= 2


@pytest.mark.parametrize("call", ["info", "export", "media"])
async def test_info_export_media_on_hidden_dm_not_found(source, settings, call):
    with pytest.raises(ToolError) as e:
        if call == "info":
            await tools.get_chat_info(source, settings, "Anna K")
        elif call == "export":
            await tools.export_chat(source, settings, "Anna K", since="2026-01-01", now=NOW)
        else:
            await tools.download_media(source, settings, "Anna K", 1)
    assert str(e.value).startswith('No chat matches "Anna K"')
    assert NOTE in str(e.value)


async def test_plain_cursor_for_hidden_chat_never_reads_it(source, inner, settings):
    c = Cursor(DM.id, "backward", 10, None, None, None, False)
    with pytest.raises(ToolError):
        await tools.get_messages(source, settings, chat="News Channel", cursor=encode_cursor(c))
    with pytest.raises(ToolError):
        await tools.get_messages(source, settings, chat="Anna", cursor=encode_cursor(c))
    assert inner.message_queries == []


async def test_read_cursor_for_hidden_chat_not_found(source, settings):
    rc = ReadCursor(SearchOffset(DM.id, 1, T0), T0, T0 + timedelta(days=30), None, False, False)
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source, settings, cursor=encode_read_cursor(rc), now=NOW)
    assert NOTE in str(e.value)
    no_leak(str(e.value))


async def test_read_cursor_for_hidden_chat_via_folder_cursor_not_listed(source, settings):
    rc = ReadCursor(SearchOffset(DM.id, 1, T0), T0, T0 + timedelta(days=30), 5, False, False)
    out = await tools.get_messages(source, settings, cursor=encode_read_cursor(rc), now=NOW)
    no_leak(out)


async def test_comments_of_public_channel_work(source, settings):
    out = await tools.get_comments(source, settings, "News Channel", 9, since="2026-01-01", now=NOW)
    assert "a comment" in out


async def test_get_messages_of_visible_private_group_works(source, settings):
    out = await tools.get_messages(source, settings, chat="Open Club", since="2026-01-01", now=NOW)
    assert "other" in out


async def test_scope_note_stays_within_budget(tmp_path):
    # The note is added after rendering, so the rendering must leave room for it.
    s = load_settings(env={**CREDS, "TG_BRIDGE_MAX_CHARS": "500"}, config_file=tmp_path / "c")
    many = [make_chat(id=-1001000000100 - i, kind="channel", title=f"Public channel number {i}", username=f"pub{i}")
            for i in range(60)]
    src = ScopedSource(FakeSource(dialogs=many), Allowlist(tmp_path / "a.json"))
    listed = await tools.list_chats(src, s, limit=60)
    assert NOTE in listed and len(listed) <= 500
    with pytest.raises(ToolError) as e:
        await tools.get_chat_info(src, s, "Public channel numbr")  # not found, close matches → suggestion list
    assert NOTE in str(e.value) and len(str(e.value)) <= 500


async def test_switched_on_dm_is_readable_and_a_second_dm_stays_hidden(inner, settings, tmp_path):
    allow = Allowlist(tmp_path / "dm.json")
    allow.set(DM.id, True)
    src = ScopedSource(inner, allow)
    out = await tools.get_messages(src, settings, chat="Anna", since="2026-01-01", now=NOW)
    assert "other" in out
    with pytest.raises(ToolError) as e:
        await tools.get_messages(src, settings, chat="Helper", since="2026-01-01", now=NOW)
    assert str(e.value).startswith('No chat matches "Helper".')
    assert "helper_bot" not in (await tools.list_chats(src, settings))
