"""💬N on channel post lines and the one get_comments hint per page: how a model finds the comment threads."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tests.fakes import make_chat, make_message
from tg_bridge.format import render_around, render_hits, render_page
from tg_bridge.format_many import render_many
from tg_bridge.headlines import Repost

W = ZoneInfo("Europe/Warsaw")
CHANNEL = make_chat(id=-1001234567890, kind="channel", title="imatrofAI", username="imatrofAI")
T = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
HINT = 'Comments: call get_comments(chat="-1001234567890", post=3186)'


def post(id=3186, count=None, minutes=0, text="НЕДІЛЬНИЙ AI ЧЕКІН"):
    return make_message(
        id=id, chat_id=CHANNEL.id, date=T + timedelta(minutes=minutes), text=text, sender=None,
        sender_title="imatrofAI", comment_count=count,
    )


def page(messages, max_chars=12000):
    return render_page(
        "HEAD", messages, tz=W, max_chars=max_chars, keep="oldest", more=False, next_hint=lambda m: "NEXT",
        end_text="End of period.", empty_text="None.",
    )


def test_post_line_shows_comment_count():
    out = page([post(count=37)])

    assert "#3186 10:00 imatrofAI: НЕДІЛЬНИЙ AI ЧЕКІН 💬37" in out


def test_no_count_or_zero_shows_nothing_and_no_hint():
    out = page([post(1, None), post(2, 0, 1)])

    assert "💬" not in out and "get_comments" not in out


def test_hint_once_per_page_names_the_first_post_with_comments():
    out = page([post(3185, 0), post(3186, 37, 1), post(3187, 5, 2)])

    lines = out.splitlines()
    assert lines.count(HINT) == 1
    assert lines[-2] == HINT and lines[-1].startswith("Shown 3 messages")


def test_hint_counts_towards_the_budget():
    msgs = [post(3186 + i, 10, i, text="x" * 200) for i in range(20)]
    full = page(msgs)
    cut = page(msgs, max_chars=len(full) - 1)

    assert len(cut) <= len(full) - 1 and HINT in cut


def test_search_hits_show_count_and_hint():
    out = render_hits(
        "HEAD", [post(3186, 37)], chats={CHANNEL.id: CHANNEL}, query="чекін", tz=W, max_chars=12000, more=False,
        next_hint=lambda m: "NEXT",
    )

    assert "💬37" in out and out.splitlines().count(HINT) == 1


def test_around_shows_count_and_hint():
    out = render_around(
        "HEAD", [post(3185, None), post(3186, 37, 1)], 3186, tz=W, max_chars=12000, more_older=False,
        more_newer=False, older_hint=lambda m: "O", newer_hint=lambda m: "N",
    )

    assert "💬37" in out and HINT in out


def test_folder_page_shows_count_and_hint():
    out = render_many(
        "HEAD", lambda a, b: "TOTALS", [post(3186, 37)], offset=0, chats={CHANNEL.id: CHANNEL},
        labels={CHANNEL.id: "imatrofAI"}, tz=W, max_chars=12000, limit=100, next_hint=lambda m: "NEXT",
        empty_text="None.",
    )

    assert "💬37" in out and HINT in out


def test_headline_shows_count_per_post_and_hint():
    out = render_many(
        "HEAD", lambda a, b: "TOTALS", [Repost((post(3186, 37),))], offset=0, chats={CHANNEL.id: CHANNEL},
        labels=None, tz=W, max_chars=12000, limit=100, next_hint=lambda m: "NEXT", empty_text="None.",
    )

    assert "https://t.me/imatrofAI/3186 💬37" in out and HINT in out
