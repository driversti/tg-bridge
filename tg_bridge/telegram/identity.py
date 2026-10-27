"""How our sessions appear in Telegram → Settings → Devices."""

import platform

from tg_bridge import __version__

_OS_NAMES = {"Darwin": "macOS"}


def client_identity() -> dict[str, str]:
    """TelegramClient kwargs. Telethon's defaults expose the CPU architecture and kernel version."""
    system = platform.system()
    return {
        "device_model": "tg-bridge",
        "system_version": _OS_NAMES.get(system, system),
        "app_version": __version__,
    }
