"""Read-only Telethon wrapper: the only door to the account.

Exposes read methods only. Never calls client.start() (it prompts on stdin, which
the MCP transport owns), never sends read receipts or status updates.
"""

import itertools
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from telethon import TelegramClient, utils
from telethon.errors import FloodError, RPCError
from telethon.tl import functions
from telethon.tl import types as tl

from tg_bridge.config import Settings
from tg_bridge.errors import (
    MessageNotFoundError,
    NotLoggedInError,
    RateLimitedError,
    SessionBusyError,
    TelegramRefusedError,
)
from tg_bridge.source import (
    MEDIA_CHUNK,
    Chat,
    ChatInfo,
    Folder,
    MediaFile,
    Message,
    MessageQuery,
    Person,
    Post,
    SearchOffset,
    SearchQuery,
    Topic,
)
from tg_bridge.telegram.identity import client_identity
from tg_bridge.telegram.mapping import (
    chat_from_dialog,
    comment_from_tl,
    folder_from_tl,
    largest_photo_size,
    media_file_from_tl,
    message_from_tl,
    person_from_user,
    post_from_tl,
    topic_from_forum_topic,
)

log = logging.getLogger(__name__)

TOPICS_LIMIT = 100
# Every forum has it. Unlike other topics it is no reply thread: its messages carry no topic header.
GENERAL_TOPIC_ID = 1
# Albums hold at most 10 items with consecutive ids, so the neighbours of any member fit in this window.
ALBUM_REACH = 9
# Telethon sleeps through FloodWaits up to this long itself; longer ones surface as RateLimitedError.
FLOOD_SLEEP_THRESHOLD = 30

NOT_LOGGED_IN = "Not logged in to Telegram. Run: uv run python -m tg_bridge.login"


