"""Test doubles shared across test modules."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from tg_bridge.errors import MessageNotFoundError
from tg_bridge.source import Chat, ChatInfo, Folder, MediaFile, MediaInfo, Message, MessageQuery, Person, SearchQuery


def make_person(**overrides) -> Person:
    fields = dict(
        id=42,
        first_name="Anna",
        last_name="K",
        username="anna_k",
        phone=None,
        is_deleted=False,
        is_bot=False,
    )
    fields.update(overrides)
    return Person(**fields)


def make_chat(**overrides) -> Chat:
    fields = dict(
        id=-1001234567890,
        kind="supergroup",
        title="Family",
        username=None,
        peer=None,
        last_message_at=datetime(2026, 10, 25, 12, 3, tzinfo=UTC),
        is_archived=False,
        is_forum=False,
    )
    fields.update(overrides)
    return Chat(**fields)


def make_user_chat(person: Person | None = None, **overrides) -> Chat:
    person = person or make_person()
    fields = dict(
        id=person.id,
        kind="bot" if person.is_bot else "user",
        title=person.display_name,
        username=person.username,
        peer=person,
    )
    fields.update(overrides)
    return make_chat(**fields)


def make_folder(**overrides) -> Folder:
    fields = dict(
        id=2,
        title="WAR",
        emoticon=None,
        kind="custom",
        contacts=False,
        non_contacts=False,
        groups=False,
        broadcasts=False,
        bots=False,
        exclude_muted=False,
        exclude_read=False,
        exclude_archived=False,
        pinned_ids=(),
        include_ids=(),
        exclude_ids=(),
    )
    fields.update(overrides)
    return Folder(**fields)


def make_message(**overrides) -> Message:
    fields = dict(
        id=1,
        chat_id=-1001234567890,
        date=datetime(2026, 10, 26, 7, 12, tzinfo=UTC),
        edit_date=None,
        out=False,
        sender=make_person(),
        sender_title=None,
        text="hello",
        reply_to_id=None,
        forward_from=None,
        topic_id=None,
        media=None,
        action=None,
    )
    fields.update(overrides)
    return Message(**fields)


def make_media_file(message_id: int = 4812, *, kind="photo", name=None, size=None, mime="image/jpeg", ext=".jpg", album_id=None):
    return MediaFile(
        message_id=message_id,
        album_id=album_id,
        media=MediaInfo(kind, name, size, mime, None, None),
        extension=ext,
    )


class FakeSource:
    """In-memory MessageSource."""

    def __init__(
        self,
        me: Person | None = None,
        error: Exception | None = None,
        dialogs: list[Chat] | None = None,
        folders: list[Folder] | None = None,
        infos: dict[int, ChatInfo] | None = None,
        messages: dict[int, list[Message]] | None = None,
        media: dict[tuple[int, int], tuple[MediaFile, bytes]] | None = None,
        chunk: int = 4,
    ):
        self.me = me or make_person()
        self.error = error
        self.closed = False
        self.dialogs = list(dialogs or [])
        self.folders = list(folders or [])
        self.infos = dict(infos or {})
        self.messages = {k: list(v) for k, v in (messages or {}).items()}
        self.message_queries: list[MessageQuery] = []
        self.search_queries: list[tuple[SearchQuery, int | None]] = []  # (query, searched chat id or None)
        self.list_dialogs_calls: list[bool] = []  # the `refresh` flag of each call
        self.list_folders_calls: list[bool] = []
        self.media = dict(media or {})  # (chat id, message id) -> (file, content)
        self.chunk = chunk
        self.read_offsets: list[tuple[int, int]] = []  # (message_id, offset) per read_media call

    async def whoami(self) -> Person:
        if self.error:
            raise self.error
        return self.me

    async def close(self) -> None:
        self.closed = True

    async def list_dialogs(self, *, refresh: bool = False) -> list[Chat]:
        self.list_dialogs_calls.append(refresh)
        if self.error:
            raise self.error
        return list(self.dialogs)

    async def list_folders(self, *, refresh: bool = False) -> list[Folder]:
        self.list_folders_calls.append(refresh)
        if self.error:
            raise self.error
        return list(self.folders)

    async def get_chat_info(self, chat: Chat) -> ChatInfo:
        if self.error:
            raise self.error
        return self.infos.get(chat.id) or ChatInfo(
            chat=chat, description=None, members_count=None, topics=(), topics_total=None
        )

    async def get_messages(self, chat: Chat, query: MessageQuery) -> list[Message]:
        """Behaves like Telegram: direction, period, id boundary, topic and from_me are applied in plain Python."""
        self.message_queries.append(query)
        if self.error:
            raise self.error
        found = sorted(self.messages.get(chat.id, []), key=lambda m: m.id)
        if query.start is not None:
            found = [m for m in found if m.date >= query.start]
        if query.end is not None:
            found = [m for m in found if m.date < query.end]
        if query.topic_id is not None:
            found = [m for m in found if m.topic_id == query.topic_id]
        if query.from_me:
            found = [m for m in found if m.out]
        if query.direction == "forward":
            if query.boundary_id is not None:
                found = [m for m in found if m.id > query.boundary_id]
            return found[: query.limit]
        if query.boundary_id is not None:
            found = [m for m in found if m.id < query.boundary_id]
        return found[::-1][: query.limit]

    async def search_messages(self, query: SearchQuery, chat: Chat | None) -> list[Message]:
        """Casefolded substring match, newest first by (date, chat_id, id)."""
        self.search_queries.append((query, chat.id if chat else None))
        if self.error:
            raise self.error
        pools = [chat.id] if chat else list(self.messages)
        needle = query.text.casefold()
        found = [m for cid in pools for m in self.messages.get(cid, []) if needle in m.text.casefold()]
        if query.start is not None:
            found = [m for m in found if m.date >= query.start]
        if query.end is not None:
            found = [m for m in found if m.date < query.end]
        found.sort(key=lambda m: (m.date, m.chat_id, m.id), reverse=True)
        if query.after is not None:
            a = query.after
            found = [m for m in found if (m.date, m.chat_id, m.id) < (a.date, a.chat_id, a.message_id)]
        if query.from_me:
            found = [m for m in found if m.out]
        return found[: query.limit]

    async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
        if self.error:
            raise self.error
        entry = self.media.get((chat.id, message_id))
        if entry is None:
            if any(m.id == message_id for m in self.messages.get(chat.id, [])):
                return []
            raise MessageNotFoundError(message_id)
        album_id = entry[0].album_id
        if album_id is None:
            return [entry[0]]
        siblings = [f for (cid, _), (f, _) in self.media.items() if cid == chat.id and f.album_id == album_id]
        return sorted(siblings, key=lambda f: f.message_id)

    async def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
        self.read_offsets.append((message_id, offset))
        if self.error:
            raise self.error
        data = self.media[(chat.id, message_id)][1][offset:]
        for i in range(0, len(data), self.chunk):
            yield data[i : i + self.chunk]
