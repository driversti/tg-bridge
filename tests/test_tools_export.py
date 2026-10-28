from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_message
from tests.test_config import CREDS
from tests.test_export_file import FlakySource, Sleeps
from tg_bridge import export, tools
from tg_bridge.config import load_settings
from tg_bridge.errors import RateLimitedError

NOW = lambda: datetime(2026, 10, 28, 13, 3, 12, tzinfo=UTC)  # noqa: E731
T0 = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)  # message N is at (12 + N):00 in Warsaw
ANNA = make_chat(id=-77, kind="group", title="Anna K")
BOB = make_chat(id=-78, kind="group", title="Anna B")


@pytest.fixture
def settings(tmp_path):
    return load_settings(env={**CREDS, "TG_BRIDGE_EXPORT_DIR": str(tmp_path / "exports")}, config_file=tmp_path / "c")


def messages(n):
    return [make_message(id=i, chat_id=ANNA.id, date=T0 + timedelta(hours=i), text=f"text {i}") for i in range(1, n + 1)]


def source(n=3, cls=FakeSource, **kw):
    return cls(dialogs=[ANNA], messages={ANNA.id: messages(n)}, **kw)


async def test_done_text_and_file(settings):
    out = await tools.export_chat(source(3), settings, "anna", now=NOW, sleep=Sleeps())

    first, path, rng = out.split("\n")
    assert first == "Exported 3 messages from Anna K (-77) to:"
    assert path.startswith(str(settings.export_dir)) and path.endswith(".md")
    assert rng == (
        "Range: 2026-10-01 13:00 → 2026-10-01 15:00 (Europe/Warsaw). "
        "The file has the full text; it was not read into this conversation."
    )
    with open(path, encoding="utf-8") as f:
        assert "text 3" in f.read()


async def test_period_arguments_are_applied(settings):
    out = await tools.export_chat(source(5), settings, "anna", since="2026-10-01 14:00", until="2026-10-01", now=NOW)

    assert out.startswith("Exported 4 messages from Anna K (-77) to:")  # messages 2..5


async def test_empty_period_writes_no_file(settings):
    out = await tools.export_chat(source(2), settings, "anna", since="2026-11-01", now=NOW)

    assert out == "No messages from Anna K (-77) in this period (2026-11-01 00:00 → now); no file written."
    assert not settings.export_dir.exists()


async def test_stopped_text_says_how_to_resume(settings, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = source(4, FlakySource, error=RateLimitedError(900), fail_on=2)

    out = await tools.export_chat(src, settings, "anna", now=NOW, sleep=Sleeps())

    lines = out.split("\n")
    assert lines[0] == "Exported 2 messages from Anna K (-77) to:"
    assert lines[2] == (
        "Stopped early: Telegram rate limit: retry in 900 s. The file ends at 2026-10-01 14:00. "
        'To continue this file later, call export_chat(chat="-77", resume=true).'
    )


class Clock:
    def __init__(self, step):
        self.step, self.now = step, -step

    def __call__(self):
        self.now += self.step
        return self.now


async def test_paused_export_resumes(settings, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_PAGE", 2)
    src = source(5)

    out = await tools.export_chat(src, settings, "anna", now=NOW, clock=Clock(25))

    lines = out.split("\n")
    assert lines[0] == "Exported 4 messages so far from Anna K (-77) to:"
    assert lines[2] == (
        "So far: 2026-10-01 13:00 → 2026-10-01 16:00 (Europe/Warsaw). Not finished: one call has a time limit. "
        'Next: call export_chat(chat="-77", resume=true)'
    )

    out = await tools.export_chat(src, settings, "anna", days=1, resume=True, now=NOW, clock=Clock(25))

    first, path, rng = out.split("\n")
    assert first == "Exported 5 messages from Anna K (-77) to:"  # the whole file; `days` is ignored when resuming
    assert path == lines[1]
    assert rng.startswith("Range: 2026-10-01 13:00 → 2026-10-01 17:00 (Europe/Warsaw).")


async def test_resume_without_unfinished_export(settings):
    with pytest.raises(ToolError, match="^No unfinished export of this chat to resume"):
        await tools.export_chat(source(1), settings, "anna", resume=True, now=NOW)


async def test_stopped_before_first_page_is_an_error_not_empty(settings):
    src = source(4, FlakySource, error=RateLimitedError(900), fail_on=1)

    with pytest.raises(ToolError, match="^Could not export: Telegram rate limit: retry in 900 s. No file written.$"):
        await tools.export_chat(src, settings, "anna", now=NOW, sleep=Sleeps())
    assert not settings.export_dir.exists()


async def test_only_md_format(settings):
    with pytest.raises(ToolError, match='Only format="md" is supported.'):
        await tools.export_chat(source(), settings, "anna", format="pdf")
    await tools.export_chat(source(), settings, "anna", format=" MD ", now=NOW)


async def test_ambiguous_chat_lists_candidates(settings):
    src = FakeSource(dialogs=[ANNA, BOB])

    with pytest.raises(ToolError, match="ambiguous") as e:
        await tools.export_chat(src, settings, "anna")

    assert "-77" in str(e.value) and "-78" in str(e.value)


async def test_days_and_since_conflict(settings):
    with pytest.raises(ToolError, match="either `days` or `since`/`until`"):
        await tools.export_chat(source(), settings, "anna", days=2, since="2026-10-01")


async def test_result_obeys_max_chars(tmp_path):
    env = {**CREDS, "TG_BRIDGE_EXPORT_DIR": str(tmp_path / "e"), "TG_BRIDGE_MAX_CHARS": "200"}
    small = load_settings(env=env, config_file=tmp_path / "c")
    chat = make_chat(id=-77, kind="group", title="x" * 500)
    src = FakeSource(dialogs=[chat], messages={chat.id: messages(1)})

    out = await tools.export_chat(src, small, "x", now=NOW)

    assert len(out) <= small.max_chars
