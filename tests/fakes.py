"""Test doubles shared across test modules."""

from tg_bridge.source import Person


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


class FakeSource:
    """In-memory MessageSource."""

    def __init__(self, me: Person | None = None, error: Exception | None = None):
        self.me = me or make_person()
        self.error = error
        self.closed = False

    async def whoami(self) -> Person:
        if self.error:
            raise self.error
        return self.me

    async def close(self) -> None:
        self.closed = True
