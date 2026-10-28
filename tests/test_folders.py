from datetime import UTC, datetime

import pytest

from tests.fakes import make_chat, make_folder, make_person, make_user_chat
from tg_bridge.errors import AmbiguousFolderError, FolderNotFoundError
from tg_bridge.folders import chat_folders, folder_chats, folder_contains, resolve_folder, rule_summary

GROUP = make_chat(id=-100, kind="group", title="Old group")
SUPERGROUP = make_chat(id=-1001, kind="supergroup", title="Super")
CHANNEL = make_chat(id=-1002, kind="channel", title="News")


def at(day: int) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC)


# explicit membership


def test_include_id_is_member():
    assert folder_contains(make_folder(include_ids=(GROUP.id,)), GROUP)


def test_pinned_id_is_member():
    assert folder_contains(make_folder(pinned_ids=(GROUP.id,)), GROUP)


def test_unlisted_chat_not_member_without_rules():
    assert not folder_contains(make_folder(include_ids=(1,)), GROUP)


# precedence


def test_exclude_beats_include():
    assert not folder_contains(make_folder(include_ids=(GROUP.id,), exclude_ids=(GROUP.id,)), GROUP)


def test_exclude_beats_pinned():
    assert not folder_contains(make_folder(pinned_ids=(GROUP.id,), exclude_ids=(GROUP.id,)), GROUP)


def test_exclude_beats_rule():
    assert not folder_contains(make_folder(groups=True, exclude_ids=(GROUP.id,)), GROUP)


def test_include_ignores_exclude_read_muted_archived():
    chat = make_chat(id=-100, kind="group", is_muted=True, is_archived=True, is_unread=False)
    folder = make_folder(include_ids=(chat.id,), exclude_muted=True, exclude_read=True, exclude_archived=True)

    assert folder_contains(folder, chat)


# type flags


def test_contacts_flag():
    contact = make_user_chat(make_person(id=1, is_contact=True))
    stranger = make_user_chat(make_person(id=2, is_contact=False))
    folder = make_folder(contacts=True)

    assert folder_contains(folder, contact)
    assert not folder_contains(folder, stranger)


def test_non_contacts_flag():
    contact = make_user_chat(make_person(id=1, is_contact=True))
    stranger = make_user_chat(make_person(id=2, is_contact=False))
    folder = make_folder(non_contacts=True)

    assert folder_contains(folder, stranger)
    assert not folder_contains(folder, contact)


def test_bots_flag_even_for_contact_bot():
    bot = make_user_chat(make_person(id=3, is_bot=True, is_contact=True))
    user = make_user_chat(make_person(id=4))
    folder = make_folder(bots=True)

    assert folder_contains(folder, bot)
    assert not folder_contains(folder, user)
    assert not folder_contains(make_folder(contacts=True), bot)


def test_groups_flag_covers_group_and_supergroup():
    folder = make_folder(groups=True)

    assert folder_contains(folder, GROUP)
    assert folder_contains(folder, SUPERGROUP)
    assert not folder_contains(folder, CHANNEL)


def test_broadcasts_flag():
    folder = make_folder(broadcasts=True)

    assert folder_contains(folder, CHANNEL)
    assert not folder_contains(folder, SUPERGROUP)


# filters


def test_exclude_read_keeps_only_unread():
    folder = make_folder(groups=True, exclude_read=True)

    assert folder_contains(folder, make_chat(is_unread=True))
    assert not folder_contains(folder, make_chat(is_unread=False))


def test_exclude_archived_drops_archived():
    folder = make_folder(groups=True, exclude_archived=True)

    assert folder_contains(folder, make_chat(is_archived=False))
    assert not folder_contains(folder, make_chat(is_archived=True))


def test_exclude_muted_drops_muted():
    folder = make_folder(groups=True, exclude_muted=True)

    assert folder_contains(folder, make_chat(is_muted=False))
    assert not folder_contains(folder, make_chat(is_muted=True))


