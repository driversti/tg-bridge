from dataclasses import replace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_folder, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.errors import RateLimitedError


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={**CREDS, "TG_BRIDGE_SCOPE": "all"}, config_file=tmp_path / "c")


def channels(first: int, count: int, unread: int = 0):
    """`count` channels with ids -(first)…; the first `unread` of them unread."""
    return [make_chat(id=-first - i, kind="channel", title=f"C{first + i}", is_unread=i < unread) for i in range(count)]


def example_source() -> FakeSource:
    """Dialogs and folders that reproduce the plan's four example lines."""
    war = channels(1000, 34, unread=7)  # folder 2: 30 included + 4 pinned
    shared = channels(2000, 18, unread=3)  # folder 5
    rooms = [make_chat(id=-3000 - i, title=f"G{i}") for i in range(19)]  # 18 matched by rule + 1 excluded
    side = channels(4000, 2)  # folder 4: the 2 included ones
    extra = [make_user_chat(make_person(id=10 + i), is_unread=True) for i in range(2)]  # folder 3 gets 7+3+2 unread
    folders = [
        make_folder(
            id=2, title="WAR", emoticon="🪖", pinned_ids=tuple(c.id for c in war[:4]), include_ids=tuple(c.id for c in war[4:])
        ),
        make_folder(
            id=3, title="Unread", contacts=True, non_contacts=True, groups=True, broadcasts=True, bots=True,
            exclude_read=True, exclude_archived=True,
        ),
        make_folder(
            id=4, title="Work", groups=True, exclude_muted=True,
            include_ids=tuple(c.id for c in side), exclude_ids=(rooms[-1].id,),
        ),
        make_folder(id=5, title="F1", kind="shared", include_ids=tuple(c.id for c in shared)),
    ]
    return FakeSource(dialogs=war + shared + rooms + side + extra, folders=folders)


async def test_list_folders_example_output(settings):
    source = example_source()
    text = await tools.list_folders(source, settings)

    assert text == (
        "Folders (4)\n"
        "2 · WAR 🪖 · custom · 34 chats · 7 unread · 30 included, 4 pinned\n"
        "3 · Unread · custom · 12 chats · 12 unread · rules: contacts+non-contacts+groups+channels+bots, excl. read+archived\n"
        "4 · Work · custom · 20 chats · 0 unread · rules: groups, excl. muted · 2 included, 1 excluded\n"
        "5 · F1 · shared · 18 chats · 3 unread · 18 included"
    )


async def test_list_folders_none(settings):
    assert await tools.list_folders(FakeSource(), settings) == "No chat folders."


async def test_list_folders_respects_max_chars(settings):
    small = replace(settings, max_chars=500)
    folders = [make_folder(id=i, title=f"Folder {i}", groups=True) for i in range(2, 42)]

    text = await tools.list_folders(FakeSource(folders=folders), small)

    assert len(text) <= 500
    assert text.endswith("(output truncated: narrow with query/kind)")


async def test_list_folders_source_error_becomes_tool_error(settings):
    with pytest.raises(ToolError, match="retry in 42 s"):
        await tools.list_folders(FakeSource(error=RateLimitedError(42)), settings)


def war_source(**folder_kw) -> FakeSource:
    chats = [
        make_chat(id=-1, title="Old pinned", last_message_at=None),
        make_chat(id=-2, kind="channel", title="Newest", is_archived=True),
        make_chat(id=-3, title="Middle"),
        make_chat(id=-4, title="Outside"),
    ]
    chats[2] = make_chat(id=-3, title="Middle", last_message_at=chats[1].last_message_at.replace(day=20))
    folder = make_folder(title="WAR", pinned_ids=(-1,), include_ids=(-2, -3), **folder_kw)
    return FakeSource(dialogs=chats, folders=[folder])


async def test_list_chats_in_folder_order_pinned_first_archived_in_place(settings):
    text = await tools.list_chats(war_source(), settings, folder="war")

    lines = text.splitlines()
    assert lines[0] == 'Chats in folder "WAR" (3 shown of 3)'
    assert lines[1].startswith("Totals:")
    assert [line.split(" · ")[0] for line in lines[2:]] == ["-1", "-2", "-3"]
    assert lines[3].endswith("archived")  # not moved to the end
    assert "Outside" not in text


async def test_list_chats_folder_by_id(settings):
    text = await tools.list_chats(war_source(), settings, folder="2")

    assert text.splitlines()[0].startswith('Chats in folder "WAR"')


async def test_list_chats_folder_with_kind_query_and_limit(settings):
    source = war_source()

    by_kind = await tools.list_chats(source, settings, folder="WAR", kind="channel")
    by_query = await tools.list_chats(source, settings, folder="WAR", query="mid")
    limited = await tools.list_chats(source, settings, folder="WAR", limit=1)

    assert by_kind.splitlines()[0] == 'Chats in folder "WAR" (1 shown of 1)'
    assert by_query.splitlines()[0] == 'Chats in folder "WAR" matching "mid" (1 shown of 1)'
    assert limited.splitlines()[0] == 'Chats in folder "WAR" (1 shown of 3)'
    assert len(limited.splitlines()) == 3


