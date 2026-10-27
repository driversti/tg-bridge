"""Telethon objects → domain models."""

from tg_bridge.source import Person


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
