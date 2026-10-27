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


# --- chats ---------------------------------------------------------------

from datetime import UTC, datetime, timedelta, timezone  # noqa: E402

from telethon.tl import types as tl  # noqa: E402

from tg_bridge.telegram.mapping import chat_from_dialog, topic_from_forum_topic  # noqa: E402


def dialog(entity, id=1, date=datetime(2026, 10, 25, 12, 3, tzinfo=UTC), folder_id=None):
    return SimpleNamespace(id=id, entity=entity, date=date, folder_id=folder_id)


def tl_user(**kw):
    return tl.User(**{"id": 1, "first_name": "Anna", "last_name": "K", "username": "anna_k", **kw})


def tl_channel(**kw):
    return tl.Channel(**{"id": 10, "title": "News", "photo": tl.ChatPhotoEmpty(), "date": None, **kw})


def test_user_dialog():
    chat = chat_from_dialog(dialog(tl_user(), id=1))

    assert (chat.id, chat.kind, chat.title, chat.username) == (1, "user", "Anna K", "anna_k")
    assert chat.peer.id == 1
    assert chat.last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)
    assert chat.is_archived is False and chat.is_forum is False


def test_bot_dialog():
    chat = chat_from_dialog(dialog(tl_user(bot=True, first_name="Helper", last_name=None)))

    assert chat.kind == "bot"
    assert chat.peer.is_bot is True


def test_deleted_user_dialog():
    chat = chat_from_dialog(dialog(tl_user(deleted=True, first_name=None, last_name=None, username=None)))

    assert chat.title == "Deleted Account"
    assert chat.deleted_peer is True
    assert chat.username is None


def test_basic_group_dialog():
    group = tl.Chat(id=5, title="Old", photo=tl.ChatPhotoEmpty(), participants_count=3, date=None, version=1)

    chat = chat_from_dialog(dialog(group, id=-5))

    assert (chat.id, chat.kind, chat.title, chat.peer, chat.username) == (-5, "group", "Old", None, None)


def test_supergroup_dialog():
    chat = chat_from_dialog(dialog(tl_channel(megagroup=True, username="famchat"), id=-1000000000010))

    assert (chat.kind, chat.username, chat.is_forum) == ("supergroup", "famchat", False)


def test_broadcast_channel_dialog():
    assert chat_from_dialog(dialog(tl_channel(broadcast=True))).kind == "channel"


def test_gigagroup_is_supergroup():
    assert chat_from_dialog(dialog(tl_channel(megagroup=True, gigagroup=True))).kind == "supergroup"


def test_forum_supergroup_dialog():
    chat = chat_from_dialog(dialog(tl_channel(megagroup=True, forum=True)))

    assert chat.is_forum is True


def test_chat_forbidden_has_no_username_or_date():
    chat = chat_from_dialog(dialog(tl.ChatForbidden(id=5, title="Kicked"), id=-5, date=None))

    assert (chat.kind, chat.title, chat.username, chat.last_message_at) == ("group", "Kicked", None, None)


def test_channel_forbidden_maps():
    forbidden = tl.ChannelForbidden(id=10, access_hash=1, title="Gone", broadcast=True)

    chat = chat_from_dialog(dialog(forbidden, date=None))

    assert (chat.kind, chat.title, chat.last_message_at) == ("channel", "Gone", None)


def test_archived_dialog():
    assert chat_from_dialog(dialog(tl_user(), folder_id=1)).is_archived is True
    assert chat_from_dialog(dialog(tl_user(), folder_id=0)).is_archived is False


def test_naive_dialog_date_treated_as_utc():
    chat = chat_from_dialog(dialog(tl_user(), date=datetime(2026, 10, 25, 12, 3)))

    assert chat.last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)


def test_aware_dialog_date_converted_to_utc():
    local = datetime(2026, 10, 25, 14, 3, tzinfo=timezone(timedelta(hours=2)))

    assert chat_from_dialog(dialog(tl_user(), date=local)).last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)


def forum_topic(**kw):
    fields = dict(
        id=23, date=None, peer=tl.PeerChannel(10), title="Trips", icon_color=0, top_message=0,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, unread_poll_votes_count=0, from_id=tl.PeerUser(1), notify_settings=None,
    )
    fields.update(kw)
    return tl.ForumTopic(**fields)


def test_forum_topic_maps():
    topic = topic_from_forum_topic(forum_topic(closed=True))

    assert (topic.id, topic.title, topic.is_closed) == (23, "Trips", True)
    assert topic_from_forum_topic(forum_topic()).is_closed is False


def test_deleted_forum_topic_is_none():
    assert topic_from_forum_topic(tl.ForumTopicDeleted(id=9)) is None
