"""Test doubles shared across test modules."""

from datetime import UTC, datetime

from tg_bridge.source import Chat, ChatInfo, Person


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


class FakeSource:
    """In-memory MessageSource."""

    def __init__(
        self,
        me: Person | None = None,
        error: Exception | None = None,
        dialogs: list[Chat] | None = None,
        infos: dict[int, ChatInfo] | None = None,
    ):
        self.me = me or make_person()
        self.error = error
        self.closed = False
        self.dialogs = list(dialogs or [])
        self.infos = dict(infos or {})
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
