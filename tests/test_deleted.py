from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tests.fakes import FakeSource, make_chat, make_person, make_user_chat
from tg_bridge.deleted import SCAN_BUDGET, deleted_dialogs, identity_guesses, join_only_count, scan_deleted
from tg_bridge.errors import TelegramRefusedError
from tg_bridge.source import ContactCard

TZ = ZoneInfo("Europe/Warsaw")


def gone(id):
    return make_user_chat(make_person(id=id, first_name=None, last_name=None, username=None, is_deleted=True))


def test_deleted_dialogs_keeps_only_deleted_user_peers_sorted():
    group = make_chat(id=-5, kind="group", title="G")
    bot = make_user_chat(make_person(id=3, is_bot=True, is_deleted=True))
    normal = make_user_chat(make_person(id=4))

    assert [c.id for c in deleted_dialogs([gone(912), group, bot, normal, gone(777)])] == [777, 912]


def card(msg_id, year, user_id=777, first="Olena", last="P"):
    return ContactCard(msg_id, datetime(year, 5, 2, 12, tzinfo=UTC), user_id, first, last)


def test_identity_guesses_ignores_other_users_and_dedupes():
    cards = [
        card(9, 2021),
        card(8, 2021, user_id=555, first="Ivan", last=None),
        card(5, 2020),
        card(3, 2019, first="O.", last=None),
        card(2, 2018, first=None, last=None),
    ]

    assert identity_guesses(777, cards, TZ) == (
        "Olena P (name on a contact card, #9, 2021-05-02; a guess)",
        "O. (name on a contact card, #3, 2019-05-02; a guess)",
    )


def test_identity_guesses_capped_at_three():
    cards = [card(i, 2020, first=f"N{i}", last=None) for i in range(1, 6)]

    assert len(identity_guesses(777, cards, TZ)) == 3


class StatsFail(FakeSource):
    async def get_history_stats(self, chat):
        raise TelegramRefusedError("X")


async def test_scan_deleted_stats_error_gives_none():
    src = StatsFail(dialogs=[gone(777)])

    [item] = await scan_deleted(src, TZ)

    assert item.stats is None and item.chat.id == 777


def test_join_only_chats_are_left_out_and_counted():
    joined = make_user_chat(gone(500).peer, last_is_join_notice=True)
    chats = [gone(912), joined, gone(777)]

    assert [c.id for c in deleted_dialogs(chats)] == [777, 912]
    assert join_only_count(chats) == 1
    assert join_only_count([joined, make_user_chat(make_person(id=4), last_is_join_notice=True)]) == 1


async def test_scan_stops_checking_after_the_budget():
    ticks = iter([0.0, 0.0, SCAN_BUDGET + 1, SCAN_BUDGET + 2, SCAN_BUDGET + 3])
    src = FakeSource(dialogs=[gone(1), gone(2), gone(3)])

    items = await scan_deleted(src, TZ, clock=lambda: next(ticks))

    assert [(i.chat.id, i.checked) for i in items] == [(1, True), (2, False), (3, False)]
    assert items[1].stats is None
