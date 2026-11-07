import re
from datetime import UTC, datetime, timedelta

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_chat, make_message, make_person
from tests.test_config import CREDS
from tg_bridge import tools
from tg_bridge.config import load_settings
from tg_bridge.cursor import Cursor, encode_cursor
from tg_bridge.errors import CommentsUnavailableError, RateLimitedError
from tg_bridge.source import Post

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
CHANNEL = make_chat(id=-1001234567890, kind="channel", title="imatrofAI", username="imatrofAI")
GROUP = make_chat(id=-77, kind="group", title="Family")
DISCUSSION = -1000000000555
ANNA = make_person(id=42, first_name="Anna", last_name="K", username="anna_k")


@pytest.fixture
def settings(tmp_path):
    return load_settings(env=CREDS, config_file=tmp_path / "c")


def make_post(id=3186, comments=True, count=250) -> Post:
    msg = make_message(id=id, chat_id=CHANNEL.id, date=T0, text="НЕДІЛЬНИЙ AI ЧЕКІН", sender=None, sender_title="imatrofAI")
    return Post(msg, comments, count if comments else None, DISCUSSION if comments else None)


def thread(n=250, post_id=3186, step=timedelta(minutes=1)):
    return [
        make_message(id=5000 + i, chat_id=DISCUSSION, date=T0 + timedelta(minutes=5) + i * step, text=f"idea {i}", sender=ANNA)
        for i in range(n)
    ]


def source(n=250, **kw):
    return FakeSource(
        dialogs=[CHANNEL, GROUP],
        posts={(CHANNEL.id, 3186): make_post(count=n), (CHANNEL.id, 3187): make_post(3187, comments=False)},
        comments={(CHANNEL.id, 3186): thread(n), (CHANNEL.id, 3188): thread(2)},
        **kw,
    )


def shown_ids(out):
    return [int(m) for m in re.findall(r"^#(\d+) ", out, re.M)]


def next_cursor(out):
    m = re.search(r'Next: call get_messages\(chat="-1001234567890", cursor="([^"]+)"\)', out)
    return m.group(1) if m else None


async def read(src, settings, **kw):
    return await tools.get_messages(src, settings, "imatrofAI", comments_of=3186, **kw)


async def test_whole_thread_oldest_first(settings):
    src = source(3)

    out = await read(src, settings)

    assert out.startswith("Comments on post #3186 in imatrofAI (-1001234567890) · 3 comments · whole thread")
    assert shown_ids(out) == [5000, 5001, 5002]
    assert "Anna K (https://t.me/anna_k)" in out and "?comment=5000" in out
    assert out.endswith("End of comments on post #3186.")
    assert src.comment_queries[0].direction == "forward" and src.comment_queries[0].limit == 101


async def test_comments_pages_cover_everything_once(settings):
    src = source(250)
    seen: list[int] = []
    out = await read(src, settings, limit=100)
    while True:
        seen += shown_ids(out)
        token = next_cursor(out)
        if token is None:
            break
        out = await tools.get_messages(src, settings, "imatrofAI", cursor=token, limit=100)

    assert seen == [5000 + i for i in range(250)]


async def test_period_filters_by_comment_date_and_cursor_keeps_it(settings):
    msgs = [
        make_message(id=1, chat_id=DISCUSSION, date=datetime(2026, 10, 31, 12, tzinfo=UTC), text="old"),
        *[make_message(id=10 + i, chat_id=DISCUSSION, date=datetime(2026, 11, 1, 8 + i, tzinfo=UTC), text="d") for i in range(4)],
        make_message(id=99, chat_id=DISCUSSION, date=datetime(2026, 11, 3, 12, tzinfo=UTC), text="later"),
    ]
    src = source()
    src.comments[(CHANNEL.id, 3186)] = msgs

    out = await read(src, settings, since="2026-11-01", until="2026-11-01", limit=2)
    assert shown_ids(out) == [10, 11] and "2026-11-01" in out.splitlines()[0]
    rest = await tools.get_messages(src, settings, "imatrofAI", cursor=next_cursor(out), limit=5)

    assert shown_ids(rest) == [12, 13] and "End of comments" in rest


