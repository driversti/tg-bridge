"""Domain models. Telethon-free so tools and future sources (archive) share them."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Person:
    id: int
    first_name: str | None
    last_name: str | None
    username: str | None
    phone: str | None
    is_deleted: bool
    is_bot: bool
    is_contact: bool = False

    @property
    def display_name(self) -> str:
        if self.is_deleted:
            return "Deleted Account"
        name = " ".join(p.strip() for p in (self.first_name, self.last_name) if p and p.strip())
        return name or "(no name)"


ChatKind = Literal["user", "bot", "group", "supergroup", "channel"]


@dataclass(frozen=True)
class Chat:
    id: int  # Telethon "marked" id: users +N, basic groups -N, channels/supergroups -100N
    kind: ChatKind
    title: str  # 1:1 chats: the peer's display name
    username: str | None  # own public username; for 1:1 chats the peer's
    peer: Person | None  # 1:1 dialogs only
    last_message_at: datetime | None  # aware UTC
    is_archived: bool
    is_forum: bool
    # Dialog state, only what folder rules need; not printed in chat lines.
    is_muted: bool = False
    is_unread: bool = False
    has_unread_mention: bool = False
    is_self: bool = False  # the Saved Messages dialog
    is_creator: bool = False  # the owner created this group/channel; always False for users
    photo_id: int | None = None  # changes when the profile photo does; lets the admin page cache icons
    top_message_id: int | None = None  # newest message id: makes a t.me/c/<N>/<id> link that opens in Telegram

    @property
    def deleted_peer(self) -> bool:
        return self.peer is not None and self.peer.is_deleted


FolderKind = Literal["custom", "shared"]


@dataclass(frozen=True)
class Folder:
    id: int
    title: str  # plain text, custom emoji removed
    emoticon: str | None
    kind: FolderKind
    contacts: bool
    non_contacts: bool
    groups: bool
    broadcasts: bool
    bots: bool
    exclude_muted: bool
    exclude_read: bool
    exclude_archived: bool
    pinned_ids: tuple[int, ...]  # marked ids, folder order
    include_ids: tuple[int, ...]
    exclude_ids: tuple[int, ...]


@dataclass(frozen=True)
class Topic:
    id: int
    title: str
    is_closed: bool


@dataclass(frozen=True)
class ChatInfo:
    chat: Chat
    description: str | None  # channel/group "about", or user bio
    members_count: int | None  # None when hidden/unknown or 1:1
    topics: tuple[Topic, ...]  # empty unless forum
    topics_total: int | None  # Telegram's count when it exceeds len(topics)


MediaKind = Literal[
    "photo", "video", "gif", "voice", "audio", "video_note", "sticker", "document", "poll", "location", "contact", "other"
]


@dataclass(frozen=True)
class MediaInfo:
    kind: MediaKind
    file_name: str | None
    size: int | None  # bytes
    mime_type: str | None
    duration: int | None  # seconds
    label: str | None  # sticker emoji, poll question


# Telegram's upload.GetFile page size; offsets must be multiples of it.
MEDIA_CHUNK = 512 * 1024


@dataclass(frozen=True)
class MediaFile:
    """A message's downloadable file, as `download_media` needs it."""

    message_id: int
    album_id: int | None  # Telegram grouped_id
    media: MediaInfo
    extension: str  # ".jpg", ".pdf", "" when unknown


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: int
    date: datetime  # aware UTC
    edit_date: datetime | None  # aware UTC
    out: bool  # sent by the logged-in account
    sender: Person | None  # user senders
    sender_title: str | None  # channel / anonymous admin title, or channel post_author
    text: str  # "" when none
    reply_to_id: int | None  # None when it only points at the forum topic root
    forward_from: str | None  # display name of the original sender/chat, "hidden" if unknown
    topic_id: int | None
    media: MediaInfo | None
    action: str | None  # short service-message label, e.g. "joined", "pinned #4807"
    sender_username: str | None = None  # public @username of a channel that sent this (e.g. commenting as its channel)
    comment_count: int | None = None  # channel posts with a comment thread only; None = no comments possible


@dataclass(frozen=True)
class Post:
    """A channel post plus what is known about its comment thread."""

    message: Message  # the channel post itself
    comments_enabled: bool  # replies is MessageReplies with comments=True
    comment_count: int | None  # replies.replies; None when comments are off
    discussion_id: int | None  # marked id of the discussion supergroup (-100N); None when comments are off


@dataclass(frozen=True)
class MessageQuery:
    direction: Literal["forward", "backward"]
    start: datetime | None
    end: datetime | None
    boundary_id: int | None  # forward: ids > it; backward: ids < it
    topic_id: int | None
    from_me: bool
    limit: int


@dataclass(frozen=True)
class HistoryStats:
    """Size of a 1:1 history: Telegram's own total and the oldest message's date."""

    count: int
    first_at: datetime | None  # aware UTC; None when the history is empty


@dataclass(frozen=True)
class ContactCard:
    """A shared contact (MessageMediaContact): the name as written on the card, not the account's current one."""

    message_id: int
    date: datetime  # aware UTC
    user_id: int | None
    first_name: str | None
    last_name: str | None


@dataclass(frozen=True)
class SearchOffset:
    """"Continue with hits older than this one."""

    chat_id: int
    message_id: int
    date: datetime  # aware UTC


@dataclass(frozen=True)
class SearchQuery:
    text: str
    start: datetime | None
    end: datetime | None
    from_me: bool
    limit: int  # the tool asks for limit + 1, as for get_messages
    after: SearchOffset | None

