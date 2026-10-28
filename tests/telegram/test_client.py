from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl import types as tl

from tests.fakes import make_chat, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import MessageQuery
from tg_bridge.telegram.client import ReadOnlyTelegram
from tests.telegram.test_mapping import forum_topic, tl_channel, tl_msg, tl_user, user


class FakeTelethonClient:
    def __init__(self):
        self.args, self.kwargs = (), {}
        self._self_id = 42  # read by Telethon's Message._finish_init, together with the entity cache
        self._mb_entity_cache = SimpleNamespace(get=lambda *_: None)
        self.calls: list[str] = []
        self.authorized = True
        self.me = user(id=42, first_name="Anna", username="anna_k")
        self.get_me_error: Exception | None = None
        self.dialogs: list = []
        self.iter_dialogs_kwargs: dict = {}
        self.iter_dialogs_error: Exception | None = None
        self.requests: list = []  # every request object passed to client(...)
        self.results: dict[str, object] = {}  # request type name -> canned result
        self.result_queues: dict[str, list] = {}  # request type name -> results handed out one per call
        self.request_errors: dict[str, Exception] = {}
        self.input_entity_ids: list[int] = []
        self.unknown_entities = 0
        self.messages: list = []  # tl messages iter_messages yields, in the order given
        self.iter_messages_calls: list[tuple] = []  # (entity, kwargs)
        self.iter_messages_fail_after: tuple[int, Exception] | None = None

    def __getattr__(self, name):  # only reached for attributes the fake does not define
        if name == "send_read_acknowledge":
            raise AssertionError("read receipts must never be sent")
        raise AttributeError(name)

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

    async def iter_dialogs(self, *args, **kwargs):
        self.iter_dialogs_kwargs = kwargs
        if self.iter_dialogs_error:
            raise self.iter_dialogs_error
        for d in self.dialogs:
            yield d

    async def iter_messages(self, entity, *args, **kwargs):
        self.iter_messages_calls.append((entity, kwargs))
        for i, m in enumerate(self.messages):
            if self.iter_messages_fail_after and i == self.iter_messages_fail_after[0]:
                raise self.iter_messages_fail_after[1]
            yield m

    async def get_input_entity(self, peer):
        self.input_entity_ids.append(peer)
        if self.unknown_entities:
            # Telethon raises ValueError when the peer isn't in its session entity cache yet.
            self.unknown_entities -= 1
            raise ValueError("Could not find the input entity")
        return f"input:{peer}"

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(request)
        if name in self.request_errors:
            raise self.request_errors[name]
        if self.result_queues.get(name):
            return self.result_queues[name].pop(0)
        return self.results[name]

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

    assert public == {"connect", "get_me", "disconnect", "list_dialogs", "get_chat_details", "get_messages", "search"}


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


async def test_client_identifies_as_tg_bridge(wrapper):
    from importlib.metadata import version

    tg, fake = wrapper
    await tg.connect()

    assert fake.kwargs["device_model"] == "tg-bridge"
    assert fake.kwargs["app_version"] == version("tg-bridge")
    # OS name only: no kernel version or CPU architecture leaks into Settings → Devices.
    assert fake.kwargs["system_version"] in {"macOS", "Linux", "Windows"}


# --- dialogs and chat details ------------------------------------------------

ALLOWED_REQUESTS = {
    "GetFullUserRequest", "GetFullChatRequest", "GetFullChannelRequest", "GetForumTopicsRequest", "GetForumTopicsByIDRequest",
}


def dlg(entity, id, **kw):
    return SimpleNamespace(id=id, entity=entity, date=datetime(2026, 10, 25, 12, 3, tzinfo=UTC), folder_id=kw.get("folder_id"))


