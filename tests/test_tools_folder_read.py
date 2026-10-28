import re
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_folder, make_message
from tests.test_config import CREDS
from tg_bridge import many, tools
from tg_bridge.config import load_settings
from tg_bridge.source import MediaInfo

NOW = datetime(2026, 10, 28, 13, 3, 12, tzinfo=UTC)  # Wed 14:03 in Warsaw
MON = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # inside this_week (starts Sun 23:00 UTC)
NEWS, UA, P1, OLD, OUTSIDE = -2001, -2002, -2003, -2004, -2009
NEWS_TEXT = "Verstappen signs new contract with Red Bull until 2028"


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


def f1_dialogs():
    return [
        make_chat(id=NEWS, kind="channel", title="F1 News", username="f1news"),
        make_chat(id=UA, kind="channel", title="Motorsport UA"),
        make_chat(id=P1, kind="channel", title="P1"),
        make_chat(id=OLD, kind="channel", title="Old Archive", is_archived=True, is_muted=True),
        make_chat(id=OUTSIDE, kind="channel", title="Elsewhere"),
    ]


def post(id, chat, minutes=0, text="hello", **kw):
    titles = {NEWS: "F1 News", UA: "Motorsport UA", P1: "P1", OLD: "Old Archive", OUTSIDE: "Elsewhere"}
    fields = dict(sender=None, sender_title=titles[chat])
    fields.update(kw)
    return make_message(id=id, chat_id=chat, date=MON + timedelta(minutes=minutes), text=text, **fields)


def f1_source(*messages, folder_kw=None, dialogs=None):
    by_chat: dict[int, list] = {}
    for m in messages:
        by_chat.setdefault(m.chat_id, []).append(m)
    folder = make_folder(id=5, title="F1", kind="shared", include_ids=(NEWS, UA, P1, OLD), **(folder_kw or {}))
    return FakeSource(dialogs=dialogs or f1_dialogs(), folders=[folder], messages=by_chat)


async def run(source, settings, **kw):
    kw.setdefault("now", lambda: NOW)
    return await tools.get_messages(source, settings, **kw)


# --- errors ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "text"),
    [
        (dict(chat="x", folder="F1", period="today"), "Pass either chat or folder, not both."),
        (dict(period="today"), "Pass chat or folder."),
        (
            dict(folder="F1"),
            'Reading a folder needs a period, e.g. period="today", period="this_week" or days=3.',
        ),
        (dict(folder="F1", mode="headlines"), 'Reading a folder needs a period, e.g. period="today", period="this_week" or days=3.'),
        (dict(chat="x", mode="headlines"), 'mode="headlines" needs a period, e.g. period="this_week" or days=7.'),
        (dict(folder="F1", period="today", mode="x"), 'Unknown mode "x". Allowed: full, headlines.'),
        (dict(chat="x", mode="loud"), 'Unknown mode "loud". Allowed: full, headlines.'),
        (dict(folder="F1", period="today", topic="3"), "topic and around work with one chat, not with a folder."),
        (dict(folder="F1", period="today", around=5), "topic and around work with one chat, not with a folder."),
    ],
)
async def test_argument_errors(settings, kwargs, text):
    with pytest.raises(ToolError) as e:
        await run(f1_source(), settings, **kwargs)
    assert str(e.value) == text


async def test_mode_is_casefolded_and_stripped(settings):
    text = await run(f1_source(post(1, NEWS, text="Norris wins")), settings, folder="F1", period="this_week", mode=" Headlines ")
    assert "1 headline;" in text


async def test_folder_too_big(settings):
    dialogs = [make_chat(id=-2000 - i, kind="channel", title=f"C{i}") for i in range(51)]
    folder = make_folder(id=5, title="F1", include_ids=tuple(c.id for c in dialogs))
    source = FakeSource(dialogs=dialogs, folders=[folder])
    with pytest.raises(ToolError) as e:
        await run(source, settings, folder="F1", period="today")
    assert str(e.value) == (
        'Folder "F1" has 51 chats; folder queries handle up to 50. Use a smaller folder, or search without folder.'
    )
    assert source.message_queries == []


async def test_folder_exactly_at_limit_is_read(settings):
    dialogs = [make_chat(id=-2000 - i, kind="channel", title=f"C{i}") for i in range(50)]
    source = FakeSource(dialogs=dialogs, folders=[make_folder(id=5, title="F1", include_ids=tuple(c.id for c in dialogs))])
    text = await run(source, settings, folder="F1", period="today")
    assert text.endswith("No messages in this period.")
    assert len(source.message_queries) == 50