def test_exclude_muted_keeps_muted_with_unread_mention():
    folder = make_folder(groups=True, exclude_muted=True)

    assert folder_contains(folder, make_chat(is_muted=True, has_unread_mention=True))


def test_exclude_muted_drops_archived_muted_with_mention():
    folder = make_folder(groups=True, exclude_muted=True)
    chat = make_chat(is_muted=True, has_unread_mention=True, is_archived=True)

    assert not folder_contains(folder, chat)


def test_shared_folder_is_explicit_only():
    folder = make_folder(kind="shared", include_ids=(GROUP.id,))

    assert folder_contains(folder, GROUP)
    assert not folder_contains(folder, SUPERGROUP)


# folder_chats / chat_folders


def test_folder_chats_pinned_first_then_recent():
    a = make_chat(id=-1, last_message_at=at(1))
    b = make_chat(id=-2, last_message_at=at(5))
    c = make_chat(id=-3, last_message_at=None)
    d = make_chat(id=-4, last_message_at=at(3))
    e = make_chat(id=-5, last_message_at=at(9))
    folder = make_folder(pinned_ids=(-4, -1), include_ids=(-2, -3, -5, -99))

    assert folder_chats(folder, [a, b, c, d, e]) == [d, a, e, b, c]


def test_folder_chats_ignores_peers_missing_from_dialogs():
    assert folder_chats(make_folder(include_ids=(-99,), pinned_ids=(-98,)), [GROUP]) == []


def test_chat_folders_in_folder_order():
    f1 = make_folder(id=2, include_ids=(GROUP.id,))
    f2 = make_folder(id=3, title="Other", include_ids=(CHANNEL.id,))
    f3 = make_folder(id=4, title="Both", groups=True)

    assert chat_folders(GROUP, [f1, f2, f3]) == [f1, f3]


# resolve_folder

WAR = make_folder(id=2, title="WAR 🪖")
WORK = make_folder(id=3, title="Work chats")
WORK2 = make_folder(id=4, title="Work news")


def test_resolve_by_id():
    assert resolve_folder("3", [WAR, WORK]) is WORK


def test_resolve_exact_title_case_and_emoji_insensitive():
    assert resolve_folder("war", [WAR, WORK]) is WAR


def test_resolve_fragment():
    assert resolve_folder("chats", [WAR, WORK, WORK2]) is WORK


def test_resolve_exact_beats_fragment():
    work = make_folder(id=5, title="Work")

    assert resolve_folder("work", [WORK, WORK2, work]) is work


def test_resolve_ambiguous():
    with pytest.raises(AmbiguousFolderError) as exc:
        resolve_folder("work", [WAR, WORK, WORK2])

    assert exc.value.candidates == [WORK, WORK2]


def test_resolve_not_found_carries_all_folders():
    with pytest.raises(FolderNotFoundError) as exc:
        resolve_folder("nope", [WAR, WORK])

    assert exc.value.folders == [WAR, WORK]


def test_resolve_digits_fall_through_to_title():
    year = make_folder(id=9, title="2025")

    assert resolve_folder("2025", [WAR, year]) is year


# rule_summary


def test_summary_pinned_and_included():
    assert rule_summary(make_folder(include_ids=(1, 2, 3), pinned_ids=(4,))) == "3 included, 1 pinned"


def test_summary_rules_all_types_excluding():
    folder = make_folder(
        contacts=True, non_contacts=True, groups=True, broadcasts=True, bots=True, exclude_read=True, exclude_archived=True
    )

    assert rule_summary(folder) == "rules: contacts+non-contacts+groups+channels+bots, excl. read+archived"


def test_summary_rules_and_explicit():
    folder = make_folder(groups=True, exclude_muted=True, include_ids=(1, 2), exclude_ids=(3,))

    assert rule_summary(folder) == "rules: groups, excl. muted · 2 included, 1 excluded"


def test_summary_empty():
    assert rule_summary(make_folder()) == "empty"