class ReadOnlyTelegram:
    def __init__(self, settings: Settings, client_factory: Callable[..., Any] = TelegramClient):
        self._settings = settings
        self._client_factory = client_factory
        self._client: Any = None
        # Raw messages of the last get_media_files call, so read_media needs no second fetch.
        self._media_raw: dict[tuple[int, int], Any] = {}

    async def connect(self) -> None:
        session = self._settings.session_path
        # Telethon opens (and would create) the SQLite session in its constructor,
        # so check first: a missing session means "run login", not "make an empty one".
        if not session.is_file():
            raise NotLoggedInError(NOT_LOGGED_IN)
        if self._client is None:
            # receive_updates=False: we never need the update stream; it keeps the session passive.
            self._client = self._client_factory(
                str(session),
                self._settings.api_id,
                self._settings.api_hash,
                receive_updates=False,
                flood_sleep_threshold=FLOOD_SLEEP_THRESHOLD,
                **client_identity(),
            )
        await self._call(self._client.connect)
        if not await self._call(self._client.is_user_authorized):
            await self._client.disconnect()
            # Drop it: Telethon saves its session on disconnect, and a stale client
            # reused after the user logs in would overwrite the fresh session file.
            self._client = None
            raise NotLoggedInError(NOT_LOGGED_IN)

    async def get_me(self) -> Person:
        return person_from_user(await self._call(self._connected().get_me))

    async def list_dialogs(self) -> list[Chat]:
        client = self._connected()
        started = time.monotonic()
        raw_dialogs = await self._raw_dialogs(client)
        defaults = await self._mute_defaults(client)
        now = datetime.now(UTC)
        chats = [chat_from_dialog(d, defaults, now) for d in raw_dialogs]
        log.info("fetched %d dialogs in %.2f s", len(chats), time.monotonic() - started)
        return chats

    async def _raw_dialogs(self, client: Any) -> list[Any]:
        async def collect() -> list[Any]:
            # No archived=/folder= filter: Telegram then returns every folder. Migrated basic groups
            # would otherwise show up next to the supergroup they became.
            return [d async for d in client.iter_dialogs(ignore_migrated=True)]

        return await self._call(collect)

    async def _mute_defaults(self, client) -> dict[str, datetime | None]:
        """Account-wide mute_until per chat category; chats without an own setting inherit it."""
        defaults: dict[str, datetime | None] = {}
        for category, peer in (
            ("users", tl.InputNotifyUsers()),
            ("chats", tl.InputNotifyChats()),
            ("broadcasts", tl.InputNotifyBroadcasts()),
        ):
            try:
                settings = await self._call(
                    lambda peer=peer: client(functions.account.GetNotifySettingsRequest(peer))
                )
            except (TelegramRefusedError, RateLimitedError) as e:
                # Only the "muted" flag of chats without an own setting depends on it; not worth failing the list.
                log.warning("notify defaults unavailable for %s: %s", category, _error_name(e))
                continue
            defaults[category] = settings.mute_until
        return defaults

    async def list_folders(self) -> list[Folder]:
        """The user's chat folders in app order, without the built-in "All chats"."""
        client = self._connected()
        started = time.monotonic()
        response = await self._call(lambda: client(functions.messages.GetDialogFiltersRequest()))
        raw_filters = getattr(response, "filters", response)  # layer 229: messages.DialogFilters; older: a bare list
        me = await self._call(lambda: client.get_me(input_peer=True))
        self_id = utils.get_peer_id(me)
        folders = [f for f in (folder_from_tl(raw, self_id) for raw in raw_filters) if f is not None]
        log.info("fetched %d folders in %.2f s", len(folders), time.monotonic() - started)
        return folders

    async def get_chat_details(self, chat: Chat) -> ChatInfo:
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        description: str | None = None
        members: int | None = None
        topics: list[Topic] = []
        topics_total: int | None = None
        try:
            if chat.kind in ("user", "bot"):
                full = await self._call(lambda: client(functions.users.GetFullUserRequest(entity)))
                description = full.full_user.about
            elif chat.kind == "group":
                full = await self._call(lambda: client(functions.messages.GetFullChatRequest(-chat.id)))
                description = full.full_chat.about
                members = next((c.participants_count for c in full.chats if c.id == -chat.id), None)
            else:
                full = await self._call(lambda: client(functions.channels.GetFullChannelRequest(entity)))
                description = full.full_chat.about
                members = getattr(full.full_chat, "participants_count", None)
                if chat.is_forum:
                    topics, topics_total = await self._forum_topics(client, entity)
        except TelegramRefusedError as e:
            # E.g. ChannelPrivateError for a chat we were removed from: the dialog data is still worth showing.
            log.warning("chat details unavailable for %d: %s", chat.id, _error_name(e))
            description, members, topics, topics_total = None, None, [], None
        return ChatInfo(
            chat=chat,
            description=description or None,
            members_count=members,
            topics=tuple(topics),
            topics_total=topics_total,
        )

    async def get_messages(self, chat: Chat, query: MessageQuery) -> list[Message]:
        """At most query.limit messages in iteration order (forward = ascending ids, backward = descending)."""
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        forward = query.direction == "forward"
        kwargs: dict[str, Any] = {
            "limit": None,  # stopped by hand below: end date, client-side filters and query.limit
            "reverse": forward,
            "offset_date": query.start if forward else query.end,
            "offset_id": query.boundary_id or 0,  # takes priority over offset_date; forward means "ids greater than"
        }
        _no_page_sleeps(kwargs, query.limit)
        client_side_from_me = False
        general_only = query.topic_id == GENERAL_TOPIC_ID
        if general_only:
            # GetReplies(msg_id=1) returns nothing, so read the whole history and drop other topics' messages below.
            if query.from_me:
                kwargs["from_user"] = "me"
        elif query.topic_id is not None:
            kwargs["reply_to"] = query.topic_id
            # With reply_to Telethon silently drops from_user, so the filter happens below.
            client_side_from_me = query.from_me
        elif query.from_me:
            kwargs["from_user"] = "me"
        started = time.monotonic()

        async def collect() -> list[Message]:
            found: list[Message] = []
            if query.limit <= 0:
                return found
            async for raw in client.iter_messages(entity, **kwargs):
                msg = message_from_tl(raw, chat.id)
                if msg is None:
                    continue
                if general_only and not _in_general(raw, msg):
                    continue
                if forward:
                    if query.end is not None and msg.date >= query.end:
                        break
                    if query.start is not None and msg.date < query.start:
                        continue
                else:
                    if query.start is not None and msg.date < query.start:
                        break
                    if query.end is not None and msg.date >= query.end:
                        continue
                if client_side_from_me and not msg.out:
                    continue
                found.append(msg)
                if len(found) >= query.limit:
                    break
            return found

        messages = await self._call(collect)
        log.info(
            "read %d messages from %d (%s) in %.2f s", len(messages), chat.id, query.direction, time.monotonic() - started
        )
        return messages

    async def get_post(self, chat: Chat, post_id: int) -> Post | None:
        """One channel post with its comment-thread info; None for an unknown id or a service message."""
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        raw = await self._fetch_message(client, entity, post_id)
        return None if raw is None else post_from_tl(raw, chat.id)

    async def get_comments(self, chat: Chat, post: Post, query: MessageQuery) -> list[Message]:
        """At most query.limit comments of `post`, oldest first (messages.GetReplies: no membership needed).
        Comments carry the discussion group's chat id."""
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        post_id = post.message.id
        kwargs: dict[str, Any] = {
            "limit": None,  # stopped by hand: end date and query.limit
            "reverse": True,
            "reply_to": post_id,
            "offset_date": query.start,
            "offset_id": query.boundary_id or 0,  # "ids greater than"; takes priority over offset_date
        }
        _no_page_sleeps(kwargs, query.limit)
        started = time.monotonic()

        async def collect() -> list[Message]:
            found: list[Message] = []
            if query.limit <= 0:
                return found
            async for raw in client.iter_messages(entity, **kwargs):
                msg = comment_from_tl(raw, post.discussion_id)
                if msg is None:
                    continue
                if query.end is not None and msg.date >= query.end:
                    break
                if query.start is not None and msg.date < query.start:
                    continue
                found.append(msg)
                if len(found) >= query.limit:
                    break
            return found

        comments = await self._call(collect)
        log.info(
            "read %d comments of post %d in %d in %.2f s", len(comments), post_id, chat.id, time.monotonic() - started
        )
        return comments

    async def search(self, query: SearchQuery, chat: Chat | None) -> list[Message]:
        """At most query.limit text hits, newest first: one chat via messages.Search, all dialogs via
        messages.SearchGlobal, or the user's own messages in all dialogs via messages.Search on inputPeerEmpty."""
        client = self._connected()
        started = time.monotonic()
        if chat is not None:
            entity = await self._input_entity(client, chat.id)
            found = await self._call(lambda: self._search_chat(client, entity, chat, query))
            mode = f"chat {chat.id}"
        elif query.from_me:
            found = await self._call(lambda: self._search_pages(client, query, self._mine_request))
            mode = "global, mine"
        else:
            found = await self._call(lambda: self._search_pages(client, query, self._global_request))
            mode = "global"
        log.info("searched %s: %d hits in %.2f s", mode, len(found), time.monotonic() - started)
        return found

    @staticmethod
    async def _search_chat(client: Any, entity: Any, chat: Chat, query: SearchQuery) -> list[Message]:
        kwargs: dict[str, Any] = {
            "search": query.text,
            "limit": None,  # stopped by hand: start date and query.limit
            "offset_date": query.end,
            "offset_id": query.after.message_id if query.after else 0,
        }
        if query.from_me:
            kwargs["from_user"] = "me"
        _no_page_sleeps(kwargs, query.limit)
        found: list[Message] = []
        if query.limit > 0:
            async for raw in client.iter_messages(entity, **kwargs):
                msg = message_from_tl(raw, chat.id)
                if msg is None:
                    continue
                if query.start is not None and msg.date < query.start:
                    break
                if query.end is not None and msg.date >= query.end:
                    continue
                found.append(msg)
                if len(found) >= query.limit:
                    break
        return found

    async def _global_request(self, client: Any, query: SearchQuery, after: SearchOffset | None, limit: int) -> Any:
        # The boundary hit's date serves as offset_rate, so a search resumes from any shown hit (smoke-tested).
        return functions.messages.SearchGlobalRequest(
            q=query.text,
            filter=tl.InputMessagesFilterEmpty(),
            min_date=query.start,
            max_date=query.end,
            offset_rate=int(after.date.timestamp()) if after else 0,
            offset_peer=await self._input_entity(client, after.chat_id) if after else tl.InputPeerEmpty(),
            offset_id=after.message_id if after else 0,
            limit=limit,
        )

    @staticmethod
    async def _mine_request(client: Any, query: SearchQuery, after: SearchOffset | None, limit: int) -> Any:
        # SearchGlobal has no sender filter; messages.Search on inputPeerEmpty with from_id=self searches the user's
        # own messages in every dialog, filtered by the server (smoke-tested: pages by offset_id + max_date).
        return functions.messages.SearchRequest(
            peer=tl.InputPeerEmpty(),
            q=query.text,
            filter=tl.InputMessagesFilterEmpty(),
            min_date=query.start,
            max_date=after.date if after else query.end,
            offset_id=after.message_id if after else 0,
            add_offset=0,
            limit=limit,
            max_id=0,
            min_id=0,
            hash=0,
            from_id=tl.InputPeerSelf(),
        )

    async def _search_pages(
        self,
        client: Any,
        query: SearchQuery,
        request_for: Callable[[Any, SearchQuery, SearchOffset | None, int], Awaitable[Any]],
    ) -> list[Message]:
        """Raw requests: iter_messages cannot resume a global search from a given hit (nor run the inputPeerEmpty
        search: its post-processing raises), so pages are fetched here, each resuming from the previous last hit."""
        after = query.after
        found: list[Message] = []
        while len(found) < query.limit:
            want = min(100, query.limit - len(found))
            result = await client(await request_for(client, query, after, want))
            entities = {utils.get_peer_id(e): e for e in itertools.chain(result.users, result.chats)}
            last: Message | None = None
            for raw in result.messages:
                if isinstance(raw, tl.MessageEmpty):
                    continue
                raw._finish_init(client, entities, None)  # same call as Telethon's _MessagesIter: resolves senders
                msg = message_from_tl(raw, utils.get_peer_id(raw.peer_id))
                if query.start is not None and msg.date < query.start:
                    return found
                last = msg
                if query.end is not None and msg.date >= query.end:
                    continue
                found.append(msg)
                if len(found) >= query.limit:
                    return found
            # A non-slice answer is the end (asking again would repeat it), and so is an empty page. A short page is
            # not: Telegram drops hidden hits from a page (Telethon's iterator makes the same call).
            if last is None or isinstance(result, tl.messages.Messages):
                break
            after = SearchOffset(last.chat_id, last.id, last.date)
        return found

    async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
        """The message's file, or every file of its album ordered by id; [] when it has no media."""
        client = self._connected()
        entity = await self._input_entity(client, chat.id)
        raw = await self._fetch_message(client, entity, message_id)
        if raw is None:
            raise MessageNotFoundError(message_id)
        raws = [raw]
        if getattr(raw, "grouped_id", None) is not None:
            window = list(range(max(1, message_id - ALBUM_REACH), message_id + ALBUM_REACH + 1))
            fetched = await self._call(lambda: client.get_messages(entity, ids=window))
            raws = [m for m in fetched if _is_message(m) and m.grouped_id == raw.grouped_id]
        files = [(m, media_file_from_tl(m)) for m in sorted(raws, key=lambda m: m.id)]
        found = [(m, f) for m, f in files if f is not None]
        self._media_raw = {(chat.id, m.id): m for m, _ in found}
        log.info("media files=%d chat=%s", len(found), chat.id)
        return [f for _, f in found]

    async def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
        """The file's bytes from `offset` (a multiple of MEDIA_CHUNK) to the end, one Telegram page at a time."""
        client = self._connected()
        raw = self._media_raw.get((chat.id, message_id))
        if raw is None:
            entity = await self._input_entity(client, chat.id)
            raw = await self._fetch_message(client, entity, message_id)
            if raw is None:
                raise MessageNotFoundError(message_id)
        file, size, dc_id = _download_target(raw)
        try:
            async for chunk in client.iter_download(
                file, offset=offset, file_size=size, request_size=MEDIA_CHUNK, dc_id=dc_id
            ):
                yield bytes(chunk)  # Telethon's generic iterator yields memoryviews
        except FloodError as e:
            seconds = int(getattr(e, "seconds", 0) or 0)
            log.warning("telegram flood wait: %d s", seconds)
            raise RateLimitedError(seconds) from e
        except RPCError as e:
            code = _rpc_code(e)
            log.warning("telegram refused a download: %s", code)
            raise TelegramRefusedError(code) from e

    async def download_chat_photo(self, chat_id: int) -> bytes | None:
        """The small profile photo of a chat, in memory; None when it has none or Telegram refuses (a missing icon is
        no reason to fail the admin page)."""
        client = self._connected()
        try:
            return await client.download_profile_photo(chat_id, file=bytes, download_big=False)
        except RPCError as e:
            log.warning("could not download a chat photo: %s", type(e).__name__)
            return None

    async def _fetch_message(self, client: Any, entity: Any, message_id: int) -> Any | None:
        # Passing the entity matters: in private chats and basic groups ids are account-wide, and Telethon then
        # returns None for an id that belongs to another chat.
        found = await self._call(lambda: client.get_messages(entity, ids=[message_id]))
        raw = found[0] if found else None
        return raw if _is_message(raw) else None

    async def _input_entity(self, client: Any, chat_id: int) -> Any:
        try:
            return await self._call(lambda: client.get_input_entity(chat_id))
        except ValueError:
            # The session's entity cache is cold (the dialog list came from our disk cache, not Telegram):
            # listing dialogs fills it, then the lookup works.
            await self._raw_dialogs(client)
            return await self._call(lambda: client.get_input_entity(chat_id))

    async def _forum_topics(self, client: Any, entity: Any) -> tuple[list[Topic], int | None]:
        request = functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=TOPICS_LIMIT
        )
        result = await self._call(lambda: client(request))
        topics = [t for t in map(topic_from_forum_topic, result.topics) if t is not None]
        if not any(t.id == GENERAL_TOPIC_ID for t in topics):
            # The page is sorted by activity, so a quiet General can fall outside it; it is readable all the same.
            topics.insert(0, await self._general_topic(client, entity))
        return topics, (result.count if result.count > len(topics) else None)

    async def _general_topic(self, client: Any, entity: Any) -> Topic:
        request = functions.messages.GetForumTopicsByIDRequest(peer=entity, topics=[GENERAL_TOPIC_ID])
        result = await self._call(lambda: client(request))
        found = [t for t in map(topic_from_forum_topic, result.topics) if t is not None and t.id == GENERAL_TOPIC_ID]
        return found[0] if found else Topic(GENERAL_TOPIC_ID, "General", False)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def _connected(self) -> Any:
        if self._client is None:
            raise RuntimeError("ReadOnlyTelegram.connect() must be called first")
        return self._client

    @staticmethod
    async def _call(fn: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await fn()
        except FloodError as e:
            seconds = int(getattr(e, "seconds", 0) or 0)
            log.warning("telegram flood wait: %d s", seconds)
            raise RateLimitedError(seconds) from e
        except RPCError as e:
            # Any other refusal becomes a reason the model can act on (skip, try another chat), not a crash.
            code = _rpc_code(e)
            log.warning("telegram refused: %s", code)
            raise TelegramRefusedError(code) from e
        except sqlite3.OperationalError as e:
            # Telethon keeps the session's write transaction open between saves, so a second long-lived process on
            # the same session file locks the others out.
            if "locked" not in str(e):
                raise
            log.warning("telegram session is locked by another process")
            raise SessionBusyError() from e


# Telethon's generic bases carry the real code in `.message`; its named subclasses only in the class name.
_GENERIC_RPC = {"RPCError", "InvalidDCError", "BadRequestError", "UnauthorizedError", "ForbiddenError", "NotFoundError",
                "AuthKeyError", "FloodError", "ServerError", "TimedOutError"}


def _rpc_code(e: RPCError) -> str:
    """Telegram's error code, e.g. ChannelPrivateError -> CHANNEL_PRIVATE."""
    name = type(e).__name__
    if name in _GENERIC_RPC:
        return str(e.message or "UNKNOWN")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name.removesuffix("Error")).upper()


