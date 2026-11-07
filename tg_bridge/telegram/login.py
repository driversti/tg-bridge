"""Interactive login: by QR code or by login code, done step by step instead of client.start().

client.start() hides how Telegram delivered the code; when nothing arrives the user
can't tell an in-app code from SMS, a call or a required login email. The code flow says
where the code went and lets the user resend it. Telegram sometimes doesn't deliver codes
to third-party apps at all, so QR login (scan from a logged-in phone) is offered too.
"""

import getpass
from collections.abc import Callable
from typing import Literal

from telethon import TelegramClient, errors

from tg_bridge.config import Settings
from tg_bridge.errors import LoginError, RateLimitedError
from tg_bridge.source import Person
from tg_bridge.telegram.identity import client_identity
from tg_bridge.telegram.mapping import person_from_user

LoginMethod = Literal["qr", "code"]

QR_HINT = "Scan it with Telegram on your phone: Settings → Devices → Link Desktop Device."

_DELIVERY = {
    "SentCodeTypeApp": "Code sent to your Telegram app: open the 'Telegram' service chat "
    "(blue check) on a device where you are already logged in.",
    "SentCodeTypeSms": "Code sent by SMS.",
    "SentCodeTypeSmsWord": "Code sent by SMS (a word instead of digits).",
    "SentCodeTypeSmsPhrase": "Code sent by SMS (a phrase instead of digits).",
    "SentCodeTypeFirebaseSms": "Code sent by SMS.",
    "SentCodeTypeCall": "Telegram will call you and read the code.",
    "SentCodeTypeFlashCall": "Telegram will call you; the code is in the calling number.",
    "SentCodeTypeMissedCall": "Telegram will give you a missed call; the code is the last digits of that number.",
    "SentCodeTypeEmailCode": "Code sent to your login email.",
    "SentCodeTypeSetUpEmailRequired": "Telegram requires a login email first: set it in an official app "
    "(Settings → Privacy and Security → Login Email), then run this again.",
    "SentCodeTypeFragmentSms": "Code sent to your Fragment number: check fragment.com.",
}

_NEXT = {
    "CodeTypeSms": "SMS",
    "CodeTypeCall": "a phone call",
    "CodeTypeFlashCall": "a flash call",
    "CodeTypeMissedCall": "a missed call",
    "CodeTypeFragmentSms": "Fragment",
}


def describe_delivery(sent) -> str:
    name = type(sent.type).__name__
    text = _DELIVERY.get(name, f"Code sent (delivery: {name}).")
    if sent.next_type is not None:
        via = _NEXT.get(type(sent.next_type).__name__, type(sent.next_type).__name__)
        wait = f"If it doesn't arrive within {sent.timeout} s, p" if sent.timeout else "If it doesn't arrive, p"
        text += f" {wait}ress Enter at the code prompt to resend via {via}."
    return text


async def run_login_flow(
    client,
    ask: Callable[[str], str] = input,
    ask_secret: Callable[[str], str] = getpass.getpass,
    say: Callable[[str], None] = print,
) -> Person:
    """Authorize a connected client: phone → code (with resend) → optional 2FA password."""
    try:
        if not await client.is_user_authorized():
            phone = ask("Phone number in international format (e.g. +48123456789): ").strip()
            sent = await _send_code(client, phone)
            say(describe_delivery(sent))
            if await _enter_code(client, phone, sent, ask, say):
                await _enter_password(client, ask_secret, say)
        return person_from_user(await client.get_me())
    except errors.FloodError as e:
        raise RateLimitedError(int(getattr(e, "seconds", 0) or 0)) from e


async def _send_code(client, phone: str):
    try:
        # A repeated call for the same phone makes Telethon resend via the next method.
        return await client.send_code_request(phone)
    except (errors.PhoneNumberInvalidError, errors.PhoneNumberBannedError, errors.PhoneNumberFloodError) as e:
        raise LoginError(f"Telegram rejected the phone number ({type(e).__name__}).") from e
    except errors.SendCodeUnavailableError as e:
        raise LoginError(
            "Telegram can't send the code another way. Try: uv run python -m tg_bridge.login --method qr"
        ) from e


async def _enter_code(client, phone: str, sent, ask, say) -> bool:
    """Returns True when Telegram asks for the 2FA password next."""
    while True:
        code = ask("Code (press Enter to resend): ").strip()
        if not code:
            sent = await _send_code(client, phone)
            say(describe_delivery(sent))
            continue
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            return False
        except errors.PhoneCodeInvalidError:
            say("Wrong code, try again.")
        except errors.PhoneCodeExpiredError:
            say("Code expired; sending a new one.")
            sent = await _send_code(client, phone)
            say(describe_delivery(sent))
        except errors.SessionPasswordNeededError:
            return True


async def _enter_password(client, ask_secret, say) -> None:
    while True:
        try:
            await client.sign_in(password=ask_secret("Two-step verification password: "))
            return
        except errors.PasswordHashInvalidError:
            say("Wrong password, try again.")


async def run_qr_flow(
    client,
    show_qr: Callable[[str], None],
    ask_secret: Callable[[str], str] = getpass.getpass,
    say: Callable[[str], None] = print,
    attempts: int = 5,
) -> Person:
    """Authorize by QR: show a login token, wait for a scan, refresh it when it expires (~30 s)."""
    try:
        if not await client.is_user_authorized():
            await _scan_qr(client, show_qr, ask_secret, say, attempts)
        return person_from_user(await client.get_me())
    except errors.FloodError as e:
        raise RateLimitedError(int(getattr(e, "seconds", 0) or 0)) from e


async def _scan_qr(client, show_qr, ask_secret, say, attempts: int) -> None:
    qr = await client.qr_login()
    for attempt in range(attempts):
        if attempt:
            await qr.recreate()
            say("QR code expired, here is a new one.")
        show_qr(qr.url)
        say(QR_HINT)
        try:
            await qr.wait()
            return
        except TimeoutError:
            continue
        except errors.SessionPasswordNeededError:
            await _enter_password(client, ask_secret, say)
            return
    raise LoginError("QR code was not scanned in time. Run the login again.")


async def interactive_login(settings: Settings, method: LoginMethod, show_qr: Callable[[str], None] = print) -> Person:
    # QR login completes via an UpdateLoginToken update, so only that method needs the update
    # stream. This is the short-lived login process; the MCP server never receives updates.
    client = TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        receive_updates=(method == "qr"),
        **client_identity(settings.device_name),
    )
    await client.connect()
    try:
        if method == "qr":
            return await run_qr_flow(client, show_qr)
        return await run_login_flow(client)
    finally:
        await client.disconnect()