async def test_list_dialogs_includes_archived_and_ignores_migrated(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.dialogs = [dlg(tl_user(), 1), dlg(tl_user(id=2, first_name="Bo"), 2, folder_id=1)]

    chats = await tg.list_dialogs()

    assert [(c.id, c.is_archived) for c in chats] == [(1, False), (2, True)]
    assert fake.iter_dialogs_kwargs == {"ignore_migrated": True}


async def test_list_dialogs_flood_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.iter_dialogs_error = FloodWaitError(request=None, capture=90)

    with pytest.raises(RateLimitedError) as exc:
        await tg.list_dialogs()

    assert exc.value.seconds == 90


def full(**kw):
    return SimpleNamespace(full_chat=SimpleNamespace(**kw))


async def test_chat_details_channel_uses_full_channel(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about="Hello", participants_count=12)
    chat = make_chat(id=-1001234567890)

    info = await tg.get_chat_details(chat)

    assert (info.chat, info.description, info.members_count, info.topics) == (chat, "Hello", 12, ())
    assert fake.input_entity_ids == [-1001234567890]
    assert [type(r).__name__ for r in fake.requests] == ["GetFullChannelRequest"]


async def test_chat_details_cold_entity_cache_lists_dialogs_then_retries(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.unknown_entities = 1
    fake.results["GetFullChannelRequest"] = full(about="Hello", participants_count=12)

    info = await tg.get_chat_details(make_chat(id=-1001234567890))

    assert info.description == "Hello"
    assert fake.input_entity_ids == [-1001234567890, -1001234567890]
    assert fake.iter_dialogs_kwargs == {"ignore_migrated": True}


async def test_chat_details_basic_group_uses_positive_chat_id(wrapper):
    tg, fake = wrapper
    await tg.connect()
    group = tl.Chat(id=555, title="Old", photo=tl.ChatPhotoEmpty(), participants_count=7, date=None, version=1)
    fake.results["GetFullChatRequest"] = SimpleNamespace(full_chat=SimpleNamespace(about="About"), chats=[group])

    info = await tg.get_chat_details(make_chat(id=-555, kind="group"))

    assert fake.requests[0].chat_id == 555
    assert (info.description, info.members_count) == ("About", 7)


async def test_chat_details_user_bio(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullUserRequest"] = SimpleNamespace(full_user=SimpleNamespace(about="Bio"))

    info = await tg.get_chat_details(make_user_chat())

    assert (info.description, info.members_count, info.topics) == ("Bio", None, ())


async def test_chat_details_forum_lists_topics_and_total(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about=None, participants_count=3)
    fake.results["GetForumTopicsRequest"] = SimpleNamespace(
        count=41,
        topics=[forum_topic(id=1, title="General"), tl.ForumTopicDeleted(id=2), forum_topic(id=23, title="Trips", closed=True)],
    )

    info = await tg.get_chat_details(make_chat(is_forum=True))

    assert [(t.id, t.title, t.is_closed) for t in info.topics] == [(1, "General", False), (23, "Trips", True)]
    assert info.topics_total == 41
    req = fake.requests[1]
    assert (req.offset_date, req.offset_id, req.offset_topic, req.limit) == (None, 0, 0, 100)


async def test_chat_details_forum_fetches_general_when_page_omits_it(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about=None, participants_count=3)
    fake.results["GetForumTopicsRequest"] = SimpleNamespace(count=2, topics=[forum_topic(id=23, title="Trips")])
    fake.results["GetForumTopicsByIDRequest"] = SimpleNamespace(count=1, topics=[forum_topic(id=1, title="Загальне")])

    info = await tg.get_chat_details(make_chat(is_forum=True))

    assert [(t.id, t.title) for t in info.topics] == [(1, "Загальне"), (23, "Trips")]
    assert fake.requests[-1].topics == [1]
    assert info.topics_total is None


async def test_chat_details_forum_lists_general_even_when_lookup_returns_nothing(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about=None, participants_count=3)
    fake.results["GetForumTopicsRequest"] = SimpleNamespace(count=1, topics=[forum_topic(id=23, title="Trips")])
    fake.results["GetForumTopicsByIDRequest"] = SimpleNamespace(count=0, topics=[])

    info = await tg.get_chat_details(make_chat(is_forum=True))

    assert [(t.id, t.title) for t in info.topics] == [(1, "General"), (23, "Trips")]


async def test_chat_details_forum_total_none_when_all_fit(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about=None, participants_count=3)
    fake.results["GetForumTopicsRequest"] = SimpleNamespace(count=1, topics=[forum_topic(id=1, title="General")])

    info = await tg.get_chat_details(make_chat(is_forum=True))

    assert info.topics_total is None


async def test_chat_details_rpc_error_degrades(wrapper, caplog):
    tg, fake = wrapper
    await tg.connect()
    fake.request_errors["GetFullChannelRequest"] = ChannelPrivateError(request=None)
    chat = make_chat(id=-1001234567890, title="Secret Title", is_forum=True)

    with caplog.at_level("DEBUG"):
        info = await tg.get_chat_details(chat)

    assert (info.chat, info.description, info.members_count, info.topics) == (chat, None, None, ())
    assert "ChannelPrivateError" in caplog.text
    assert "Secret Title" not in caplog.text


async def test_chat_details_flood_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.request_errors["GetFullChannelRequest"] = FloodWaitError(request=None, capture=61)

    with pytest.raises(RateLimitedError):
        await tg.get_chat_details(make_chat())


async def test_only_read_requests_used(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetFullChannelRequest"] = full(about=None, participants_count=3)
    fake.results["GetFullChatRequest"] = SimpleNamespace(full_chat=SimpleNamespace(about=None), chats=[])
    fake.results["GetFullUserRequest"] = SimpleNamespace(full_user=SimpleNamespace(about=None))
    fake.results["GetForumTopicsRequest"] = SimpleNamespace(count=0, topics=[])
    fake.results["GetForumTopicsByIDRequest"] = SimpleNamespace(count=0, topics=[])
    for chat in (make_chat(is_forum=True), make_chat(id=-5, kind="group"), make_user_chat()):
        await tg.get_chat_details(chat)

    assert {type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS


# --- messages ------------------------------------------------------------------


def at(day, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute)  # naive: how Telethon hands out UTC


def aware(day, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def query(**overrides) -> MessageQuery:
    fields = dict(direction="forward", start=None, end=None, boundary_id=None, topic_id=None, from_me=False, limit=100)
    fields.update(overrides)
    return MessageQuery(**fields)


async def read(wrapper, messages, q):
    tg, fake = wrapper
    fake.messages = messages
    await tg.connect()
    return await tg.get_messages(make_chat(), q)


async def test_client_built_with_flood_threshold_30(wrapper):
    tg, fake = wrapper
    await tg.connect()

    assert fake.kwargs["flood_sleep_threshold"] == 30


async def test_forward_query_kwargs(wrapper):
    await read(wrapper, [], query(start=aware(25)))
    await read(wrapper, [], query(start=aware(25), boundary_id=50))

    (_, first), (_, second) = wrapper[1].iter_messages_calls
    assert (first["reverse"], first["offset_date"], first["offset_id"], first["limit"]) == (True, aware(25), 0, None)
    assert second["offset_id"] == 50


async def test_backward_query_kwargs(wrapper):
    await read(wrapper, [], query(direction="backward", end=aware(25)))
    await read(wrapper, [], query(direction="backward", end=aware(25), boundary_id=50))

    (_, first), (_, second) = wrapper[1].iter_messages_calls
    assert (first["reverse"], first["offset_date"], first["offset_id"]) == (False, aware(25), 0)
    assert second["offset_id"] == 50


async def test_from_me_uses_from_user_me(wrapper):
    await read(wrapper, [], query(from_me=True))

    assert wrapper[1].iter_messages_calls[0][1]["from_user"] == "me"


async def test_topic_uses_reply_to_and_filters_out_client_side(wrapper):
    msgs = [tl_msg(1, "mine", out=True), tl_msg(2, "theirs"), tl_msg(3, "mine too", out=True)]

    result = await read(wrapper, msgs, query(topic_id=12, from_me=True))

    kwargs = wrapper[1].iter_messages_calls[0][1]
    assert kwargs["reply_to"] == 12 and "from_user" not in kwargs
    assert [m.id for m in result] == [1, 3]


def in_topic(topic_id, reply_to_msg_id=None):
    return tl.MessageReplyHeader(
        forum_topic=True,
        reply_to_msg_id=reply_to_msg_id or topic_id,
        reply_to_top_id=topic_id if reply_to_msg_id else None,
    )


def topic_created(id, title):
    return tl.MessageService(
        id=id, peer_id=tl.PeerChannel(10), date=at(25), action=tl.MessageActionTopicCreate(title=title, icon_color=0)
    )


async def test_general_topic_reads_history_and_keeps_messages_outside_other_topics(wrapper):
    msgs = [
        tl_msg(1, "old, pre-forum"),
        topic_created(2, "Trips"),
        tl_msg(3, "in Trips", reply_to=in_topic(2)),
        tl_msg(4, "General"),
        tl_msg(5, "reply in Trips", reply_to=in_topic(2, reply_to_msg_id=3)),
        tl_msg(6, "reply in General", reply_to=tl.MessageReplyHeader(reply_to_msg_id=4)),
    ]

    result = await read(wrapper, msgs, query(topic_id=1))

    kwargs = wrapper[1].iter_messages_calls[0][1]
    assert "reply_to" not in kwargs  # GetReplies(msg_id=1) returns nothing: General is no reply thread
    assert [m.id for m in result] == [1, 4, 6]


async def test_general_topic_limit_counts_only_general_messages(wrapper):
    msgs = [tl_msg(1), tl_msg(2, reply_to=in_topic(9)), tl_msg(3), tl_msg(4)]

    result = await read(wrapper, msgs, query(topic_id=1, limit=2))

    assert [m.id for m in result] == [1, 3]


async def test_general_topic_pages_from_boundary_and_filters_from_me_server_side(wrapper):
    result = await read(wrapper, [tl_msg(51, out=True)], query(topic_id=1, boundary_id=50, from_me=True))

    kwargs = wrapper[1].iter_messages_calls[0][1]
    assert (kwargs["offset_id"], kwargs["from_user"]) == (50, "me")
    assert [m.id for m in result] == [51]


async def test_forward_stops_at_end(wrapper):
    msgs = [tl_msg(1, date=at(25)), tl_msg(2, date=at(26)), tl_msg(3, date=at(27))]

    result = await read(wrapper, msgs, query(start=aware(25), end=aware(27)))

    assert [m.id for m in result] == [1, 2]


async def test_backward_stops_before_start(wrapper):
    msgs = [tl_msg(3, date=at(27)), tl_msg(2, date=at(26)), tl_msg(1, date=at(25))]

    result = await read(wrapper, msgs, query(direction="backward", start=aware(26)))

    assert [m.id for m in result] == [3, 2]


async def test_skips_messages_before_start_in_forward(wrapper):
    msgs = [tl_msg(1, date=at(24)), tl_msg(2, date=at(26))]

    result = await read(wrapper, msgs, query(start=aware(25)))

    assert [m.id for m in result] == [2]


async def test_stops_at_limit(wrapper):
    result = await read(wrapper, [tl_msg(i) for i in range(1, 10)], query(limit=3))

    assert [m.id for m in result] == [1, 2, 3]


async def test_message_empty_skipped(wrapper):
    msgs = [tl_msg(1), tl.MessageEmpty(id=2, peer_id=tl.PeerChannel(10)), tl_msg(3)]

    assert [m.id for m in await read(wrapper, msgs, query())] == [1, 3]


async def test_flood_mid_iteration_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    fake.iter_messages_fail_after = (2, FloodWaitError(request=None, capture=45))

    with pytest.raises(RateLimitedError) as exc:
        await read(wrapper, [tl_msg(1), tl_msg(2), tl_msg(3)], query())

    assert exc.value.seconds == 45


async def test_get_messages_cold_entity_cache(wrapper):
    tg, fake = wrapper
    fake.unknown_entities = 1

    result = await read(wrapper, [tl_msg(1)], query())

    assert [m.id for m in result] == [1]
    assert fake.iter_messages_calls[0][0] == "input:-1001234567890"


async def test_no_read_acknowledge(wrapper):
    await read(wrapper, [tl_msg(1)], query())  # the fake raises AssertionError if send_read_acknowledge is touched

    assert "send_read_acknowledge" not in wrapper[1].calls