async def test_empty_thread(settings):
    src = source()
    src.comments[(CHANNEL.id, 3186)] = []

    assert (await read(src, settings)).endswith("No comments on post #3186.")
    assert (await read(src, settings, since="2026-11-01")).endswith("No comments in this period.")


def error_of(exc):
    return str(exc.value)


async def test_non_channel_chat(settings):
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source(), settings, "family", comments_of=1)

    assert error_of(e) == (
        'comments_of works only with channels. "Family" is a group: its replies are ordinary messages, '
        "read them with get_messages."
    )


async def test_unknown_post(settings):
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source(), settings, "imatrofAI", comments_of=1)

    assert error_of(e) == (
        'Post #1 not found in "imatrofAI". Take post ids from get_messages or search_messages on this channel.'
    )


async def test_comments_off(settings):
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source(), settings, "imatrofAI", comments_of=3187)

    assert error_of(e) == "Post #3187 has no comments: comments are off for this channel or this post."


async def test_comments_unavailable(settings):
    src = source()

    async def refuse(*a, **k):
        raise CommentsUnavailableError

    src.get_comments = refuse
    with pytest.raises(ToolError) as e:
        await read(src, settings)

    assert error_of(e) == "Telegram does not show the comments of post #3186 to this account."


@pytest.mark.parametrize(
    "arg, kw",
    [("folder", dict(folder="WAR")), ("topic", dict(topic=5)), ("around", dict(around=10)), ("from_me", dict(from_me=True)),
     ("mode", dict(mode="headlines"))],
)
async def test_rejected_arguments(settings, arg, kw):
    name = "mode=\"headlines\"" if arg == "mode" else arg
    with pytest.raises(ToolError) as e:
        await tools.get_messages(source(), settings, "imatrofAI", comments_of=3186, **kw)

    assert error_of(e) == f"comments_of cannot be combined with {name}."


async def test_comments_of_needs_chat(settings):
    with pytest.raises(ToolError, match="comments_of needs chat"):
        await tools.get_messages(source(), settings, None, comments_of=3186)


async def test_cursor_of_another_post(settings):
    src = source()
    token = next_cursor(await read(src, settings, limit=100))

    with pytest.raises(ToolError, match="another post's comments"):
        await tools.get_messages(src, settings, "imatrofAI", comments_of=3188, cursor=token)
    ok = await read(src, settings, cursor=token, limit=5)
    assert shown_ids(ok)[0] == 5100


async def test_channel_cursor_with_comments_of_is_rejected(settings):
    channel_cursor = encode_cursor(Cursor(CHANNEL.id, "backward", 100, None, None, None, False))

    with pytest.raises(ToolError, match="another post's comments"):
        await tools.get_messages(source(), settings, "imatrofAI", comments_of=3186, cursor=channel_cursor)


async def test_comments_cursor_alone_continues_thread_and_channel_cursor_never_reads_comments(settings):
    src = source()
    token = next_cursor(await read(src, settings, limit=100))

    out = await tools.get_messages(src, settings, "imatrofAI", cursor=token, limit=3)
    assert shown_ids(out) == [5100, 5101, 5102] and out.startswith("Comments on post #3186")

    src.messages[CHANNEL.id] = [make_message(id=i, chat_id=CHANNEL.id, date=T0 + timedelta(minutes=i), text="p") for i in range(1, 6)]
    channel_cursor = encode_cursor(Cursor(CHANNEL.id, "backward", 4, None, None, None, False))
    before = len(src.comment_queries)
    plain = await tools.get_messages(src, settings, "imatrofAI", cursor=channel_cursor)
    assert shown_ids(plain) == [3, 2, 1][::-1] and len(src.comment_queries) == before


async def test_rate_limit_text(settings):
    src = source()
    src.error = RateLimitedError(30)

    with pytest.raises(ToolError, match="retry in 30 s"):
        await read(src, settings)