async def test_folder_empty(settings):
    source = FakeSource(dialogs=f1_dialogs(), folders=[make_folder(id=5, title="F1")])
    with pytest.raises(ToolError) as e:
        await run(source, settings, folder="F1", period="today")
    assert str(e.value) == 'Folder "F1" has no chats.'


async def test_unknown_folder_lists_folders(settings):
    with pytest.raises(ToolError, match='No folder matches "nope"'):
        await run(f1_source(), settings, folder="nope", period="today")


async def test_over_cap(settings, monkeypatch):
    monkeypatch.setattr(many, "MAX_READ_MESSAGES", 2)
    source = f1_source(*[post(i, NEWS, i) for i in range(1, 4)])
    with pytest.raises(ToolError) as e:
        await run(source, settings, folder="F1", period="this_week")
    assert str(e.value) == (
        'More than 2 messages in this period across 4 chats of folder "F1". Pass a shorter period.'
    )


async def test_single_chat_headlines_over_cap(settings, monkeypatch):
    monkeypatch.setattr(many, "MAX_READ_MESSAGES", 2)
    source = f1_source(*[post(i, NEWS, i) for i in range(1, 4)])
    with pytest.raises(ToolError) as e:
        await run(source, settings, chat=str(NEWS), period="this_week", mode="headlines")
    assert str(e.value) == 'More than 2 messages in this period in "F1 News". Pass a shorter period.'


async def test_deadline(settings):
    source = f1_source(post(1, NEWS))
    ticks = iter([0.0, 0.0, 41.0, 41.0, 41.0, 41.0])
    with pytest.raises(ToolError) as e:
        await run(source, settings, folder="F1", period="this_week", clock=lambda: next(ticks))
    assert str(e.value) == (
        "Reading took too long: 1 of 4 chats read in 41 s. Pass a shorter period or use a smaller folder."
    )


# --- folder reads ------------------------------------------------------------------


async def test_folder_read_full_merges_chronologically(settings):
    source = f1_source(
        post(4812, NEWS, 183, text="late news"),
        post(911, UA, 60, text="ua news"),
        post(2, P1, 120, text="p1 news"),
        post(1, NEWS, 5, text="first"),
        post(7, OUTSIDE, 6, text="not in folder"),
    )

    text = await run(source, settings, folder="F1", period="this_week")

    assert text.splitlines() == [
        "Folder: F1 (5) · 4 chats · 2026-10-26 00:00 → now (Europe/Warsaw)",
        "4 messages in 3 of 4 chats; showing 1–4.",
        "F1 News = -2001 · Links: https://t.me/f1news/<id>",
        "Motorsport UA = -2002",
        "P1 = -2003",
        "— 2026-10-26 Mon —",
        "#1 09:05 F1 News: first",
        "#911 10:00 Motorsport UA: ua news",
        "#2 11:00 P1: p1 news",
        "#4812 12:03 F1 News: late news",
        "End of period.",
    ]
    # Folder order, one forward query per member, the extra message only detects the cap.
    assert [q.direction for q in source.message_queries] == ["forward"] * 4
    assert [q.limit for q in source.message_queries][0] == 2001
    assert all(q.start == datetime(2026, 10, 25, 23, 0, tzinfo=UTC) for q in source.message_queries)


async def test_folder_read_includes_archived_member(settings):
    source = f1_source(post(3, OLD, 10, text="from the archive"))

    text = await run(source, settings, folder="5", period="this_week")

    assert "#3 09:10 Old Archive: from the archive" in text.splitlines()


async def test_folder_read_from_me(settings):
    source = f1_source(post(1, NEWS, 1), post(2, NEWS, 2, out=True))

    text = await run(source, settings, folder="F1", period="this_week", from_me=True)

    assert all(q.from_me for q in source.message_queries)
    assert "From me only" in text.splitlines()
    assert "#2 " in text and "#1 " not in text


async def test_folder_read_empty_period(settings):
    text = await run(f1_source(), settings, folder="F1", period="today")

    assert text.splitlines() == [
        "Folder: F1 (5) · 4 chats · 2026-10-28 00:00 → now (Europe/Warsaw)",
        "No messages in this period.",
    ]


