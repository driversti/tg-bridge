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

    @property
    def deleted_peer(self) -> bool:
        return self.peer is not None and self.peer.is_deleted


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
