from types import SimpleNamespace

from tg_bridge.telegram.mapping import person_from_user


def user(**overrides):
    fields = dict(id=1, first_name="Anna", last_name="K", username="anna_k", phone="48123", deleted=False, bot=False)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_maps_all_fields():
    person = person_from_user(user())

    assert (person.id, person.first_name, person.last_name, person.username, person.phone) == (
        1, "Anna", "K", "anna_k", "48123",
    )
    assert person.is_deleted is False
    assert person.is_bot is False


def test_deleted_user():
    person = person_from_user(user(first_name=None, last_name=None, username=None, deleted=True))

    assert person.is_deleted is True
    assert person.display_name == "Deleted Account"


def test_none_flags_become_false():
    person = person_from_user(user(deleted=None, bot=None))

    assert person.is_deleted is False
    assert person.is_bot is False
