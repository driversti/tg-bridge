"""Telethon objects → domain models."""

import re
from datetime import UTC, datetime

from telethon.tl import types as tl

from tg_bridge.source import Chat, ChatKind, MediaInfo, MediaKind, Message, Person, Topic


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


def message_from_tl(msg, chat_id: int) -> Message | None:
    """None for MessageEmpty (a deleted or inaccessible id)."""
    if isinstance(msg, tl.MessageEmpty):
        return None
    sender = getattr(msg, "sender", None)
    person: Person | None = None
    title: str | None = getattr(msg, "post_author", None) or None
    if isinstance(sender, tl.User):
        person = person_from_user(sender)
        title = None
    elif title is None and sender is not None:
        title = getattr(sender, "title", None)
    topic_id, reply_to_id = _topic_and_reply(msg.reply_to)
    is_service = isinstance(msg, tl.MessageService)
    return Message(
        id=msg.id,
        chat_id=chat_id,
        date=_utc(msg.date),
        edit_date=_utc(getattr(msg, "edit_date", None)),
        out=bool(msg.out),
        sender=person,
        sender_title=title,
        text="" if is_service else (msg.message or ""),
        reply_to_id=reply_to_id,
        forward_from=_forward_origin(msg),
        topic_id=topic_id,
        media=None if is_service else _media(msg),
        action=_action(msg) if is_service else None,
    )


def _topic_and_reply(header) -> tuple[int | None, int | None]:
    if not isinstance(header, tl.MessageReplyHeader):
        return None, None
    replied, top = header.reply_to_msg_id, header.reply_to_top_id
    if not header.forum_topic:
        return None, replied
    topic = top or replied
    # In a forum a "reply" to the topic's first message is just how Telegram marks topic membership.
    return topic, (None if replied == topic else replied)


def _forward_origin(msg) -> str | None:
    header = getattr(msg, "fwd_from", None)
    if header is None:
        return None
    if header.from_name:
        return header.from_name
    forward = msg.forward
    if forward is not None:
        if getattr(forward.chat, "title", None):
            return forward.chat.title
        if isinstance(forward.sender, tl.User):
            return person_from_user(forward.sender).display_name
    return "hidden"


def _media(msg) -> MediaInfo | None:
    media = msg.media
    kind: MediaKind
    label: str | None = None
    if isinstance(media, tl.MessageMediaPoll):
        question = media.poll.question
        return MediaInfo("poll", None, None, None, None, getattr(question, "text", question))
    if isinstance(media, tl.MessageMediaContact):
        return MediaInfo("contact", None, None, None, None, None)
    if isinstance(media, (tl.MessageMediaGeo, tl.MessageMediaGeoLive, tl.MessageMediaVenue)):
        return MediaInfo("location", None, None, None, None, None)
    # Convenience properties like msg.photo also match web previews, so gate on the media type itself.
    if isinstance(media, tl.MessageMediaPhoto) and isinstance(media.photo, tl.Photo):
        kind = "photo"
    elif isinstance(media, tl.MessageMediaDocument) and isinstance(media.document, tl.Document):
        if msg.gif:
            kind = "gif"
        elif msg.video_note:
            kind = "video_note"
        elif msg.voice:
            kind = "voice"
        elif msg.audio:
            kind = "audio"
        elif msg.sticker:
            kind, label = "sticker", msg.file.emoji
        elif msg.video:
            kind = "video"
        else:
            kind = "document"
    elif media is None or isinstance(media, tl.MessageMediaWebPage):
        return None
    else:
        return MediaInfo("other", None, None, None, None, None)
    file = msg.file
    # Video durations arrive as floats (layer's `double`); the model holds whole seconds.
    duration = None if file.duration is None else round(file.duration)
    return MediaInfo(kind, file.name, file.size, file.mime_type, duration, label)


def _action(msg) -> str:
    action, sender_id = msg.action, msg.sender_id
    if isinstance(action, tl.MessageActionChatAddUser):
        if list(action.users) == [sender_id]:
            return "joined"
        return f"added {len(action.users)} members"
    if isinstance(action, (tl.MessageActionChatJoinedByLink, tl.MessageActionChatJoinedByRequest)):
        return "joined via link" if isinstance(action, tl.MessageActionChatJoinedByLink) else "joined"
    if isinstance(action, tl.MessageActionChatDeleteUser):
        return "left" if action.user_id == sender_id else "removed a member"
    if isinstance(action, tl.MessageActionPinMessage):
        replied = getattr(msg.reply_to, "reply_to_msg_id", None)
        return f"pinned #{replied}" if replied else "pinned a message"
    if isinstance(action, tl.MessageActionChatEditTitle):
        return f'renamed to "{action.title}"'
    if isinstance(action, tl.MessageActionChatEditPhoto):
        return "changed photo"
    if isinstance(action, (tl.MessageActionChatCreate, tl.MessageActionChannelCreate)):
        return "created chat"
    if isinstance(action, tl.MessageActionTopicCreate):
        return f'created topic "{action.title}"'
    if isinstance(action, tl.MessageActionGroupCall):
        return "voice chat"
    name = type(action).__name__.removeprefix("MessageAction")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
