import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import FakeSource, make_chat, make_message, make_person
from tg_bridge import export
from tg_bridge.errors import ExportFileError, RateLimitedError
from tg_bridge.export import ExportResult, export_chat_file
from tg_bridge.source import ChatInfo
from tg_bridge.timerange import Period

W = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 28, 13, 3, 12, tzinfo=UTC)
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
CHAT = make_chat(kind="group", id=-5, title="Secret Chess Club")
INFO = ChatInfo(chat=CHAT, description=None, members_count=3, topics=(), topics_total=None)


def msgs(n, start=1):
    return [
        make_message(id=i, chat_id=CHAT.id, date=T0 + timedelta(hours=i), text=f"text {i}") for i in range(start, start + n)
    ]


class FlakySource(FakeSource):
    """Raises `error` from the `fail_on`-th get_messages call (1-based) on, `times` times."""

    def __init__(self, error, fail_on, times=1, **kw):
        super().__init__(**kw)
        self.failure, self.fail_on, self.times = error, fail_on, times

    async def get_messages(self, chat, query):
        call = len(self.message_queries) + 1
        if call >= self.fail_on and self.times > 0:
            self.message_queries.append(query)
            self.times -= 1
            raise self.failure
        return await super().get_messages(chat, query)


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


async def run(source, dest, period=Period(None, None), sleep=None, now=NOW):
    return await export_chat_file(source, INFO, period, dest, W, now, sleep=sleep or Sleeps())


async def test_exports_period_to_file(tmp_path):
    src = FakeSource(messages={CHAT.id: msgs(5)})
    period = Period(T0 + timedelta(hours=2), T0 + timedelta(hours=5))  # messages 2, 3, 4

    result = await run(src, tmp_path, period)

    body = result.path.read_text(encoding="utf-8")
    assert result.path.parent == tmp_path
    assert body.startswith("# Secret Chess Club\n")
    assert "text 1" not in body and "text 5" not in body
    assert all(f"text {i}" in body for i in (2, 3, 4))
    assert body.endswith("Exported 3 messages · 2026-10-01 14:00 → 2026-10-01 16:00 (Europe/Warsaw) · complete\n")
    assert (result.count, result.first, result.last, result.stopped) == (3, T0 + timedelta(hours=2), T0 + timedelta(hours=4), None)


async def test_creates_dir_and_file_mode_600(tmp_path):
    dest = tmp_path / "missing" / "exports"

    result = await run(FakeSource(messages={CHAT.id: msgs(1)}), dest)

    assert result.path.stat().st_mode & 0o777 == 0o600
    assert dest.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("n, boundaries", [(4, [None, 2, 4]), (5, [None, 2, 4])])
async def test_pages_without_gaps_or_duplicates(tmp_path, monkeypatch, n, boundaries):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FakeSource(messages={CHAT.id: msgs(n)})

    result = await run(src, tmp_path)

    body = result.path.read_text(encoding="utf-8")
    assert [body.count(f"text {i}\n") for i in range(1, n + 1)] == [1] * n
    assert result.count == n
    assert [q.boundary_id for q in src.message_queries] == boundaries


async def test_whole_history_when_no_period(tmp_path):
    src = FakeSource(messages={CHAT.id: msgs(1)})

    await run(src, tmp_path)

    q = src.message_queries[0]
    assert (q.direction, q.start, q.end, q.boundary_id, q.topic_id, q.from_me) == ("forward", None, None, None, None, False)
    assert q.limit == export.EXPORT_PAGE


async def test_empty_period_writes_nothing(tmp_path):
    dest = tmp_path / "out"

    result = await run(FakeSource(messages={CHAT.id: msgs(2)}), dest, Period(T0 + timedelta(days=9), None))

    assert result == ExportResult(None, 0, None, None, None)
    assert not dest.exists() or list(dest.iterdir()) == []


async def test_never_overwrites(tmp_path):
    src = FakeSource(messages={CHAT.id: msgs(2)})

    one = await run(src, tmp_path)
    before = one.path.read_text(encoding="utf-8")
    two = await run(src, tmp_path)

    assert two.path != one.path and two.path.name.endswith("_2.md")
    assert one.path.read_text(encoding="utf-8") == before


async def test_gives_up_after_99_files(tmp_path):
    src = FakeSource(messages={CHAT.id: msgs(1)})
    for _ in range(99):
        await run(src, tmp_path)

    with pytest.raises(FileExistsError):
        await run(src, tmp_path)


async def test_short_flood_wait_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    sleeps = Sleeps()
    src = FlakySource(RateLimitedError(40), fail_on=2, messages={CHAT.id: msgs(4)})

    result = await run(src, tmp_path, sleep=sleeps)

    assert sleeps.calls == [41]
    assert result.stopped is None and result.count == 4
    assert result.path.read_text(encoding="utf-8").endswith("· complete\n")