async def test_list_chats_empty_folder(settings):
    source = FakeSource(dialogs=[make_chat()], folders=[make_folder(title="WAR")])

    assert await tools.list_chats(source, settings, folder="WAR") == 'Folder "WAR" has no chats.'


async def test_list_chats_folder_filters_match_nothing(settings):
    text = await tools.list_chats(war_source(), settings, folder="WAR", query="zzz")

    assert text == 'No chats in folder "WAR" match "zzz".'


async def test_list_chats_unknown_folder_lists_folders(settings):
    source = FakeSource(dialogs=[], folders=[make_folder(id=2, title="WAR"), make_folder(id=3, title="Work")])

    with pytest.raises(ToolError) as exc:
        await tools.list_chats(source, settings, folder="nope")

    text = str(exc.value)
    assert text.startswith('No folder matches "nope"')
    assert "2 · WAR" in text and "3 · Work" in text


async def test_list_chats_unknown_folder_without_any_folders(settings):
    with pytest.raises(ToolError, match="no chat folders"):
        await tools.list_chats(FakeSource(), settings, folder="nope")


async def test_list_chats_ambiguous_folder_lists_candidates(settings):
    source = FakeSource(folders=[make_folder(id=2, title="Work chats"), make_folder(id=3, title="Work news")])

    with pytest.raises(ToolError) as exc:
        await tools.list_chats(source, settings, folder="work")

    text = str(exc.value)
    assert text.startswith('Folder "work" is ambiguous')
    assert "2 · Work chats" in text and "3 · Work news" in text


async def test_list_chats_without_folder_does_not_touch_folders(settings):
    source = FakeSource(dialogs=[make_chat()])

    await tools.list_chats(source, settings)

    assert source.list_folders_calls == []


# --- list_chats(min_folders=) ----------------------------------------------------


def overlap_source() -> FakeSource:
    """-1 in three folders, -2 in two, -3 in one, -4 in none."""
    chats = [make_chat(id=-i, kind="channel", title=f"C{i}") for i in range(1, 5)]
    folders = [
        make_folder(id=2, title="WAR", emoticon="🪖", include_ids=(-2, -1)),
        make_folder(id=3, title="News", include_ids=(-1, -2, -3)),
        make_folder(id=4, title="F1", include_ids=(-1,)),
    ]
    return FakeSource(dialogs=chats, folders=folders)


async def test_min_folders_lists_overlaps_with_their_folders_most_first(settings):
    text = await tools.list_chats(overlap_source(), settings, min_folders=2)

    lines = text.splitlines()
    assert lines[0] == "Chats in 2+ folders (2 shown of 2)"
    assert lines[1].startswith("Totals: 2 channel")
    assert lines[2].startswith("-1 · channel · C1") and lines[2].endswith(" · folders: WAR, News, F1")
    assert lines[3].startswith("-2 · channel · C2") and lines[3].endswith(" · folders: WAR, News")


async def test_min_folders_one_means_in_any_folder(settings):
    text = await tools.list_chats(overlap_source(), settings, min_folders=1)

    assert text.splitlines()[0] == "Chats in 1+ folders (3 shown of 3)"
    assert "C4" not in text


async def test_min_folders_combines_with_folder_and_query(settings):
    source = overlap_source()

    in_news = await tools.list_chats(source, settings, folder="News", min_folders=2)
    by_query = await tools.list_chats(source, settings, query="c2", min_folders=2)

    assert in_news.splitlines()[0] == 'Chats in folder "News", in 2+ folders (2 shown of 2)'
    assert by_query.splitlines()[0] == 'Chats matching "c2", in 2+ folders (1 shown of 1)'


async def test_min_folders_none_match(settings):
    text = await tools.list_chats(overlap_source(), settings, min_folders=4)

    assert text == "No chats are in 4+ folders."


async def test_min_folders_below_one_rejected(settings):
    with pytest.raises(ToolError, match="min_folders"):
        await tools.list_chats(overlap_source(), settings, min_folders=0)


async def test_min_folders_reads_folders_once(settings):
    source = overlap_source()

    await tools.list_chats(source, settings, folder="News", min_folders=2)

    assert source.list_folders_calls == [False]


# --- get_chat_info -------------------------------------------------------------


async def test_chat_info_lists_folders(settings):
    chat = make_chat(id=-1, title="Family")
    folders = [
        make_folder(id=2, title="WAR", include_ids=(-1,)),
        make_folder(id=3, title="Other"),
        make_folder(id=5, title="F1", kind="shared", include_ids=(-1,)),
    ]

    text = await tools.get_chat_info(FakeSource(dialogs=[chat], folders=folders), settings, "-1")

    assert "Folders: WAR, F1" in text.splitlines()


async def test_chat_info_in_no_folder(settings):
    text = await tools.get_chat_info(FakeSource(dialogs=[make_chat(id=-1)], folders=[make_folder()]), settings, "-1")

    assert "Folders: none" in text.splitlines()


async def test_chat_info_omits_folders_line_when_they_cannot_load(settings):
    class NoFolders(FakeSource):
        async def list_folders(self, *, refresh=False):
            raise RateLimitedError(5)

    text = await tools.get_chat_info(NoFolders(dialogs=[make_chat(id=-1)]), settings, "-1")

    assert "Folders:" not in text
    assert text.startswith("Family — supergroup")
