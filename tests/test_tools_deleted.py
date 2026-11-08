from dataclasses import replace
from datetime import UTC, datetime

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.errors import RateLimitedError, TelegramRefusedError
from tg_bridge.source import ContactCard, HistoryStats

NOW = lambda: datetime(2026, 11, 7, 20, 0, 0, tzinfo=UTC)  # noqa: E731


@pytest.fixture
def settings(tmp_path):
    env = {**CREDS, "TG_BRIDGE_EXPORT_DIR": str(tmp_path / "exports"), "TG_BRIDGE_SCOPE": "all"}
    return load_settings(env=env, config_file=tmp_path / "c")


def gone(id, last=None, **kw):
    person = make_person(id=id, first_name=None, last_name=None, username=None, is_deleted=True)
    return make_user_chat(person, last_message_at=last, **kw)


def day(y, m, d):
    return datetime(y, m, d, 12, tzinfo=UTC)


class RefusesStatsFor912(FakeSource):
    async def get_history_stats(self, chat):
        if chat.id == 912:
            raise TelegramRefusedError("X")
        return await super().get_history_stats(chat)


async def test_find_lists_counts_dates_archived_and_guess(settings):
    src = RefusesStatsFor912(
        dialogs=[gone(912, day(2022, 1, 3)), gone(777, day(2023, 7, 14), is_archived=True)],
        stats={777: HistoryStats(1234, day(2019, 3, 1))},
        cards={777: [ContactCard(5, day(2020, 5, 2), 777, "Olena", "P")]},
    )

    out = await tools.find_deleted_users(src, settings)

    assert out == (
        "Found 2 chats with deleted accounts (1:1 chats, archived included).\n"
        "Deleted Account · id 777 · 1234 messages · 2019-03-01 → 2023-07-14 · archived"
        " · guess: Olena P (name on a contact card, #5, 2020-05-02; a guess)\n"
        "Deleted Account · id 912 · count unavailable · last 2022-01-03\n"
        "Chats you deleted on your side are gone and cannot be found. To save them as files: export_deleted_users()."
    )


async def test_find_none_found(settings):
    out = await tools.find_deleted_users(FakeSource(dialogs=[make_user_chat()]), settings)

    assert out == "No chats with deleted accounts (1:1 chats, archived included)."


async def test_find_mentions_scope_when_public(settings):
    public = replace(settings, scope="public")

    out = await tools.find_deleted_users(FakeSource(dialogs=[gone(777)]), public)

    assert out.splitlines()[-2:] == [
        "Scope is public: private chats not switched on in the allowlist were not checked.",
        "Chats you deleted on your side are gone and cannot be found. To save them as files: export_deleted_users().",
    ]
    none = await tools.find_deleted_users(FakeSource(), public)
    assert none.splitlines() == [
        "No chats with deleted accounts (1:1 chats, archived included).",
        "Scope is public: private chats not switched on in the allowlist were not checked.",
    ]


async def test_find_truncates_to_max_chars_keeping_first_line(settings):
    small = replace(settings, max_chars=500)
    src = FakeSource(dialogs=[gone(i) for i in range(1, 61)])

    out = await tools.find_deleted_users(src, small)

    assert len(out) <= 500 and out.endswith("…")
    assert out.startswith("Found 60 chats with deleted accounts")


async def test_find_rate_limit_is_tool_error(settings):
    class Limited(FakeSource):
        async def get_history_stats(self, chat):
            raise RateLimitedError(30)

    with pytest.raises(ToolError, match="rate"):
        await tools.find_deleted_users(Limited(dialogs=[gone(777)]), settings)


# --- export_deleted_users ------------------------------------------------------

from datetime import timedelta  # noqa: E402

from tests.test_export_file import FlakySource, Sleeps  # noqa: E402
from tg_bridge import export as export_mod  # noqa: E402
from tg_bridge.deleted import DELETED_DIR  # noqa: E402

T0 = datetime(2019, 3, 1, 10, 0, tzinfo=UTC)


def history(chat, n):
    from tests.fakes import make_message

    return [make_message(id=i, chat_id=chat.id, date=T0 + timedelta(hours=i), text=f"text {i}") for i in range(1, n + 1)]


