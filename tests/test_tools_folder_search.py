import re
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_folder, make_message
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
NOW = lambda: datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # noqa: E731
A, B, C, OUT = -2001, -2002, -2003, -2009
TITLES = {A: "F1 News", B: "Motorsport UA", C: "P1", OUT: "Elsewhere"}


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


def hit(chat, id, day, text="Norris wins", **kw):
    fields = dict(sender=None, sender_title=TITLES[chat])
    fields.update(kw)
    return make_message(id=id, chat_id=chat, date=T0 + timedelta(days=day), text=text, **fields)


def make_source(**messages):
    dialogs = [make_chat(id=cid, kind="channel", title=title) for cid, title in TITLES.items()]
    folders = [
        make_folder(id=5, title="F1", kind="shared", include_ids=(A, B, C)),
        make_folder(id=6, title="Other", include_ids=(OUT,)),
    ]
    return FakeSource(dialogs=dialogs, folders=folders, messages={k: v for k, v in messages.items()} or None)


def source_with(*hits):
    by_chat: dict[int, list] = {}
    for h in hits:
        by_chat.setdefault(h.chat_id, []).append(h)
    src = make_source()
    src.messages = by_chat
    return src


def hit_ids(text):
    return [int(m) for m in re.findall(r"^#(\d+) ", text, re.M)]


def cursor_of(text):
    return re.search(r'cursor="([^"]+)"', text).group(1)


async def test_chat_and_folder_together(settings):
    with pytest.raises(ToolError) as e:
        await tools.search_messages(make_source(), settings, "norris", "F1 News", folder="F1")
    assert str(e.value) == "Pass either chat or folder, not both."


async def test_folder_errors(settings):
    big = [make_chat(id=-3000 - i, kind="channel", title=f"C{i}") for i in range(51)]
    src = FakeSource(
        dialogs=big,
        folders=[make_folder(id=5, title="F1", include_ids=tuple(c.id for c in big)), make_folder(id=6, title="E")],
    )
    with pytest.raises(ToolError) as e:
        await tools.search_messages(src, settings, "x", folder="F1")
    assert str(e.value) == (
        'Folder "F1" has 51 chats; folder queries handle up to 50. Use a smaller folder, or search without folder.'
    )
    with pytest.raises(ToolError) as e:
        await tools.search_messages(src, settings, "x", folder="E")
    assert str(e.value) == 'Folder "E" has no chats.'
    with pytest.raises(ToolError, match='No folder matches "nope"'):
        await tools.search_messages(src, settings, "x", folder="nope")


async def test_folder_search_groups_by_chat_newest_first(settings):
    src = source_with(hit(A, 1, 1), hit(A, 2, 5), hit(B, 7, 3), hit(OUT, 9, 4), hit(C, 3, 2, text="no match"))

    out = await tools.search_messages(src, settings, "norris", folder="F1")

    lines = out.splitlines()
    assert lines[0] == 'Search "norris" in folder "F1" (3 chats) · all time (Europe/Warsaw)'
    assert hit_ids(out) == [2, 1, 7]  # grouped by chat: F1 News (newest first), then Motorsport UA
    assert "Elsewhere" not in out and "#9 " not in out
    assert "— F1 News (-2001) —" in lines and "— Motorsport UA (-2002) —" in lines
    assert "No more hits." in out
    queries = [(q, cid) for q, cid in src.search_queries]
    assert [cid for _, cid in queries] == [A, B, C]  # one per member, none global
    assert all(q.limit == 31 and q.after is None for q, _ in queries)


async def test_folder_search_paging_covers_every_hit_once(settings):
    src = source_with(*[hit(A, i, i) for i in range(1, 6)], hit(B, 1, 2), hit(B, 2, 8), hit(C, 1, 1, text="Norris"))
    expected = sorted(
        [(h.date, h.chat_id, h.id) for hs in src.messages.values() for h in hs], reverse=True
    )

    out = await tools.search_messages(src, settings, "norris", folder="F1", limit=2, now=NOW)
    # Walk with the hint: query, folder and cursor only.
    pages = [out]
    while "Next: call search_messages(" in out:
        call = re.search(r"Next: call search_messages\((.*)\)", out).group(1)
        args = dict(re.findall(r'(\w+)=("(?:[^"\\]|\\.)*")', call))
        assert set(args) == {"query", "folder", "cursor"} and args["folder"] == '"5"'
        out = await tools.search_messages(
            src, settings, "norris", folder="5", cursor=args["cursor"].strip('"'), limit=2, now=NOW
        )
        pages.append(out)
        assert len(pages) < 20
    all_hits = []
    for page in pages:
        chat = None
        for line in page.splitlines():
            if line.startswith("— "):
                chat = {"F1 News": A, "Motorsport UA": B, "P1": C}[line[2:].split(" (")[0]]
            m = re.match(r"#(\d+) ", line)
            if m:
                all_hits.append((chat, int(m.group(1))))
    assert sorted(all_hits) == sorted((c, i) for _, c, i in expected)
    assert len(all_hits) == len(set(all_hits)) == 8
    assert len(pages) > 2


async def test_folder_cursor_alone_is_enough(settings):
    src = source_with(*[hit(A, i, i) for i in range(1, 5)])
    first = await tools.search_messages(src, settings, "norris", folder="F1", limit=2)

    second = await tools.search_messages(src, settings, "norris", cursor=cursor_of(first), limit=2)

    assert hit_ids(second) == [2, 1]
    assert 'folder "F1"' in second.splitlines()[0]


async def test_folder_cursor_with_chat(settings):
    src = source_with(*[hit(A, i, i) for i in range(1, 5)])
    first = await tools.search_messages(src, settings, "norris", folder="F1", limit=2)

    with pytest.raises(ToolError) as e:
        await tools.search_messages(src, settings, "norris", "F1 News", cursor=cursor_of(first))
    assert str(e.value) == "This cursor is from a folder search; call search_messages with folder or only the cursor."


async def test_folder_cursor_with_other_folder(settings):
    src = source_with(*[hit(A, i, i) for i in range(1, 5)])
    first = await tools.search_messages(src, settings, "norris", folder="F1", limit=2)

    with pytest.raises(ToolError, match="belongs to another folder"):
        await tools.search_messages(src, settings, "norris", folder="Other", cursor=cursor_of(first))


async def test_global_and_chat_cursors_with_folder(settings):
    src = source_with(*[hit(A, i, i) for i in range(1, 5)])
    glob = await tools.search_messages(src, settings, "norris", limit=2)
    one = await tools.search_messages(src, settings, "norris", "F1 News", limit=2)

    with pytest.raises(ToolError, match="all chats"):
        await tools.search_messages(src, settings, "norris", folder="F1", cursor=cursor_of(glob))
    with pytest.raises(ToolError, match="one chat"):
        await tools.search_messages(src, settings, "norris", folder="F1", cursor=cursor_of(one))


async def test_folder_search_from_me_and_period(settings):
    src = source_with(hit(A, 1, 1, out=True), hit(A, 2, 2))
    out = await tools.search_messages(src, settings, "norris", folder="F1", from_me=True, days=30, now=NOW)

    assert "From me only" in out and hit_ids(out) == [1]
    assert all(q.from_me and q.start is not None for q, _ in src.search_queries)