async def test_folder_read_paging_by_limit_continues_with_cursor_only(settings):
    msgs = [post(i, NEWS, i, text=f"m{i}") for i in range(1, 8)]
    source = f1_source(*msgs)

    first = await run(source, settings, folder="F1", period="this_week", limit=3)
    assert "showing 1–3." in first
    call, args = next_call(first)
    assert call == "get_messages" and set(args) == {"folder", "cursor"} and args["folder"] == "5"

    second = await run(source, settings, folder="F1", cursor=args["cursor"], limit=3)
    assert "showing 4–6." in second and "#4 " in second and "#3 " not in second

    third = await run(source, settings, cursor=next_call(second)[1]["cursor"], limit=3)
    assert "showing 7–7." in third and third.splitlines()[-1] == "End of period."


# --- headlines ---------------------------------------------------------------------


async def test_folder_headlines_groups_reposts(settings):
    media_only = post(5, UA, 30, text="", media=MediaInfo("photo", None, 10, "image/jpeg", None, None))
    source = f1_source(
        post(4812, NEWS, 0, text=NEWS_TEXT),
        post(911, UA, 20, text="Max Verstappen has signed a new contract with Red Bull until 2028!"),
        post(77, P1, 40, text="Verstappen signed new contract with Red Bull until 2028 official"),
        post(8, NEWS, 90, text="Norris wins the Mexican GP"),
        media_only,
    )

    text = await run(source, settings, folder="F1", period="this_week", mode="headlines")

    assert text.splitlines() == [
        "Folder: F1 (5) · 4 chats · 2026-10-26 00:00 → now (Europe/Warsaw)",
        "5 posts in 3 of 4 chats → 2 headlines (2 repeats merged, 1 without text skipped); showing 1–2.",
        "F1 News = -2001 · Links: https://t.me/f1news/<id>",
        "Motorsport UA = -2002",
        "P1 = -2003",
        "— 2026-10-26 Mon —",
        f"09:00 ×3 F1 News #4812, Motorsport UA #911, P1 #77: {NEWS_TEXT}",
        "10:30 F1 News #8: Norris wins the Mexican GP",
        "End of period.",
    ]


async def test_headlines_totals_omit_zero_parts(settings):
    source = f1_source(post(1, NEWS, 0, text="Norris wins the Mexican GP"))

    text = await run(source, settings, folder="F1", period="this_week", mode="headlines")

    assert "1 post in 1 of 4 chats → 1 headline; showing 1–1." in text


async def test_headlines_nothing_with_text(settings):
    source = f1_source(post(1, NEWS, 0, text=""))

    text = await run(source, settings, folder="F1", period="this_week", mode="headlines")

    assert text.splitlines()[-1] == "No posts with text in this period."


async def test_folder_headlines_pages_cover_everything_once(settings):
    # Equal timestamps across chats at the page boundaries; some groups have members after the boundary.
    stories = [f"Story {i} alpha{i} beta{i} gamma{i} delta{i} epsilon{i} zeta{i}" for i in range(1, 9)]
    msgs = []
    for i, story in enumerate(stories, start=1):
        minute = (i + 1) // 2  # two stories per minute: ties across chats
        msgs.append(post(i, NEWS, minute, text=story))
        msgs.append(post(100 + i, UA, minute, text=story + "!"))  # a repost one chat later, same second
    msgs.append(post(999, P1, 3, text="Unique tail item with several different words here"))
    source = f1_source(*msgs)
    small = replace_max(settings, 600)

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        kw = dict(folder="F1", period="this_week", mode="headlines") if cursor is None else dict(cursor=cursor)
        text = await run(source, small, **kw)
        assert len(text) <= 600
        pages += 1
        seen += [int(m) for m in re.findall(r"^\d\d:\d\d (?:×\d+ )?[^#\n]*?#(\d+)", text, re.M)]
        if text.splitlines()[-1] == "End of period.":
            break
        call, args = next_call(text)
        assert set(args) == {"folder", "cursor"}
        cursor = args["cursor"]
        assert pages < 30
    assert pages >= 3
    # Each group's first post exactly once; on a tie the lower chat id (Motorsport UA) comes first.
    assert sorted(seen) == [*range(101, 109), 999]


def replace_max(settings, n):
    from dataclasses import replace

    return replace(settings, max_chars=n)


