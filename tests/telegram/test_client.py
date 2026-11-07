from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.helpers import TotalList
from telethon.errors import ChannelPrivateError, FloodWaitError
from telethon.tl import types as tl

from tests.fakes import make_chat, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.source import HistoryStats, MessageQuery
from tg_bridge.telegram.client import ReadOnlyTelegram
from tests.telegram.test_mapping import forum_topic, tl_channel, tl_filter, tl_msg, tl_user, user


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
        self.messages_by_id: dict[int, object] = {}  # raw messages get_messages(ids=...) can find
        self.get_messages_calls: list[tuple] = []  # (entity, ids)
        self.get_messages_error: Exception | None = None
        self.history_total = 0  # TotalList.total of get_messages(entity, limit=...)
        self.history_page: list = []
        self.history_calls: list[tuple] = []  # (entity, kwargs)
        self.history_error: Exception | None = None
        self.download_chunks: list = []
        self.download_fail_after: tuple[int, Exception] | None = None
        self.iter_download_calls: list[tuple] = []  # (file, offset, file_size, dc_id, request_size)
        self.profile_photo: bytes | None = None
        self.profile_photo_error: Exception | None = None
        self.profile_photo_calls: list[tuple] = []  # (entity, kwargs)

    def __getattr__(self, name):  # only reached for attributes the fake does not define
        if name == "send_read_acknowledge":
            raise AssertionError("read receipts must never be sent")
        raise AttributeError(name)

    async def connect(self):
        self.calls.append("connect")

    async def is_user_authorized(self):
        self.calls.append("is_user_authorized")
        return self.authorized

    async def get_me(self, input_peer=False):
        self.calls.append("get_me")
        if self.get_me_error:
            raise self.get_me_error
        return tl.InputPeerUser(user_id=self.me.id, access_hash=1) if input_peer else self.me

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

    async def get_messages(self, entity, ids=None, **kwargs):
        if ids is None:
            self.history_calls.append((entity, kwargs))
            if self.history_error:
                raise self.history_error
            page = TotalList(self.history_page)
            page.total = self.history_total
            return page
        self.get_messages_calls.append((entity, ids))
        if self.get_messages_error:
            raise self.get_messages_error
        return [self.messages_by_id.get(i) for i in ids]

    async def iter_download(self, file, *, offset=0, file_size=None, dc_id=None, request_size=None, **kw):
        self.iter_download_calls.append((file, offset, file_size, dc_id, request_size))
        for i, chunk in enumerate(self.download_chunks):
            if self.download_fail_after and i == self.download_fail_after[0]:
                raise self.download_fail_after[1]
            yield chunk

    async def download_profile_photo(self, entity, file=None, **kwargs):
        self.profile_photo_calls.append((entity, {"file": file, **kwargs}))
        if self.profile_photo_error:
            raise self.profile_photo_error
        return self.profile_photo

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

    assert public == {
        "connect", "get_me", "disconnect", "list_dialogs", "list_folders", "get_chat_details", "get_messages", "search",
        "get_media_files", "read_media", "get_history_stats", "get_contact_cards", "get_post", "get_comments", "download_chat_photo",
    }


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
    "GetMessagesRequest", "GetFileRequest",  # download_media: messages.GetMessages / channels.GetMessages, upload.GetFile
    "GetDialogFiltersRequest", "GetNotifySettingsRequest",  # folders: messages.GetDialogFilters, account.GetNotifySettings
}


def dlg(entity, id, **kw):
    return SimpleNamespace(
        id=id,
        entity=entity,
        date=datetime(2026, 10, 25, 12, 3, tzinfo=UTC),
        folder_id=kw.get("folder_id"),
        dialog=kw.get("dialog"),
    )


def notify(mute_until=None):
    return tl.PeerNotifySettings(mute_until=mute_until)


