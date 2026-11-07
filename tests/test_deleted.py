from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tests.fakes import FakeSource, make_chat, make_person, make_user_chat
from tg_bridge.deleted import deleted_dialogs, identity_guesses, scan_deleted
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