def _error_name(e: Exception) -> str:
    """The original Telethon class name for logs (the cause of a TelegramRefusedError)."""
    return type(e.__cause__ or e).__name__


# Telethon's own threshold: above it iter_messages does not sleep between history pages.
_NO_WAIT_LIMIT = 3000


def _no_page_sleeps(kwargs: dict[str, Any], limit: int) -> None:
    """We pass limit=None to iter_messages (stopping by hand), and Telethon then sleeps 1 s between every history
    page. A folder read goes through dozens of chats within one tool call, so ask for no sleep when we stop early
    anyway. FloodWait handling is separate and unchanged."""
    if limit <= _NO_WAIT_LIMIT:
        kwargs["wait_time"] = 0


def _in_general(raw: Any, msg: Message) -> bool:
    # A topic's creation message is its thread root and has no topic header, yet belongs to that topic.
    return msg.topic_id is None and not isinstance(getattr(raw, "action", None), tl.MessageActionTopicCreate)


def _is_message(raw: Any) -> bool:
    return raw is not None and not isinstance(raw, tl.MessageEmpty)


def _download_target(raw: Any) -> tuple[Any, int | None, int | None]:
    """(what to pass to iter_download, its exact byte size, dc_id) for a message with a photo or document."""
    media = raw.media
    if isinstance(media, tl.MessageMediaPhoto) and isinstance(media.photo, tl.Photo):
        photo = media.photo
        largest = largest_photo_size(photo)
        if largest is None:
            raise MessageNotFoundError(raw.id)
        location = tl.InputPhotoFileLocation(
            id=photo.id, access_hash=photo.access_hash, file_reference=photo.file_reference, thumb_size=largest[0]
        )
        return location, largest[1], photo.dc_id
    return media.document, media.document.size, None