async def test_single_chat_headlines(settings):
    source = f1_source(
        post(1, NEWS, 0, text="BREAKING\nVerstappen signs"),
        post(2, NEWS, 10, text=""),
        post(3, NEWS, 20, text="#F1 https://t.me/x/1\nNorris wins"),
    )

    text = await run(source, settings, chat=str(NEWS), period="this_week", mode="headlines")

    assert text.splitlines() == [
        "Chat: F1 News (-2001) · 2026-10-26 00:00 → now (Europe/Warsaw)",
        "Links: https://t.me/f1news/<id>",
        "Mode: headlines",
        "3 posts → 2 headlines (1 without text skipped); showing 1–2.",
        "— 2026-10-26 Mon —",
        "09:00 #1: BREAKING",
        "09:20 #3: Norris wins",
        "End of period.",
    ]


async def test_single_chat_headlines_cursor_scope_is_chat(settings):
    source = f1_source(*[post(i, NEWS, i, text=f"Story {i} alpha{i} beta{i}") for i in range(1, 6)])

    first = await run(source, settings, chat=str(NEWS), period="this_week", mode="headlines", limit=2)
    call, args = next_call(first)
    assert set(args) == {"chat", "cursor"} and args["chat"] == str(NEWS)
    assert "_k" not in args["cursor"] and args["cursor"].endswith("_h")

    second = await run(source, settings, cursor=args["cursor"], limit=2)  # chat comes from the cursor
    assert "showing 3–4." in second and "#3:" in second and "#2:" not in second


async def test_headlines_with_topic_is_rejected(settings):
    with pytest.raises(ToolError, match="topic"):
        await run(f1_source(), settings, chat=str(NEWS), period="today", mode="headlines", topic="3")


# --- cursor conflicts and compatibility --------------------------------------------


async def test_folder_cursor_with_other_folder(settings):
    source = f1_source(*[post(i, NEWS, i) for i in range(1, 5)])
    source.folders.append(make_folder(id=6, title="Other", include_ids=(NEWS,)))
    first = await run(source, settings, folder="F1", period="this_week", limit=2)
    cursor = next_call(first)[1]["cursor"]

    with pytest.raises(ToolError, match="belongs to another folder"):
        await run(source, settings, folder="Other", cursor=cursor)
    with pytest.raises(ToolError, match="Invalid cursor"):
        await run(source, settings, chat=str(NEWS), cursor=cursor)


async def test_chat_cursor_with_other_chat_or_folder(settings):
    source = f1_source(*[post(i, NEWS, i, text=f"Story {i} alpha{i} beta{i}") for i in range(1, 5)])
    first = await run(source, settings, chat=str(NEWS), period="this_week", mode="headlines", limit=2)
    cursor = next_call(first)[1]["cursor"]

    with pytest.raises(ToolError, match="belongs to another chat"):
        await run(source, settings, chat=str(UA), cursor=cursor)
    with pytest.raises(ToolError, match="Invalid cursor"):
        await run(source, settings, folder="F1", cursor=cursor)


async def test_folder_deleted_since_cursor_was_issued(settings):
    source = f1_source(*[post(i, NEWS, i) for i in range(1, 5)])
    first = await run(source, settings, folder="F1", period="this_week", limit=2)
    cursor = next_call(first)[1]["cursor"]
    source.folders.clear()

    with pytest.raises(ToolError, match="no chat folders"):
        await run(source, settings, cursor=cursor)


async def test_garbage_read_cursor(settings):
    with pytest.raises(ToolError, match="Invalid cursor"):
        await run(f1_source(), settings, folder="F1", cursor="r12_d5")


async def test_old_cursors_still_work_for_plain_reads(settings):
    source = f1_source(*[post(i, NEWS, i) for i in range(1, 6)])
    first = await run(source, settings, chat=str(NEWS), period="this_week", limit=2)
    call, args = next_call(first)
    assert args["cursor"].startswith("f")

    second = await run(source, settings, chat=str(NEWS), cursor=args["cursor"], limit=2)

    assert "#3 " in second and "#2 " not in second


# --- helpers -----------------------------------------------------------------------


def next_call(text: str) -> tuple[str, dict[str, str]]:
    match = re.search(r"Next: call (\w+)\((.*)\)", text)
    assert match, text
    return match.group(1), dict(re.findall(r'(\w+)="([^"]*)"', match.group(2)))
