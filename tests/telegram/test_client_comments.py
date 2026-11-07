import logging
from datetime import UTC, datetime, timedelta

import pytest
from telethon.errors import ChannelInvalidError, ChannelPrivateError, ChatAdminRequiredError, FloodWaitError, MsgIdInvalidError, RPCError
from telethon.tl import types as tl

from tests.fakes import make_chat
from tests.telegram.test_client import ALLOWED_REQUESTS, settings, wrapper  # noqa: F401  (fixtures)
from tests.telegram.test_mapping import tl_msg, tl_user
from tg_bridge.errors import RateLimitedError, TelegramRefusedError
from tg_bridge.source import MessageQuery, Post
from tg_bridge.source.models import Message

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
CHANNEL = make_chat(id=-1000000000010, kind="channel", title="imatrofAI", username="imatrofAI")
DISCUSSION = -1000000000555


def post_of(id=3186, count=3) -> Post:
    msg = Message(id, CHANNEL.id, T0, None, False, None, "imatrofAI", "post", None, None, None, None, None)
    return Post(msg, True, count, DISCUSSION)


def raw(id, minutes=0, text="comment", top=None, replied=900):
    header = tl.MessageReplyHeader(reply_to_msg_id=replied, reply_to_top_id=top)
    return tl_msg(
        id=id, text=text, date=T0 + timedelta(minutes=minutes), peer_id=tl.PeerChannel(555), reply_to=header,
        sender=tl_user(id=7),
    )


def q(**kw):
    fields = dict(direction="forward", start=None, end=None, boundary_id=None, topic_id=None, from_me=False, limit=10)
    fields.update(kw)
    return MessageQuery(**fields)


async def comments(wrapper, raws, query, post=None):
    tg, fake = wrapper
    fake.messages = raws
    await tg.connect()
    return await tg.get_comments(CHANNEL, post or post_of(), query)


async def test_get_post_uses_one_get_messages_call(wrapper):
    tg, fake = wrapper
    replies = tl.MessageReplies(replies=137, replies_pts=1, comments=True, channel_id=555)
    fake.messages_by_id = {3186: tl_msg(id=3186, text="post", replies=replies, peer_id=tl.PeerChannel(10))}
    await tg.connect()

    post = await tg.get_post(CHANNEL, 3186)

    assert (post.message.id, post.comment_count, post.discussion_id) == (3186, 137, DISCUSSION)
    assert fake.get_messages_calls == [("input:-1000000000010", [3186])]
    assert fake.requests == []


async def test_get_post_missing_is_none(wrapper):
    tg, fake = wrapper
    await tg.connect()

    assert await tg.get_post(CHANNEL, 1) is None


async def test_get_comments_iterates_replies_forward(wrapper):
    result = await comments(wrapper, [raw(1, 0), raw(2, 1, top=900, replied=1)], q(boundary_id=0, start=T0))

    kwargs = wrapper[1].iter_messages_calls[0][1]
    assert kwargs["reply_to"] == 3186 and kwargs["reverse"] is True and kwargs["limit"] is None
    assert kwargs["offset_id"] == 0 and kwargs["offset_date"] == T0 and kwargs["wait_time"] == 0
    assert [m.id for m in result] == [1, 2]
    assert all(m.chat_id == DISCUSSION for m in result)
    assert [m.reply_to_id for m in result] == [None, 1]


async def test_get_comments_boundary_becomes_offset_id(wrapper):
    await comments(wrapper, [raw(1)], q(boundary_id=5040))

    assert wrapper[1].iter_messages_calls[0][1]["offset_id"] == 5040


async def test_get_comments_period_and_limit(wrapper):
    msgs = [raw(1, -60), raw(2, 0), raw(3, 5), raw(4, 10), raw(5, 20)]

    result = await comments(wrapper, msgs, q(start=T0, end=T0 + timedelta(minutes=15), limit=2))
    assert [m.id for m in result] == [2, 3]

    result = await comments(wrapper, msgs, q(start=T0, end=T0 + timedelta(minutes=15)))
    assert [m.id for m in result] == [2, 3, 4]  # skips before start, stops at end


@pytest.mark.parametrize(
    "error, code",
    [
        (MsgIdInvalidError(request=None), "MSG_ID_INVALID"),
        (ChannelPrivateError(request=None), "CHANNEL_PRIVATE"),
        (ChatAdminRequiredError(request=None), "CHAT_ADMIN_REQUIRED"),
        (ChannelInvalidError(request=None), "CHANNEL_INVALID"),
        (RPCError(request=None, message="SOMETHING_NEW", code=400), "SOMETHING_NEW"),
    ],
)
async def test_refused_replies_name_the_reason(wrapper, error, code):
    wrapper[1].iter_messages_fail_after = (0, error)

    with pytest.raises(TelegramRefusedError) as exc:
        await comments(wrapper, [raw(1)], q())

    assert exc.value.code == code


async def test_refused_post_lookup_names_the_reason(wrapper):
    tg, fake = wrapper
    fake.get_messages_error = ChannelPrivateError(request=None)
    await tg.connect()

    with pytest.raises(TelegramRefusedError) as exc:
        await tg.get_post(CHANNEL, 3186)

    assert exc.value.code == "CHANNEL_PRIVATE"


async def test_flood_becomes_rate_limited(wrapper):
    wrapper[1].iter_messages_fail_after = (0, FloodWaitError(request=None, capture=45))

    with pytest.raises(RateLimitedError) as exc:
        await comments(wrapper, [raw(1)], q())

    assert exc.value.seconds == 45


async def test_only_allowed_requests(wrapper):
    await comments(wrapper, [raw(1)], q())

    assert {type(r).__name__ for r in wrapper[1].requests} <= ALLOWED_REQUESTS


async def test_log_has_counts_and_ids_only(wrapper, caplog):
    caplog.set_level(logging.DEBUG)

    await comments(wrapper, [raw(1, text="secret idea"), raw(2, text="another")], q())

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "2 comments of post 3186" in logged and "-1000000000010" in logged
    assert not any(w in logged for w in ("secret", "another", "Anna", "anna_k", "imatrofAI"))


async def test_any_read_refusal_names_the_reason(wrapper):
    # Not only comments: every Telegram refusal reaches the model as a reason, not as a crash.
    tg, fake = wrapper
    fake.iter_messages_fail_after = (0, ChannelPrivateError(request=None))
    fake.messages = [raw(1)]
    await tg.connect()

    with pytest.raises(TelegramRefusedError) as exc:
        await tg.get_messages(CHANNEL, q(direction="backward"))

    assert "private" in str(exc.value) and "(CHANNEL_PRIVATE)" in str(exc.value)
