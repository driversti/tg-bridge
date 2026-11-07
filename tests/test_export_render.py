from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.fakes import make_chat, make_message, make_person, make_user_chat
from tg_bridge.export import (
    RenderState,
    escape_block,
    export_filename,
    period_text,
    render_footer,
    render_header,
    render_messages,
)
from tg_bridge.source import ChatInfo, MediaInfo
from tg_bridge.timerange import Period

W = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 28, 13, 3, 12, tzinfo=UTC)  # 14:03:12 in Warsaw


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def info_of(chat, **overrides) -> ChatInfo:
    fields = dict(chat=chat, description=None, members_count=None, topics=(), topics_total=None)
    fields.update(overrides)
    return ChatInfo(**fields)


def render_all(chat, messages, info=None, period=Period(None, None), stopped=None):
    state = RenderState()
    return (
        render_header(info or info_of(chat), period, W, NOW)
        + render_messages(messages, chat, W, state)
        + render_footer(len(messages), messages[0].date, messages[-1].date, W, stopped)
    )


def test_full_snapshot():
    anna = make_person(id=123, phone="+48123456789", is_bot=True, is_deleted=True)
    chat = make_user_chat(anna, is_archived=True)
    msgs = [
        make_message(id=4812, chat_id=123, sender=anna, date=utc(2026, 10, 20, 12, 3), text="Привіт! Ось лог помилки:\n# not a heading"),
        make_message(
            id=4813, chat_id=123, out=True, sender=None, date=utc(2026, 10, 20, 12, 5), text="Дякую, гляну",
            reply_to_id=4812, edit_date=utc(2026, 10, 20, 12, 6),
        ),
        make_message(
            id=4814, chat_id=123, sender=anna, date=utc(2026, 10, 21, 12, 7), text="", forward_from="Bob",
            media=MediaInfo("photo", None, 1258291, None, None, None),
        ),
    ]
    period = Period(utc(2026, 9, 30, 22), utc(2026, 10, 31, 23))
    info = info_of(chat, description="Our team chat")

    assert render_all(chat, msgs, info, period) == (
        "# Deleted Account\n"
        "\n"
        "- Chat: bot · id 123 · @anna_k · archived\n"
        "- Peer: Deleted Account · first name: Anna · last name: K · @anna_k · id 123 · phone: +48123456789 · bot · deleted\n"
        "- Description: Our team chat\n"
        "- Period: 2026-10-01 00:00 → 2026-11-01 00:00 (Europe/Warsaw)\n"
        "- Exported: 2026-10-28 14:03:12 (Europe/Warsaw) by tg-bridge\n"
        "\n"
        "## 2026-10-20 Tue\n"
        "\n"
        "**14:03 Deleted Account (@anna_k)** #4812\n"
        "Привіт! Ось лог помилки:\n"
        "\\# not a heading\n"
        "\n"
        "**14:05 Me** #4813 ↩#4812 ✎\n"
        "Дякую, гляну\n"
        "\n"
        "## 2026-10-21 Wed\n"
        "\n"
        "**14:07 Deleted Account** #4814 ⤷fwd Bob\n"
        "[photo · 1.2MB]\n"
        "\n"
        "---\n"
        "Exported 3 messages · 2026-10-20 14:03 → 2026-10-21 14:07 (Europe/Warsaw) · complete\n"
    )


def test_links_in_header_and_media_line():
    chat = make_chat()  # supergroup -1001234567890
    msg = make_message(id=7, text="", media=MediaInfo("document", "a.pdf", 2048, None, None, None))

    out = render_all(chat, [msg], info_of(chat, members_count=42))

    assert "- Links: https://t.me/c/1234567890/<id>\n" in out
    assert "[document · a.pdf · 2KB · https://t.me/c/1234567890/7]" in out


def test_ids_are_links_when_the_chat_has_them():
    chat = make_chat()  # supergroup -1001234567890
    msg = make_message(id=7, text="hi", reply_to_id=5)

    out = render_all(chat, [msg], info_of(chat))

    assert "** [#7](https://t.me/c/1234567890/7) ↩[#5](https://t.me/c/1234567890/5)\n" in out


def test_group_header_has_members_and_no_peer_line():
    chat = make_chat(kind="group", id=-5, title="Chess", is_forum=False)

    head = render_header(info_of(chat, members_count=42), Period(None, None), W, NOW)

    assert "- Members: 42\n" in head
    assert "- Peer:" not in head
    assert "- Chat: group · id -5\n" in head


def test_header_skips_empty_fields():
    chat = make_chat(kind="group", id=-5, title="Chess")

    head = render_header(info_of(chat), Period(None, None), W, NOW)

    assert "Members" not in head and "Description" not in head and "Links" not in head
    assert head.endswith("\n\n")
    assert "- Period: whole history\n" in head


