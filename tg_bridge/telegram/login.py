"""Interactive login. The only place allowed to use client.start(): it prompts on the terminal."""

from telethon import TelegramClient

from tg_bridge.config import Settings
from tg_bridge.source import Person
from tg_bridge.telegram.mapping import person_from_user


async def interactive_login(settings: Settings) -> Person:
    client = TelegramClient(
        str(settings.session_path), settings.api_id, settings.api_hash, receive_updates=False
    )
    try:
        await client.start()  # asks for phone, code and 2FA password; no-op if already authorized
        return person_from_user(await client.get_me())
    finally:
        await client.disconnect()
