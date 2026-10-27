import pytest
from telethon.errors import FloodWaitError

from tests.fakes import make_person
from tests.test_config import CREDS
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.telegram.client import ReadOnlyTelegram
from tests.telegram.test_mapping import user


class FakeTelethonClient:
    def __init__(self):
        self.args, self.kwargs = (), {}
        self.calls: list[str] = []
        self.authorized = True
        self.me = user(id=42, first_name="Anna", username="anna_k")
        self.get_me_error: Exception | None = None

    async def connect(self):
        self.calls.append("connect")

    async def is_user_authorized(self):
        self.calls.append("is_user_authorized")
        return self.authorized

    async def get_me(self):
        self.calls.append("get_me")
        if self.get_me_error:
            raise self.get_me_error
        return self.me

    async def disconnect(self):
        self.calls.append("disconnect")

    async def start(self, *args, **kwargs):  # must never be used by the server: it prompts on stdin
        self.calls.append("start")


class Factory:
    """Records the client it builds so tests can configure and inspect it."""

    def __init__(self):
        self.client = FakeTelethonClient()
        self.built = 0

    def __call__(self, *args, **kwargs):
        self.built += 1
        self.client.args, self.client.kwargs = args, kwargs
        return self.client


@pytest.fixture
def settings(tmp_path):
    session = tmp_path / "t.session"
    session.touch()
    return load_settings(env={**CREDS, "TG_BRIDGE_SESSION": str(session)}, config_file=tmp_path / "c")


@pytest.fixture
def wrapper(settings):
    factory = Factory()
    return ReadOnlyTelegram(settings, client_factory=factory), factory.client


async def test_builds_client_without_updates(wrapper, settings):
    tg, fake = wrapper
    await tg.connect()

    assert fake.args == (str(settings.session_path), 123, "h-secret")
    assert fake.kwargs["receive_updates"] is False


async def test_connect_unauthorized_raises_not_logged_in(wrapper):
    tg, fake = wrapper
    fake.authorized = False

    with pytest.raises(NotLoggedInError, match="tg_bridge.login"):
        await tg.connect()

    assert "start" not in fake.calls
    assert "disconnect" in fake.calls


async def test_get_me_maps_person(wrapper):
    tg, _ = wrapper
    await tg.connect()

    assert await tg.get_me() == make_person(phone="48123")


async def test_missing_session_file_raises_without_creating_it(tmp_path):
    session = tmp_path / "nodir" / "x.session"
    settings = load_settings(env={**CREDS, "TG_BRIDGE_SESSION": str(session)}, config_file=tmp_path / "c")
    factory = Factory()
    tg = ReadOnlyTelegram(settings, client_factory=factory)

    with pytest.raises(NotLoggedInError, match="tg_bridge.login"):
        await tg.connect()

    assert factory.built == 0
    assert not session.parent.exists()


def test_constructing_does_not_touch_telegram(settings):
    factory = Factory()

    ReadOnlyTelegram(settings, client_factory=factory)

    assert factory.built == 0


async def test_flood_wait_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.get_me_error = FloodWaitError(request=None, capture=42)

    with pytest.raises(RateLimitedError) as exc:
        await tg.get_me()

    assert exc.value.seconds == 42


def test_public_surface_is_read_only():
    public = {n for n in dir(ReadOnlyTelegram) if not n.startswith("_")}

    assert public == {"connect", "get_me", "disconnect"}


async def test_unauthorized_client_is_dropped_so_next_connect_rereads_session(settings):
    # Telethon saves its in-memory session on disconnect; a stale client must not outlive a fresh login.
    factory = Factory()
    tg = ReadOnlyTelegram(settings, client_factory=factory)
    factory.client.authorized = False
    with pytest.raises(NotLoggedInError):
        await tg.connect()

    factory.client = FakeTelethonClient()
    await tg.connect()

    assert factory.built == 2
