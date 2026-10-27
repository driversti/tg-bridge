import pytest

from tests.fakes import make_chat, make_person, make_user_chat
from tg_bridge.errors import AmbiguousChatError, ChatNotFoundError
from tg_bridge.resolve import match_chats, resolve_chat

CHANNEL_ID = -1001234567890


def test_by_marked_id():
    chat = make_chat(id=CHANNEL_ID)

    assert resolve_chat("-1001234567890", [make_chat(id=-5, title="Other"), chat]) is chat


def test_by_bare_channel_id():
    chat = make_chat(id=CHANNEL_ID)

    assert resolve_chat("1234567890", [chat]) is chat


def test_by_bare_basic_group_id():
    chat = make_chat(id=-555, kind="group", title="Old group")

    assert resolve_chat("555", [chat]) is chat


def test_negative_id_not_in_dialogs_is_not_found():
    with pytest.raises(ChatNotFoundError):
        resolve_chat("-777", [make_chat(id=-5)])


def test_by_username_case_insensitive():
    chat = make_chat(username="famchat")

    assert resolve_chat("@FamChat", [make_chat(id=-1, title="x"), chat]) is chat


def test_by_peer_username_for_user_chat():
    chat = make_user_chat(make_person(id=7, username="anna_k"))

    assert resolve_chat("@Anna_K", [chat]) is chat


def test_by_tme_link():
    chat = make_chat(username="famchat")

    assert resolve_chat("https://t.me/famchat", [chat]) is chat


def test_unknown_username_suggests_close_ones():
    chat = make_chat(username="famchat")

    with pytest.raises(ChatNotFoundError) as exc:
        resolve_chat("@famchatt", [chat])

    assert exc.value.suggestions == [chat]


def test_exact_title_beats_fragment():
    family = make_chat(id=-1, title="Family")
    trip = make_chat(id=-2, title="Family trip")

    assert resolve_chat("family", [trip, family]) is family


def test_unique_fragment():
    family = make_chat(id=-1, title="Family")
    trip = make_chat(id=-2, title="Family trip")

    assert resolve_chat("trip", [family, trip]) is trip


def test_tokens_any_order():
    trip = make_chat(id=-2, title="Family trip")

    assert resolve_chat("trip family", [make_chat(id=-1, title="Work"), trip]) is trip


def test_cyrillic_case_insensitive():
    chat = make_chat(title="Сім'я 🏠")

    assert resolve_chat("СІМ'Я", [chat]) is chat


def test_emoji_and_punctuation_ignored():
    chat = make_chat(title="Dev-ops!! 🚀 team")

    assert resolve_chat("dev ops team", [chat]) is chat


def test_ambiguous_fragment_returns_candidates():
    a = make_chat(id=-1, title="Family")
    b = make_chat(id=-2, title="family 🏠")

    with pytest.raises(AmbiguousChatError) as exc:
        resolve_chat("family", [a, b])

    assert exc.value.candidates == [a, b]


def test_two_deleted_accounts_ambiguous():
    gone = make_person(first_name=None, last_name=None, username=None, is_deleted=True)
    a = make_user_chat(make_person(**{**gone.__dict__, "id": 1}))
    b = make_user_chat(make_person(**{**gone.__dict__, "id": 2}))

    with pytest.raises(AmbiguousChatError) as exc:
        resolve_chat("deleted account", [a, b])

    assert exc.value.candidates == [a, b]


def test_not_found_suggestions_ranked_and_capped_at_5():
    chats = [make_chat(id=-i, title=f"Familyx{'y' * i}") for i in range(1, 9)]
    chats.append(make_chat(id=-100, title="Famly"))

    with pytest.raises(ChatNotFoundError) as exc:
        resolve_chat("familz", chats)

    assert len(exc.value.suggestions) == 5
    assert exc.value.suggestions[0].title == "Famly"


def test_not_found_without_close_matches_has_empty_suggestions():
    with pytest.raises(ChatNotFoundError) as exc:
        resolve_chat("qqqqqq", [make_chat(title="Family")])

    assert exc.value.suggestions == []


def test_match_chats_returns_all_in_order():
    a = make_chat(id=-1, title="Trip one")
    b = make_chat(id=-2, title="Work")
    c = make_chat(id=-3, title="Trip two")

    assert match_chats("trip", [a, b, c]) == [a, c]


def test_blank_arg_is_not_found():
    with pytest.raises(ChatNotFoundError):
        resolve_chat("   ", [make_chat()])


def test_error_str_has_no_names():
    chat = make_chat(title="Secret Club")

    with pytest.raises(AmbiguousChatError) as amb:
        resolve_chat("secret", [chat, make_chat(id=-2, title="Secret Club 2")])
    with pytest.raises(ChatNotFoundError) as nf:
        resolve_chat("secret clubz", [chat])

    assert str(amb.value) == "chat is ambiguous"
    assert str(nf.value) == "chat not found"
