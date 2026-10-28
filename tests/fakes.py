"""Test doubles shared across test modules."""

from datetime import UTC, datetime

from tg_bridge.source import Chat, ChatInfo, Message, MessageQuery, Person, SearchOffset, SearchPage, SearchQuery


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


class FakeSource:
    """In-memory MessageSource."""

    def __init__(
        self,
        me: Person | None = None,
        error: Exception | None = None,
        dialogs: list[Chat] | None = None,
        infos: dict[int, ChatInfo] | None = None,
        messages: dict[int, list[Message]] | None = None,
        scan_cap: int | None = None,
    ):
        self.me = me or make_person()
        self.error = error
        self.closed = False
        self.dialogs = list(dialogs or [])
        self.infos = dict(infos or {})
        self.messages = {k: list(v) for k, v in (messages or {}).items()}
        self.message_queries: list[MessageQuery] = []
        self.scan_cap = scan_cap
        self.search_queries: list[tuple[SearchQuery, int | None]] = []  # (query, searched chat id or None)
        self.list_dialogs_calls: list[bool] = []  # the `refresh` flag of each call

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

    async def search_messages(self, query: SearchQuery, chat: Chat | None) -> SearchPage:
        """Casefolded substring match, newest first by (date, chat_id, id); `scan_cap` mimics the live global cap."""
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
        if query.from_me and chat is not None:
            found = [m for m in found if m.out]
            return SearchPage(messages=found[: query.limit], resume=None)
        hits: list[Message] = []
        scanned = 0
        for i, m in enumerate(found):
            if len(hits) >= query.limit:
                break
            if self.scan_cap is not None and query.from_me and scanned >= self.scan_cap:
                last = found[i - 1]
                return SearchPage(hits, SearchOffset(chat_id=last.chat_id, message_id=last.id, date=last.date))
            scanned += 1
            if not query.from_me or m.out:
                hits.append(m)
        return SearchPage(messages=hits, resume=None)
