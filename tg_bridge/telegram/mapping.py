"""Telethon objects → domain models."""

from datetime import UTC, datetime

from telethon.tl import types as tl

from tg_bridge.source import Chat, ChatKind, Person, Topic


def person_from_user(user) -> Person:
    return Person(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        phone=user.phone,
        is_deleted=bool(user.deleted),
        is_bot=bool(user.bot),
    )


def chat_from_dialog(dialog) -> Chat:
    entity = dialog.entity
    peer: Person | None = None
    forum = False
    if isinstance(entity, tl.User):
        peer = person_from_user(entity)
        kind: ChatKind = "bot" if peer.is_bot else "user"
        title, username = peer.display_name, peer.username
    elif isinstance(entity, (tl.Channel, tl.ChannelForbidden)):
        kind = "channel" if entity.broadcast else "supergroup"
        title, username = entity.title, getattr(entity, "username", None)
        forum = bool(getattr(entity, "forum", False))
    else:  # Chat, ChatForbidden
        kind = "group"
        title, username = entity.title, None
    return Chat(
        id=dialog.id,
        kind=kind,
        title=title,
        username=username,
        peer=peer,
        last_message_at=_utc(getattr(dialog, "date", None)),
        is_archived=getattr(dialog, "folder_id", None) == 1,
        is_forum=forum,
    )


def topic_from_forum_topic(topic) -> Topic | None:
    if isinstance(topic, tl.ForumTopicDeleted):
        return None
    return Topic(id=topic.id, title=topic.title, is_closed=bool(topic.closed))


def _utc(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    # Telethon hands out naive datetimes that are already UTC.
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