def test_period_text():
    end = utc(2026, 9, 30, 22)

    assert period_text(Period(None, None), W) == "whole history"
    assert period_text(Period(None, end), W) == "everything before 2026-10-01 00:00"
    assert period_text(Period(end, None), W) == "2026-10-01 00:00 → now"
    assert period_text(Period(end, utc(2026, 10, 31, 23)), W) == "2026-10-01 00:00 → 2026-11-01 00:00"


@pytest.mark.parametrize("line", ["# x", "  > q", "- a", "+ a", "* a", "= a", "1. a", "2) a", "```py", "~~~", "|a|", "-"])
def test_escape_block_marks_block_syntax(line):
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]

    assert escape_block(line) == f"{indent}\\{stripped}"


@pytest.mark.parametrize("line", ["10 apples", "a # b", "plain", ""])
def test_escape_block_leaves_the_rest(line):
    assert escape_block(line) == line


def test_text_is_full_and_normalised():
    chat = make_chat()
    long = "x" * 5000

    out = render_messages([make_message(text=long), make_message(id=2, text="a\r\nb\x07c")], chat, W, RenderState())

    assert long in out
    assert "\na\nbc\n" in out


def test_blank_lines_in_text_stay_inside_the_message():
    out = render_messages([make_message(text="a\n\n- b")], make_chat(), W, RenderState())

    assert "\na\n\n\\- b\n" in out


def test_render_state_spans_pages():
    chat = make_chat()
    state = RenderState()

    first = render_messages([make_message(id=1)], chat, W, state)
    second = render_messages([make_message(id=2, date=utc(2026, 10, 26, 8))], chat, W, state)

    assert first.count("## 2026-10-26 Mon") == 1
    assert "##" not in second
    assert "(@anna_k)" in first and "(@anna_k)" not in second


def test_sender_markup_is_escaped():
    p = make_person(first_name="a*b_[c]", last_name=None, username=None)

    out = render_messages([make_message(sender=p)], make_chat(), W, RenderState())

    assert "**08:12 a\\*b\\_\\[c\\]**" in out


def test_forum_topic_marker():
    chat = make_chat(is_forum=True)

    out = render_messages([make_message(id=5, topic_id=9), make_message(id=6)], chat, W, RenderState())

    assert "[#5](https://t.me/c/1234567890/5) topic #9\n" in out
    assert "[#6](https://t.me/c/1234567890/6)\n" in out


def test_service_message_and_media_label():
    msgs = [
        make_message(id=1, text="", action="pinned #4807"),
        make_message(id=2, text="", media=MediaInfo("sticker", None, None, None, None, "😀")),
        make_message(id=3, text="", media=MediaInfo("poll", None, None, None, None, "Pizza\nor sushi?")),
        make_message(id=4, text="", media=MediaInfo("voice", None, 3072, None, 65, None)),
    ]

    out = render_messages(msgs, make_chat(kind="group", id=-5), W, RenderState())  # a chat without links

    assert "[pinned #4807]" in out
    assert "[sticker · 😀]" in out
    assert "[poll · Pizza or sushi?]" in out
    assert "[voice · 1:05 · 3KB]" in out


def test_footer_incomplete():
    footer = render_footer(1, utc(2026, 10, 26, 7), utc(2026, 10, 26, 7), W, "Telegram rate limit: retry in 900 s")

    assert footer == (
        "---\nExported 1 message · 2026-10-26 08:00 → 2026-10-26 08:00 (Europe/Warsaw)"
        " · INCOMPLETE: Telegram rate limit: retry in 900 s\n"
    )


def test_export_filename():
    chat = make_chat(id=-100123, title="Аня / Work 🚀")
    period = Period(utc(2026, 9, 30, 22), utc(2026, 10, 31, 23))
    first = utc(2026, 10, 5, 8)

    assert export_filename(chat, period, first, W, NOW) == "аня-work_-100123_2026-10-01_2026-10-31_20261028-140312.md"
    assert export_filename(chat, period, first, W, NOW, n=2).endswith("_20261028-140312_2.md")
    assert export_filename(chat, Period(None, None), first, W, NOW) == "аня-work_-100123_2026-10-05_2026-10-28_20261028-140312.md"


def test_export_filename_without_title_words():
    chat = make_chat(id=7, title="🚀🚀")

    assert export_filename(chat, Period(None, None), NOW, W, NOW).startswith("chat_7_")


def test_header_lists_identity_guesses_after_peer():
    chat = make_user_chat(make_person(id=777, first_name=None, last_name=None, username=None, is_deleted=True))
    guess = "Olena P (name on a contact card, #5, 2020-05-02; a guess)"

    text = render_header(info_of(chat), Period(None, None), W, NOW, identity=[guess])

    assert f"- Peer: Deleted Account · id 777 · deleted\n- Identity guess: {guess}\n" in text
