from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tests.fakes import make_chat, make_person, make_user_chat
from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError
from tg_bridge.format import (
    TRUNCATED,
    ambiguous_text,
    chat_info_block,
    chat_line,
    not_found_text,
    render_lines,
)
from tg_bridge.source import ChatInfo, Topic

WARSAW = ZoneInfo("Europe/Warsaw")


def info(chat, **kw):
    fields = dict(description=None, members_count=None, topics=(), topics_total=None)
    fields.update(kw)
    return ChatInfo(chat=chat, **fields)


def test_chat_line_group_with_all_parts():
    chat = make_chat(title="Family 🏠", username="famchat", is_forum=True, is_archived=True)

    assert chat_line(chat, WARSAW) == (
        "-1001234567890 · supergroup · Family 🏠 · @famchat · last 2026-10-25 13:03 · forum · archived"
    )


def test_chat_line_user():
    chat = make_user_chat(make_person(id=299701567), last_message_at=datetime(2026, 10, 26, 8, 12, tzinfo=UTC))

    assert chat_line(chat, WARSAW) == "299701567 · user · Anna K · @anna_k · last 2026-10-26 09:12"


def test_chat_line_deleted_peer_and_no_date():
    gone = make_person(id=555, first_name=None, last_name=None, username=None, is_deleted=True)

    assert chat_line(make_user_chat(gone, last_message_at=None), WARSAW) == "555 · user · Deleted Account · deleted_peer"


def test_time_converted_across_dst_change():
    before = make_chat(last_message_at=datetime(2026, 10, 24, 23, 30, tzinfo=UTC))  # CEST, +2
    after = make_chat(last_message_at=datetime(2026, 10, 25, 12, 0, tzinfo=UTC))  # CET, +1

    assert "last 2026-10-25 01:30" in chat_line(before, WARSAW)
    assert "last 2026-10-25 13:00" in chat_line(after, WARSAW)


def test_render_lines_fits():
    assert render_lines("H", ["a", "b"], 100) == "H\na\nb"


def test_render_lines_truncates_within_budget():
    lines = ["x" * 10] * 10

    text = render_lines("H", lines, 60)

    assert text.endswith(TRUNCATED)
    assert len(text) <= 60
    assert text.count("x" * 10) < 10


def test_render_lines_exact_budget_is_not_truncated():
    assert render_lines("H", ["abc"], 5) == "H\nabc"
    assert render_lines("H", ["abc", "d"], 5).endswith(TRUNCATED)


def test_chat_info_group_block():
    chat = make_chat(title="Family 🏠", username="famchat", is_forum=True, is_archived=True)
    block = chat_info_block(
        info(
            chat,
            description="Our family",
            members_count=12,
            topics=(Topic(1, "General", False), Topic(23, "Trips", False), Topic(40, "Recipes", True)),
            topics_total=41,
        ),
        WARSAW,
    )

    assert block.splitlines() == [
        "Family 🏠 — supergroup, forum, archived",
        "id -1001234567890 · @famchat · last message 2026-10-25 13:03",
        "Members: 12",
        "Description: Our family",
        "Topics (3 of 41): #1 General · #23 Trips · #40 Recipes (closed)",
    ]


def test_chat_info_unknown_members_when_degraded():
    block = chat_info_block(info(make_chat()), WARSAW)

    assert "Members: unknown" in block
    assert "Description" not in block
    assert "Topics" not in block


def test_chat_info_topics_without_total():
    chat = make_chat(is_forum=True)

    block = chat_info_block(info(chat, members_count=1, topics=(Topic(1, "General", False),)), WARSAW)

    assert "Topics (1): #1 General" in block


def test_chat_info_private_chat_peer_line():
    person = make_person(id=42, phone="48123", is_bot=True)

    block = chat_info_block(info(make_user_chat(person), description="Bio"), WARSAW)

    lines = block.splitlines()
    assert lines[0] == "Anna K — bot"
    assert "Peer: Anna K · @anna_k · id 42 · phone 48123 · bot" in lines
    assert "Description: Bio" in lines
    assert not any(line.startswith("Members") for line in lines)


def test_chat_info_deleted_peer():
    gone = make_person(id=9, first_name=None, last_name=None, username=None, is_deleted=True)

    block = chat_info_block(info(make_user_chat(gone)), WARSAW)

    assert "Peer: Deleted Account · id 9 · deleted" in block


def test_ambiguous_text():
    a, b = make_chat(id=-1, title="Family"), make_chat(id=-2, title="family 🏠")

    text = ambiguous_text(AmbiguousChatError("fam", [a, b]), WARSAW, 5000)

    lines = text.splitlines()
    assert lines[0] == 'Chat "fam" is ambiguous; pass one of these ids:'
    assert lines[1].startswith("-1 · supergroup · Family")
    assert lines[2].startswith("-2 · supergroup · family 🏠")


def test_ambiguous_text_caps_candidates_at_10():
    chats = [make_chat(id=-i, title="Deleted Account") for i in range(1, 13)]

    lines = ambiguous_text(AmbiguousChatError("d", chats), WARSAW, 50000).splitlines()

    assert len(lines) == 1 + 10 + 1
    assert lines[-1] == "…and 2 more; use the id"


def test_not_found_with_suggestions():
    text = not_found_text(ChatNotFoundError("famly", [make_chat(title="Family")]), WARSAW, 5000)

    assert text.splitlines()[0] == 'No chat matches "famly". Closest:'
    assert "Family" in text.splitlines()[1]


def test_not_found_without_suggestions_points_to_list_chats():
    text = not_found_text(ChatNotFoundError("zzz", []), WARSAW, 5000)

    assert text.startswith('No chat matches "zzz".')
    assert "list_chats" in text
    assert "Closest" not in text
