"""Telethon objects → domain models."""

import dataclasses
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime

from telethon import helpers, utils
from telethon.tl import types as tl

from tg_bridge.source import Chat, ChatKind, Folder, MediaFile, MediaInfo, MediaKind, Message, Person, Post, Topic

log = logging.getLogger(__name__)


def person_from_user(user) -> Person:
    return Person(
        id=user.id,
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        phone=user.phone,
        is_deleted=bool(user.deleted),
        is_bot=bool(user.bot),
        is_contact=bool(getattr(user, "contact", False)),
    )


def chat_from_dialog(
    dialog, muted_by_default: Mapping[str, datetime | None] | None = None, now: datetime | None = None
) -> Chat:
    """`muted_by_default` holds the account-wide mute_until per category ("users", "chats", "broadcasts")."""
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
    raw = getattr(dialog, "dialog", None)  # Telethon's wrapper around the raw tl.Dialog; absent in some fakes
    mentions = getattr(raw, "unread_mentions_count", 0) or 0
    unread = (
        (getattr(raw, "unread_count", 0) or 0) > 0
        or bool(getattr(raw, "unread_mark", False))
        or mentions > 0
        or (getattr(raw, "unread_reactions_count", 0) or 0) > 0
    )
    own_mute = getattr(getattr(raw, "notify_settings", None), "mute_until", None)
    # An own setting, even an expired one, beats the account default for the category.
    mute_until = own_mute if own_mute is not None else (muted_by_default or {}).get(_mute_category(kind))
    return Chat(
        id=dialog.id,
        kind=kind,
        title=title,
        username=username,
        peer=peer,
        last_message_at=_utc(getattr(dialog, "date", None)),
        is_archived=getattr(dialog, "folder_id", None) == 1,
        is_forum=forum,
        is_muted=_is_muted(mute_until, now),
        is_unread=unread,
        has_unread_mention=mentions > 0,
    )


def _mute_category(kind: ChatKind) -> str:
    return {"user": "users", "bot": "users", "group": "chats", "supergroup": "chats", "channel": "broadcasts"}[kind]


def _is_muted(mute_until: datetime | None, now: datetime | None) -> bool:
    until = _utc(mute_until)
    return until is not None and until > (now or datetime.now(UTC))


def folder_from_tl(raw, self_id: int) -> Folder | None:
    """None for the built-in "All chats" entry and for filter types this layer doesn't know."""
    if isinstance(raw, tl.DialogFilter):
        custom = True
    elif isinstance(raw, tl.DialogFilterChatlist):
        custom = False  # shared folders have no rules and no excludes
    else:
        return None

    def flag(name: str) -> bool:
        return custom and bool(getattr(raw, name, False))

    return Folder(
        id=raw.id,
        title=_plain_title(raw.title, raw.id),
        emoticon=raw.emoticon or None,
        kind="custom" if custom else "shared",
        contacts=flag("contacts"),
        non_contacts=flag("non_contacts"),
        groups=flag("groups"),
        broadcasts=flag("broadcasts"),
        bots=flag("bots"),
        exclude_muted=flag("exclude_muted"),
        exclude_read=flag("exclude_read"),
        exclude_archived=flag("exclude_archived"),
        pinned_ids=_peer_ids(raw.pinned_peers, self_id),
        include_ids=_peer_ids(raw.include_peers, self_id),
        exclude_ids=_peer_ids(raw.exclude_peers, self_id) if custom else (),
    )


def _peer_ids(peers, self_id: int) -> tuple[int, ...]:
    ids: list[int] = []
    skipped = 0
    for peer in peers:
        if isinstance(peer, tl.InputPeerSelf):
            ids.append(self_id)
        elif isinstance(peer, (tl.InputPeerUser, tl.InputPeerChat, tl.InputPeerChannel)):
            ids.append(utils.get_peer_id(peer))
        else:
            skipped += 1
    if skipped:
        log.warning("skipped %d folder peers of unsupported type", skipped)
    return tuple(ids)


def _plain_title(title, folder_id: int) -> str:
    """Folder titles are TextWithEntities since layer 160-ish; custom emoji spans can't be shown as text."""
    if isinstance(title, str):
        text = title
    else:
        # Entity offsets count UTF-16 units, so cut on the surrogate-encoded text, back to front.
        text = helpers.add_surrogate(title.text)
        spans = sorted(
            ((e.offset, e.length) for e in title.entities if isinstance(e, tl.MessageEntityCustomEmoji)), reverse=True
        )
        for offset, length in spans:
            text = text[:offset] + text[offset + length :]
        text = helpers.del_surrogate(text)
    return " ".join(text.split()) or f"(folder {folder_id})"


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


def comment_from_tl(msg, chat_id: int) -> Message | None:
    """A message of a channel post's comment thread; `chat_id` is the discussion group's marked id.

    A top-level comment is technically a reply to the post's copy in the discussion group (the thread root), so
    it carries no reply of its own; only a reply to another comment (reply_to_top_id set) keeps its target.
    """
    mapped = message_from_tl(msg, chat_id)
    if mapped is None:
        return None
    header = msg.reply_to if isinstance(msg.reply_to, tl.MessageReplyHeader) else None
    reply = None if header is None or header.reply_to_top_id is None else header.reply_to_msg_id
    return dataclasses.replace(mapped, reply_to_id=reply, topic_id=None)


def post_from_tl(msg, chat_id: int) -> Post | None:
    """None for MessageEmpty and service messages (they have no comment thread)."""
    if isinstance(msg, (tl.MessageEmpty, tl.MessageService)):
        return None
    mapped = message_from_tl(msg, chat_id)
    replies = getattr(msg, "replies", None)
    if not isinstance(replies, tl.MessageReplies) or not replies.comments or replies.channel_id is None:
        return Post(mapped, comments_enabled=False, comment_count=None, discussion_id=None)
    return Post(
        mapped,
        comments_enabled=True,
        comment_count=replies.replies,
        discussion_id=utils.get_peer_id(tl.PeerChannel(replies.channel_id)),
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


# Python's mimetypes has no entry (or the odd ".oga") for these.
_EXTENSION_BY_MIME = {"audio/ogg": ".ogg", "application/x-tgsticker": ".tgs"}


def largest_photo_size(photo) -> tuple[str, int] | None:
    """(size type, bytes) of the biggest size that can really be downloaded; never the stripped/cached thumbnails
    (Telethon's own pick, `sizes[-1]`, can be the stripped one: a 1 KB blur)."""
    best: tuple[str, int] | None = None
    for size in photo.sizes:
        if isinstance(size, tl.PhotoSize):
            nbytes = size.size
        elif isinstance(size, tl.PhotoSizeProgressive):
            nbytes = max(size.sizes)
        else:
            continue
        if best is None or nbytes > best[1]:
            best = (size.type, nbytes)
    return best


def media_file_from_tl(msg) -> MediaFile | None:
    """None when the message has no media (web-page previews included)."""
    if isinstance(msg, (tl.MessageEmpty, tl.MessageService)):
        return None
    info = _media(msg)
    if info is None:
        return None
    if info.kind == "photo":
        # msg.file.size is an estimate; the limit and the short-stream check need what is really fetched.
        largest = largest_photo_size(msg.media.photo)
        if largest is not None:
            info = dataclasses.replace(info, size=largest[1])
        extension = ".jpg"
    else:
        extension = _EXTENSION_BY_MIME.get(info.mime_type or "") or utils.get_extension(msg.media)
    return MediaFile(message_id=msg.id, album_id=msg.grouped_id, media=info, extension=extension)


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