async def test_list_dialogs_includes_archived_and_ignores_migrated(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.dialogs = [dlg(tl_user(), 1), dlg(tl_user(id=2, first_name="Bo"), 2, folder_id=1)]
    fake.results["GetNotifySettingsRequest"] = notify()

    chats = await tg.list_dialogs()

    assert [(c.id, c.is_archived) for c in chats] == [(1, False), (2, True)]
    assert fake.iter_dialogs_kwargs == {"ignore_migrated": True}


async def test_list_dialogs_marks_chat_muted_by_account_default(wrapper):
    tg, fake = wrapper
    await tg.connect()
    own_unmuted = SimpleNamespace(
        notify_settings=notify(None), unread_count=0, unread_mark=False, unread_mentions_count=0, unread_reactions_count=0
    )
    fake.dialogs = [dlg(tl_user(), 1, dialog=own_unmuted), dlg(tl_channel(broadcast=True), -1000000000010, dialog=own_unmuted)]
    far = datetime(2037, 1, 1, tzinfo=UTC)
    # Asked in the order users, chats, broadcasts.
    fake.result_queues["GetNotifySettingsRequest"] = [notify(None), notify(None), notify(far)]

    chats = await tg.list_dialogs()

    assert [c.is_muted for c in chats] == [False, True]
    asked = [type(r.peer) for r in fake.requests if type(r).__name__ == "GetNotifySettingsRequest"]
    assert asked == [tl.InputNotifyUsers, tl.InputNotifyChats, tl.InputNotifyBroadcasts]


async def test_list_dialogs_survives_failing_notify_defaults(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.dialogs = [dlg(tl_user(), 1)]
    fake.request_errors["GetNotifySettingsRequest"] = ChannelPrivateError(request=None)

    chats = await tg.list_dialogs()

    assert [(c.id, c.is_muted) for c in chats] == [(1, False)]


async def test_list_folders_in_response_order_without_default(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetDialogFiltersRequest"] = tl.messages.DialogFilters(
        filters=[tl.DialogFilterDefault(), tl_filter(id=3, title="B"), tl_filter(id=2, title="A")]
    )

    folders = await tg.list_folders()

    assert [(f.id, f.title) for f in folders] == [(3, "B"), (2, "A")]


async def test_list_folders_accepts_bare_list_and_resolves_self(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.results["GetDialogFiltersRequest"] = [tl_filter(include_peers=[tl.InputPeerSelf()])]

    folders = await tg.list_folders()

    assert folders[0].include_ids == (42,)


async def test_folder_reads_use_only_allowed_requests(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.dialogs = [dlg(tl_user(), 1)]
    fake.results["GetNotifySettingsRequest"] = notify()
    fake.results["GetDialogFiltersRequest"] = tl.messages.DialogFilters(filters=[tl_filter()])

    await tg.list_dialogs()
    await tg.list_folders()

    assert {type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS


async def test_list_folders_flood_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.request_errors["GetDialogFiltersRequest"] = FloodWaitError(request=None, capture=60)

    with pytest.raises(RateLimitedError):
        await tg.list_folders()


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


async def test_no_page_sleeps_up_to_telethons_own_threshold(wrapper):
    # With limit=None Telethon sleeps 1 s between history pages; a folder read would spend its time sleeping.
    await read(wrapper, [], query(limit=3000))
    await read(wrapper, [], query(limit=1))
    await read(wrapper, [], query(limit=3001))

    small, tiny, big = (kw for _, kw in wrapper[1].iter_messages_calls)
    assert small["wait_time"] == 0 and tiny["wait_time"] == 0
    assert "wait_time" not in big


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


async def test_locked_session_becomes_session_busy(wrapper):
    # Another tg-bridge process (e.g. Claude Code next to LM Studio) holds the SQLite session's write lock.
    import sqlite3

    from tg_bridge.errors import SessionBusyError

    tg, fake = wrapper
    await tg.connect()
    fake.get_me_error = sqlite3.OperationalError("database is locked")

    with pytest.raises(SessionBusyError) as exc:
        await tg.get_me()

    assert "another tg-bridge process" in str(exc.value)


async def test_other_sqlite_errors_are_not_hidden(wrapper):
    import sqlite3

    tg, fake = wrapper
    await tg.connect()
    fake.get_me_error = sqlite3.OperationalError("disk I/O error")

    with pytest.raises(sqlite3.OperationalError):
        await tg.get_me()


# --- chat photos (admin page) -------------------------------------------------


async def test_download_chat_photo_returns_small_photo_bytes_in_memory(wrapper):
    tg, fake = wrapper
    await tg.connect()
    fake.profile_photo = b"\xff\xd8jpeg"

    assert await tg.download_chat_photo(-1001) == b"\xff\xd8jpeg"

    (entity, kwargs), = fake.profile_photo_calls
    assert entity == -1001
    assert kwargs["file"] is bytes and kwargs["download_big"] is False
    assert {type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS


async def test_download_chat_photo_none_without_photo(wrapper):
    tg, fake = wrapper
    await tg.connect()

    assert await tg.download_chat_photo(-1001) is None


async def test_download_chat_photo_none_on_rpc_error(wrapper, caplog):
    tg, fake = wrapper
    await tg.connect()
    fake.profile_photo_error = ChannelPrivateError(request=None)

    assert await tg.download_chat_photo(-1001) is None
    assert "ChannelPrivate" in caplog.text


# --- history stats and contact cards -------------------------------------------

async def test_get_history_stats_uses_total_and_oldest_message(wrapper):
    tg, fake = wrapper
    fake.history_total = 1234
    fake.messages_by_id = {}
    fake.history_page = [tl_msg(id=1, text="old", date=datetime(2019, 3, 1))]
    await tg.connect()

    stats = await tg.get_history_stats(make_user_chat(make_person(id=777)))

    assert stats == HistoryStats(1234, datetime(2019, 3, 1, tzinfo=UTC))
    entity, kwargs = fake.history_calls[0]
    assert entity == "input:777" and kwargs["limit"] == 1 and kwargs["reverse"] is True
    assert {type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS


async def test_get_history_stats_empty_history(wrapper):
    tg, fake = wrapper
    fake.history_total = 0
    fake.history_page = []
    await tg.connect()

    assert await tg.get_history_stats(make_user_chat(make_person(id=777))) == HistoryStats(0, None)


async def test_get_contact_cards_filters_contacts(wrapper):
    tg, fake = wrapper
    card = tl_msg(id=5, text="", media=tl.MessageMediaContact(phone_number="1", first_name="Olena", last_name="P", vcard="", user_id=777))
    fake.messages = [card, tl_msg(id=4, text="not a card")]
    await tg.connect()

    cards = await tg.get_contact_cards(make_user_chat(make_person(id=777)))

    entity, kwargs = fake.iter_messages_calls[0]
    assert isinstance(kwargs["filter"], tl.InputMessagesFilterContacts) and kwargs["limit"] == 20
    assert [c.message_id for c in cards] == [5]
    assert {type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS


async def test_history_stats_flood_becomes_rate_limited(wrapper):
    tg, fake = wrapper
    fake.history_error = FloodWaitError(request=None, capture=60)
    await tg.connect()

    with pytest.raises(RateLimitedError):
        await tg.get_history_stats(make_user_chat(make_person(id=777)))
