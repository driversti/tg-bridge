import pytest
from telethon import errors
from telethon.tl.types import auth

from tests.telegram.test_mapping import user
from tg_bridge.errors import LoginError, RateLimitedError
from tg_bridge.telegram.login import describe_delivery, run_login_flow, run_qr_flow


def sent(code_type, next_type=None, timeout=None):
    return auth.SentCode(type=code_type, phone_code_hash="hash", next_type=next_type, timeout=timeout)


class FakeAuthClient:
    def __init__(self, sends, sign_ins=(), authorized=False):
        self.sends = list(sends)  # SentCode or exception per send_code_request call
        self.sign_ins = list(sign_ins)  # None (ok) or exception per sign_in call
        self.authorized = authorized
        self.calls = []

    async def is_user_authorized(self):
        return self.authorized

    async def send_code_request(self, phone):
        self.calls.append(("send_code_request", phone))
        result = self.sends.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def sign_in(self, **kwargs):
        self.calls.append(("sign_in", kwargs))
        result = self.sign_ins.pop(0) if self.sign_ins else None
        if isinstance(result, Exception):
            raise result

    async def get_me(self):
        return user(id=42)


class Console:
    def __init__(self, answers, secrets=()):
        self.answers, self.secrets, self.said = list(answers), list(secrets), []

    def ask(self, prompt):
        return self.answers.pop(0)

    def ask_secret(self, prompt):
        return self.secrets.pop(0)

    def say(self, text):
        self.said.append(text)


async def run(client, console):
    return await run_login_flow(client, ask=console.ask, ask_secret=console.ask_secret, say=console.say)


def test_describe_app_delivery_points_to_service_chat():
    text = describe_delivery(sent(auth.SentCodeTypeApp(length=5), auth.CodeTypeSms(), timeout=60))

    assert "Telegram app" in text
    assert "60 s" in text and "SMS" in text


def test_describe_email_setup_required():
    text = describe_delivery(sent(auth.SentCodeTypeSetUpEmailRequired()))

    assert "Login Email" in text


def test_describe_unknown_type_names_it():
    text = describe_delivery(sent(auth.SentCodeTypeSmsWord()))

    assert "SMS" in text


async def test_already_authorized_skips_code():
    client = FakeAuthClient(sends=[], authorized=True)

    person = await run(client, Console([]))

    assert person.id == 42
    assert client.calls == []


async def test_reports_delivery_and_signs_in():
    client = FakeAuthClient(sends=[sent(auth.SentCodeTypeApp(length=5))])
    console = Console([" +48 123 ", "12345"])

    person = await run(client, console)

    assert person.id == 42
    assert client.calls[0] == ("send_code_request", "+48 123")
    assert client.calls[1] == ("sign_in", {"phone": "+48 123", "code": "12345", "phone_code_hash": "hash"})
    assert any("Telegram app" in s for s in console.said)


async def test_empty_code_resends():
    client = FakeAuthClient(sends=[sent(auth.SentCodeTypeApp(length=5), auth.CodeTypeSms()), sent(auth.SentCodeTypeSms(length=5))])
    console = Console(["+48", "", "12345"])

    await run(client, console)

    assert [c[0] for c in client.calls] == ["send_code_request", "send_code_request", "sign_in"]
    assert any("SMS" in s for s in console.said[1:])


async def test_wrong_code_asks_again():
    client = FakeAuthClient(
        sends=[sent(auth.SentCodeTypeApp(length=5))],
        sign_ins=[errors.PhoneCodeInvalidError(request=None), None],
    )
    console = Console(["+48", "11111", "12345"])

    await run(client, console)

    assert any("Wrong code" in s for s in console.said)


async def test_two_factor_password():
    client = FakeAuthClient(
        sends=[sent(auth.SentCodeTypeApp(length=5))],
        sign_ins=[errors.SessionPasswordNeededError(request=None), errors.PasswordHashInvalidError(request=None), None],
    )
    console = Console(["+48", "12345"], secrets=["bad", "good"])

    await run(client, console)

    assert client.calls[-1] == ("sign_in", {"password": "good"})
    assert any("Wrong password" in s for s in console.said)


async def test_invalid_phone_raises_login_error():
    client = FakeAuthClient(sends=[errors.PhoneNumberInvalidError(request=None)])

    with pytest.raises(LoginError, match="phone number"):
        await run(client, Console(["123"]))


async def test_flood_wait_raises_rate_limited():
    client = FakeAuthClient(sends=[errors.FloodWaitError(request=None, capture=300)])

    with pytest.raises(RateLimitedError) as exc:
        await run(client, Console(["+48"]))

    assert exc.value.seconds == 300


async def test_resend_unavailable_suggests_qr():
    client = FakeAuthClient(sends=[sent(auth.SentCodeTypeApp(length=5)), errors.SendCodeUnavailableError(request=None)])

    with pytest.raises(LoginError, match="--method qr"):
        await run(client, Console(["+48", ""]))


class FakeQr:
    def __init__(self, waits):
        self.waits = list(waits)  # None (scanned) or exception per wait()
        self.recreated = 0

    @property
    def url(self):
        return f"tg://login?token=t{self.recreated}"

    async def wait(self):
        result = self.waits.pop(0)
        if isinstance(result, Exception):
            raise result

    async def recreate(self):
        self.recreated += 1


class FakeQrClient(FakeAuthClient):
    def __init__(self, waits, sign_ins=(), authorized=False):
        super().__init__(sends=[], sign_ins=sign_ins, authorized=authorized)
        self.qr = FakeQr(waits)

    async def qr_login(self):
        self.calls.append(("qr_login",))
        return self.qr


async def run_qr(client, console, attempts=3):
    shown = []
    person = await run_qr_flow(
        client, show_qr=shown.append, ask_secret=console.ask_secret, say=console.say, attempts=attempts
    )
    return person, shown


async def test_qr_scanned_first_time():
    client = FakeQrClient(waits=[None])

    person, shown = await run_qr(client, Console([]))

    assert person.id == 42
    assert shown == ["tg://login?token=t0"]


async def test_qr_expired_is_recreated():
    client = FakeQrClient(waits=[TimeoutError(), None])

    _, shown = await run_qr(client, Console([]))

    assert shown == ["tg://login?token=t0", "tg://login?token=t1"]


async def test_qr_with_two_factor_password():
    client = FakeQrClient(waits=[errors.SessionPasswordNeededError(request=None)])

    await run_qr(client, Console([], secrets=["pw"]))

    assert client.calls[-1] == ("sign_in", {"password": "pw"})


async def test_qr_never_scanned_raises_login_error():
    client = FakeQrClient(waits=[TimeoutError()] * 3)

    with pytest.raises(LoginError, match="not scanned"):
        await run_qr(client, Console([]), attempts=3)


async def test_qr_already_authorized_skips_qr():
    client = FakeQrClient(waits=[], authorized=True)

    person, shown = await run_qr(client, Console([]))

    assert person.id == 42
    assert shown == [] and client.calls == []


async def test_login_client_identifies_as_tg_bridge(monkeypatch, tmp_path):
    from tests.test_config import CREDS
    from tg_bridge.config import load_settings
    from tg_bridge.telegram import login

    seen = {}

    class Stop(Exception):
        pass

    def fake_client(*args, **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(login, "TelegramClient", fake_client)
    settings = load_settings(env=CREDS, config_file=tmp_path / "c")

    with pytest.raises(Stop):
        await login.interactive_login(settings, "qr")

    assert seen["device_model"] == "tg-bridge"
    assert seen["receive_updates"] is True
