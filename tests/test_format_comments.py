from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.fakes import make_chat, make_message, make_person
from tg_bridge.format import COMMENT_TEXT_CHARS, comment_line_of, comments_head, render_page
from tg_bridge.source import MediaInfo, Post

W = ZoneInfo("Europe/Warsaw")
CHANNEL = make_chat(id=-1001234567890, kind="channel", title="imatrofAI", username="imatrofAI")
PRIVATE = make_chat(id=-1001234567890, kind="channel", title="Secret", username=None)
DISCUSSION = -1000000000555
POST_DATE = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)  # 10:00 in Warsaw


def post(text="НЕДІЛЬНИЙ AI ЧЕКІН", count=137, media=None, chat=CHANNEL) -> Post:
    msg = make_message(id=3186, chat_id=chat.id, date=POST_DATE, text=text, media=media, sender=None, sender_title="x")
    return Post(msg, True, count, DISCUSSION)


def comment(id=5012, minutes=14, **kw):
    fields = dict(chat_id=DISCUSSION, date=POST_DATE + timedelta(minutes=minutes), text="did a bot")
    fields.update(kw)
    return make_message(id=id, **fields)


def line(msg, chat=CHANNEL, stamp="10:14", limit=None) -> str:
    return comment_line_of(chat, 3186)(msg, stamp, limit)


def test_head_public_channel():
    assert comments_head(CHANNEL, post(), None, W) == (
        "Comments on post #3186 in imatrofAI (-1001234567890) · 137 comments · whole thread (Europe/Warsaw)\n"
        "Post 2026-11-01 Sun 10:00: НЕДІЛЬНИЙ AI ЧЕКІН · https://t.me/imatrofAI/3186"
    )


def test_head_period_private_channel_photo_post_and_zero_comments():
    head = comments_head(PRIVATE, post(text="", count=0, media=MediaInfo("photo", None, 1200, None, None, None), chat=PRIVATE), "2026-11-01 → now", W)

    first, second = head.splitlines()
    assert "· 0 comments · 2026-11-01 → now (Europe/Warsaw)" in first
    assert second == "Post 2026-11-01 Sun 10:00: [photo 1KB] · https://t.me/c/1234567890/3186"


def test_head_without_link_base_and_text():
    chat = make_chat(id=-12345, kind="channel", title="Old", username=None)

    assert comments_head(chat, post(text="", chat=chat), None, W).splitlines()[1] == "Post 2026-11-01 Sun 10:00"


def test_line_user_with_username_on_every_line():
    anna = make_person(id=42, first_name="Anna", last_name="K", username="anna_k")

    one, two = line(comment(5012, sender=anna)), line(comment(5013, sender=anna))

    assert one == "#5012 10:14 Anna K (https://t.me/anna_k): did a bot https://t.me/imatrofAI/3186?comment=5012"
    assert "Anna K (https://t.me/anna_k):" in two


def test_line_author_variants():
    nameless = make_person(id=42, first_name="Bob", last_name=None, username=None)
    deleted = make_person(id=43, first_name=None, last_name=None, username=None, is_deleted=True)

    assert "Bob (id 42):" in line(comment(sender=nameless))
    assert "Deleted Account (id 43):" in line(comment(sender=deleted))
    assert " Some Channel: " in line(comment(sender=None, sender_title="Some Channel"))
    assert " Me: " in line(comment(out=True))


def test_line_reply_edit_media_and_link_order():
    msg = comment(reply_to_id=5008, edit_date=POST_DATE, media=MediaInfo("photo", None, 1200, None, None, None))

    assert line(msg).endswith("did a bot [photo 1KB] ↩#5008 ✎ https://t.me/imatrofAI/3186?comment=5012")


def test_line_private_channel_link_and_no_link_base():
    assert line(comment(), chat=PRIVATE).endswith("https://t.me/c/1234567890/3186?comment=5012")
    bare = make_chat(id=-12345, kind="channel", title="Old", username=None)
    assert "http" not in line(comment(sender=None, sender_title="T"), chat=bare)


def test_long_comment_is_not_cut():
    text = "x" * 3000

    assert text in line(comment(text=text))
    assert COMMENT_TEXT_CHARS == 4096


def render(comments, max_chars=12000, more=False):
    return render_page(
        "HEAD", comments, tz=W, max_chars=max_chars, keep="oldest", more=more,
        next_hint=lambda last: f"More comments remain. Next: after #{last.id}", end_text="End of comments on post #3186.",
        empty_text="No comments on post #3186.", line_of=comment_line_of(CHANNEL, 3186), noun="comment",
    )


def test_page_has_day_headers_and_end():
    out = render([comment(1, 14), comment(2, 60 * 24 + 5)])

    lines = out.splitlines()
    assert lines[1] == "— 2026-11-01 Sun —" and "— 2026-11-02 Mon —" in lines
    assert lines[-1].endswith("End of comments on post #3186.")


def test_page_budget_respected_with_next():
    comments = [comment(i, i, text="y" * 500) for i in range(1, 51)]

    out = render(comments, max_chars=3000)

    assert len(out) <= 3000 and "Next:" in out


def test_oversize_comment_falls_back_to_cut():
    out = render([comment(1, text="z" * 4000)], max_chars=2000)

    assert len(out) <= 2000 and "Next:" in out


def test_line_channel_author_with_username_gets_link():
    msg = comment(sender=None, sender_title="Some Channel", sender_username="somechan")

    assert " Some Channel (https://t.me/somechan): " in line(msg)


def test_page_footer_counts_comments():
    out = render([comment(1, 14), comment(2, 15)])

    assert out.splitlines()[-1].startswith("Shown 2 comments (")
    assert render([comment(1, 14)]).splitlines()[-1].startswith("Shown 1 comment (")
