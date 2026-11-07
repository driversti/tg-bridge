"""Send the admin login link through the owner's Homelab bot (Bot API; a bot speaking, not the account).

The bot token is part of the request URL, and `urllib` errors carry that URL, so failures are logged by
exception class name or status code only.
"""

import json
import logging
import urllib.request
from collections.abc import Callable

from tg_bridge.config import Settings

log = logging.getLogger(__name__)

POST_TIMEOUT = 10  # seconds


def admin_link(settings: Settings, token: str) -> str:
    base = settings.admin_url or f"http://127.0.0.1:{settings.admin_port}"
    return f"{base}/login?token={token}"


def _post(url: str, body: bytes) -> int:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=POST_TIMEOUT) as response:  # noqa: S310 - fixed https URL
        return response.status


def send_login_link(settings: Settings, link: str, post: Callable[[str, bytes], int] = _post) -> bool:
    if settings.notify_bot_token is None or settings.notify_chat_id is None:
        return False
    payload = {
        "chat_id": settings.notify_chat_id,
        "text": f"tg-bridge started on {settings.device_name}.\nAdmin page (Tailscale): {link}",
        # Telegram's servers must never fetch a URL that contains the token.
        "link_preview_options": {"is_disabled": True},
    }
    url = f"https://api.telegram.org/bot{settings.notify_bot_token}/sendMessage"
    try:
        status = post(url, json.dumps(payload).encode())
    except Exception as e:  # noqa: BLE001 - any failure must leave the process running
        log.warning("login link not sent: %s", type(e).__name__)
        return False
    if status != 200:
        log.warning("login link not sent: %s", status)
        return False
    return True