class Clock:
    """Time only moves when a source read says so."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SlowSource(FakeSource):
    def __init__(self, clock, cost, **kw):
        super().__init__(**kw)
        self.clock, self.cost = clock, cost

    async def get_messages(self, chat, query):
        self.clock.now += self.cost
        return await super().get_messages(chat, query)


def files(settings):
    folder = settings.export_dir / DELETED_DIR
    return sorted(folder.glob("*.md")) if folder.is_dir() else []


async def export(src, settings, clock=None):
    return await tools.export_deleted_users(src, settings, now=NOW, sleep=Sleeps(), clock=clock or Clock())


async def test_export_writes_one_file_per_deleted_chat_with_identity(settings):
    a, b = gone(777), gone(912)
    src = FakeSource(
        dialogs=[b, a, make_user_chat()],
        messages={777: history(a, 3), 912: history(b, 1)},
        cards={777: [ContactCard(5, day(2020, 5, 2), 777, "Olena", "P")]},
    )

    out = await export(src, settings)

    paths = files(settings)
    assert len(paths) == 2 and all(p.parent == settings.export_dir / DELETED_DIR for p in paths)
    lines = out.splitlines()
    assert lines[0] == f"Deleted-account chats: 2. Files in {settings.export_dir / DELETED_DIR}:"
    assert lines[1].startswith("- id 777: 3 messages → ") and lines[2].startswith("- id 912: 1 message → ")
    assert lines[-1] == "All files are complete. They hold the full text; it was not read into this conversation."
    text = next(p for p in paths if "_777_" in p.name).read_text(encoding="utf-8")
    assert "- Identity guess: Olena P (name on a contact card, #5, 2020-05-02; a guess)" in text


async def test_second_call_reports_already_exported_and_writes_nothing(settings):
    a = gone(777)
    src = FakeSource(dialogs=[a], messages={777: history(a, 3)})
    await export(src, settings)
    before = files(settings)
    src.message_queries.clear()

    out = await export(src, settings)

    assert files(settings) == before and src.message_queries == []
    assert out.splitlines()[1] == f"- id 777: already exported earlier → {before[0]}"
    assert "Next:" not in out and out.endswith("it was not read into this conversation.")


async def test_time_runs_out_between_chats_then_next_call_finishes(settings):
    a, b = gone(777), gone(912)
    clock = Clock()
    src = SlowSource(clock, 38.0, dialogs=[a, b], messages={777: history(a, 2), 912: history(b, 2)})

    first = await export(src, settings, clock)

    assert len(files(settings)) == 1
    assert first.splitlines()[-1] == (
        "Not finished: one call has a time limit; 1 chat not started yet. Next: call export_deleted_users()"
    )
    clock.now = 0.0
    second = await export(src, settings, clock)
    lines = second.splitlines()
    assert lines[1].startswith("- id 777: already exported earlier") and lines[2].startswith("- id 912: 2 messages")
    assert len(files(settings)) == 2 and "Next:" not in second


async def test_paused_chat_is_resumed_into_the_same_file(settings, monkeypatch):
    monkeypatch.setattr(export_mod, "EXPORT_PAGE", 2)
    a = gone(777)
    clock = Clock()
    src = SlowSource(clock, 38.0, dialogs=[a], messages={777: history(a, 5)})

    first = await export(src, settings, clock)

    assert "4 messages so far (paused)" in first and first.splitlines()[-1].endswith("Next: call export_deleted_users()")
    [path] = files(settings)
    clock.now = 0.0

    second = await export(src, settings, clock)

    assert files(settings) == [path]
    assert "5 messages → " in second and "Next:" not in second
    body = path.read_text(encoding="utf-8")
    assert body.count("# Deleted Account") == 1 and "text 5" in body and body.rstrip().endswith("complete")


async def test_rate_limit_stops_the_call_with_incomplete_file(settings, monkeypatch):
    monkeypatch.setattr(export_mod, "EXPORT_PAGE", 2)
    a, b = gone(777), gone(912)
    src = FlakySource(
        RateLimitedError(900), fail_on=2, dialogs=[a, b], messages={777: history(a, 4), 912: history(b, 1)}
    )

    out = await export(src, settings)

    [path] = files(settings)
    assert "INCOMPLETE" in path.read_text(encoding="utf-8")
    lines = out.splitlines()
    assert lines[1].startswith("- id 777: stopped early: ") and lines[1].endswith(f"→ {path}")
    assert lines[-1].startswith("Stopped: ") and lines[-1].endswith("Call export_deleted_users() again later to continue.")
    assert not any("912" in line for line in lines)


async def test_empty_chat_gets_no_file(settings):
    out = await export(FakeSource(dialogs=[gone(501)]), settings)

    assert out.splitlines()[1] == "- id 501: no messages, no file" and files(settings) == []


async def test_export_none_found(settings):
    assert await export(FakeSource(dialogs=[make_user_chat()]), settings) == (
        "No chats with deleted accounts (1:1 chats, archived included)."
    )


async def test_find_skips_join_only_chats_and_says_how_many(settings):
    src = FakeSource(
        dialogs=[gone(777, day(2023, 7, 14)), gone(501, day(2020, 1, 1), last_is_join_notice=True),
                 gone(502, day(2020, 1, 2), last_is_join_notice=True)],
        stats={777: HistoryStats(12, day(2019, 3, 1))},
    )

    out = await tools.find_deleted_users(src, settings)

    assert out.splitlines()[:3] == [
        "Found 1 chat with deleted accounts (1:1 chats, archived included).",
        "Skipped 2 chats that hold only the \"joined Telegram\" notice.",
        "Deleted Account · id 777 · 12 messages · 2019-03-01 → 2023-07-14",
    ]


async def test_find_only_join_only_chats(settings):
    src = FakeSource(dialogs=[gone(501, day(2020, 1, 1), last_is_join_notice=True)])

    out = await tools.find_deleted_users(src, settings)

    assert out.splitlines() == [
        "No chats with deleted accounts (1:1 chats, archived included).",
        "Skipped 1 chat that holds only the \"joined Telegram\" notice.",
    ]


async def test_find_marks_chats_not_checked_in_time(settings):
    ticks = iter([0.0, 0.0, 99.0, 99.0])
    src = FakeSource(dialogs=[gone(1, day(2021, 1, 1)), gone(2, day(2022, 2, 2))], stats={1: HistoryStats(3, day(2020, 1, 1))})

    out = await tools.find_deleted_users(src, settings, clock=lambda: next(ticks))

    assert "Deleted Account · id 2 · not checked (time limit) · last 2022-02-02" in out


async def test_export_skips_join_only_chats(settings):
    a = gone(777)
    joined = gone(501, last_is_join_notice=True)
    src = FakeSource(dialogs=[a, joined], messages={777: history(a, 2), 501: history(joined, 1)})

    out = await tools.export_deleted_users(src, settings)

    assert out.splitlines()[0].startswith("Deleted-account chats: 1.")
    assert "id 501" not in out
    assert len(list((settings.export_dir / "deleted-users").iterdir())) == 1