async def test_flood_retries_are_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    sleeps = Sleeps()
    src = FlakySource(RateLimitedError(40), fail_on=2, times=99, messages={CHAT.id: msgs(4)})

    result = await run(src, tmp_path, sleep=sleeps)

    assert len(sleeps.calls) == export.MAX_FLOOD_RETRIES
    assert result.stopped == "Telegram rate limit: retry in 40 s" and result.count == 2


async def test_long_flood_wait_stops_with_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    sleeps = Sleeps()
    src = FlakySource(RateLimitedError(900), fail_on=2, messages={CHAT.id: msgs(4)})

    result = await run(src, tmp_path, sleep=sleeps)

    assert sleeps.calls == []
    assert result.stopped == "Telegram rate limit: retry in 900 s"
    assert (result.count, result.last) == (2, T0 + timedelta(hours=2))
    body = result.path.read_text(encoding="utf-8")
    assert "text 2" in body and "text 3" not in body
    assert body.endswith("· INCOMPLETE: Telegram rate limit: retry in 900 s\n")


async def test_unexpected_error_writes_footer_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FlakySource(RuntimeError("boom"), fail_on=2, messages={CHAT.id: msgs(4)})

    with pytest.raises(RuntimeError):
        await run(src, tmp_path)

    (written,) = tmp_path.iterdir()
    assert written.read_text(encoding="utf-8").endswith("· INCOMPLETE: interrupted\n")


async def test_logs_no_title_or_path(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    dest = tmp_path / "private-dir-name"

    await run(FakeSource(messages={CHAT.id: msgs(2)}), dest)

    assert caplog.records
    text = caplog.text.casefold()
    assert "chess" not in text and "private-dir-name" not in text and "text 1" not in text


# --- time limit and continuation ----------------------------------------------



class Clock:
    """Each call is `step` seconds after the previous one."""

    def __init__(self, step):
        self.step, self.now = step, -step

    def __call__(self):
        self.now += self.step
        return self.now


def plain_msgs(n):
    # No @username: whether it repeats after a pause is not what these tests are about.
    return [m.__class__(**{**m.__dict__, "sender": make_person(username=None)}) for m in msgs(n)]


async def run_chunk(src, dest, resume=None, step=25, now=NOW):
    return await export_chat_file(src, INFO, Period(None, None), dest, W, now, resume=resume, sleep=Sleeps(), clock=Clock(step))


async def test_pauses_at_the_time_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FakeSource(messages={CHAT.id: plain_msgs(5)})

    result = await run_chunk(src, tmp_path)  # 25 s per page: the second page crosses 40 s

    assert (result.count, result.paused, result.stopped) == (4, True, None)
    c = result.cursor
    assert (c.chat_id, c.boundary_id, c.count, c.file_n) == (CHAT.id, 4, 4, 1)
    assert (c.first, c.last, c.file_stamp) == (T0 + timedelta(hours=1), T0 + timedelta(hours=4), NOW)
    assert result.path.read_text(encoding="utf-8").endswith("· INCOMPLETE: paused, to be continued\n")


async def test_continuation_appends_to_the_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FakeSource(messages={CHAT.id: plain_msgs(9)})
    whole = await run_chunk(src, tmp_path / "whole", step=0)

    result = await run_chunk(src, tmp_path / "parts")
    while result.paused:
        result = await run_chunk(src, tmp_path / "parts", resume=result.cursor, now=NOW + timedelta(minutes=5))

    assert (result.count, result.first, result.last, result.cursor) == (9, whole.first, whole.last, None)
    assert [p.name for p in (tmp_path / "parts").iterdir()] == [whole.path.name]
    assert result.path.read_text(encoding="utf-8") == whole.path.read_text(encoding="utf-8")


async def test_long_flood_wait_gives_a_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FlakySource(RateLimitedError(900), fail_on=2, messages={CHAT.id: plain_msgs(4)})

    result = await run_chunk(src, tmp_path, step=0)

    assert result.stopped == "Telegram rate limit: retry in 900 s" and not result.paused
    assert (result.cursor.boundary_id, result.cursor.count) == (2, 2)


async def test_continuation_of_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FakeSource(messages={CHAT.id: plain_msgs(5)})
    result = await run_chunk(src, tmp_path)
    result.path.unlink()

    with pytest.raises(ExportFileError, match="^The file of this export is gone"):
        await run_chunk(src, tmp_path, resume=result.cursor)


async def test_continuation_of_a_complete_file(tmp_path, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = FakeSource(messages={CHAT.id: plain_msgs(5)})
    paused = await run_chunk(src, tmp_path)
    done = await run_chunk(src, tmp_path, resume=paused.cursor, step=0)
    before = done.path.read_text(encoding="utf-8")

    with pytest.raises(ExportFileError, match="^This export is already complete"):
        await run_chunk(src, tmp_path, resume=paused.cursor)
    assert done.path.read_text(encoding="utf-8") == before
