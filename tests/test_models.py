from tests.fakes import FakeSource, make_chat, make_person, make_user_chat
from tg_bridge.source import MessageSource


def test_display_name_full():
    assert make_person(first_name="Anna", last_name="K").display_name == "Anna K"


def test_display_name_first_only():
    assert make_person(first_name="Anna", last_name=None).display_name == "Anna"


def test_display_name_deleted():
    person = make_person(first_name=None, last_name=None, is_deleted=True)

    assert person.display_name == "Deleted Account"


def test_display_name_empty():
    assert make_person(first_name=" ", last_name=None).display_name == "(no name)"


async def test_fake_source_satisfies_protocol():
    source: MessageSource = FakeSource()

    assert (await source.whoami()).id == 42
    chat = make_chat()
    assert await source.list_dialogs() == []
    assert (await source.get_chat_info(chat)).chat == chat
    await source.close()
    assert source.closed


def test_deleted_peer_true_for_deleted_user_chat():
    person = make_person(first_name=None, last_name=None, username=None, is_deleted=True)

    assert make_user_chat(person).deleted_peer is True


def test_deleted_peer_false_for_group():
    assert make_chat().deleted_peer is False
    assert make_user_chat().deleted_peer is False
