from tests.fakes import FakeSource, make_person
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
    await source.close()
    assert source.closed
