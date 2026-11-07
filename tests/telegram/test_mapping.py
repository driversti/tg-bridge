from types import SimpleNamespace

import pytest

from tg_bridge.telegram.mapping import person_from_user


def user(**overrides):
    fields = dict(id=1, first_name="Anna", last_name="K", username="anna_k", phone="48123", deleted=False, bot=False)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_maps_all_fields():
    person = person_from_user(user())

    assert (person.id, person.first_name, person.last_name, person.username, person.phone) == (
        1, "Anna", "K", "anna_k", "48123",
    )
    assert person.is_deleted is False
    assert person.is_bot is False


def test_deleted_user():
    person = person_from_user(user(first_name=None, last_name=None, username=None, deleted=True))

    assert person.is_deleted is True
    assert person.display_name == "Deleted Account"


def test_none_flags_become_false():
    person = person_from_user(user(deleted=None, bot=None))

    assert person.is_deleted is False
    assert person.is_bot is False


# --- chats ---------------------------------------------------------------

from datetime import UTC, datetime, timedelta, timezone  # noqa: E402

from telethon.tl import types as tl  # noqa: E402

from tg_bridge.telegram.mapping import chat_from_dialog, folder_from_tl, topic_from_forum_topic  # noqa: E402


def dialog(entity, id=1, date=datetime(2026, 10, 25, 12, 3, tzinfo=UTC), folder_id=None, raw=None):
    return SimpleNamespace(id=id, entity=entity, date=date, folder_id=folder_id, dialog=raw)


def raw_dialog(mute_until=None, unread_count=0, unread_mark=False, mentions=0, reactions=0):
    return SimpleNamespace(
        notify_settings=SimpleNamespace(mute_until=mute_until),
        unread_count=unread_count,
        unread_mark=unread_mark,
        unread_mentions_count=mentions,
        unread_reactions_count=reactions,
    )


def tl_user(**kw):
    return tl.User(**{"id": 1, "first_name": "Anna", "last_name": "K", "username": "anna_k", **kw})


def tl_channel(**kw):
    return tl.Channel(**{"id": 10, "title": "News", "photo": tl.ChatPhotoEmpty(), "date": None, **kw})


def test_user_dialog():
    chat = chat_from_dialog(dialog(tl_user(), id=1))

    assert (chat.id, chat.kind, chat.title, chat.username) == (1, "user", "Anna K", "anna_k")
    assert chat.peer.id == 1
    assert chat.last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)
    assert chat.is_archived is False and chat.is_forum is False


def test_bot_dialog():
    chat = chat_from_dialog(dialog(tl_user(bot=True, first_name="Helper", last_name=None)))

    assert chat.kind == "bot"
    assert chat.peer.is_bot is True


def test_deleted_user_dialog():
    chat = chat_from_dialog(dialog(tl_user(deleted=True, first_name=None, last_name=None, username=None)))

    assert chat.title == "Deleted Account"
    assert chat.deleted_peer is True
    assert chat.username is None


def test_basic_group_dialog():
    group = tl.Chat(id=5, title="Old", photo=tl.ChatPhotoEmpty(), participants_count=3, date=None, version=1)

    chat = chat_from_dialog(dialog(group, id=-5))

    assert (chat.id, chat.kind, chat.title, chat.peer, chat.username) == (-5, "group", "Old", None, None)


def test_supergroup_dialog():
    chat = chat_from_dialog(dialog(tl_channel(megagroup=True, username="famchat"), id=-1000000000010))

    assert (chat.kind, chat.username, chat.is_forum) == ("supergroup", "famchat", False)


def test_broadcast_channel_dialog():
    assert chat_from_dialog(dialog(tl_channel(broadcast=True))).kind == "channel"


def test_gigagroup_is_supergroup():
    assert chat_from_dialog(dialog(tl_channel(megagroup=True, gigagroup=True))).kind == "supergroup"


def test_forum_supergroup_dialog():
    chat = chat_from_dialog(dialog(tl_channel(megagroup=True, forum=True)))

    assert chat.is_forum is True


def test_chat_forbidden_has_no_username_or_date():
    chat = chat_from_dialog(dialog(tl.ChatForbidden(id=5, title="Kicked"), id=-5, date=None))

    assert (chat.kind, chat.title, chat.username, chat.last_message_at) == ("group", "Kicked", None, None)


def test_channel_forbidden_maps():
    forbidden = tl.ChannelForbidden(id=10, access_hash=1, title="Gone", broadcast=True)

    chat = chat_from_dialog(dialog(forbidden, date=None))

    assert (chat.kind, chat.title, chat.last_message_at) == ("channel", "Gone", None)


def test_archived_dialog():
    assert chat_from_dialog(dialog(tl_user(), folder_id=1)).is_archived is True
    assert chat_from_dialog(dialog(tl_user(), folder_id=0)).is_archived is False


def test_naive_dialog_date_treated_as_utc():
    chat = chat_from_dialog(dialog(tl_user(), date=datetime(2026, 10, 25, 12, 3)))

    assert chat.last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)


def test_aware_dialog_date_converted_to_utc():
    local = datetime(2026, 10, 25, 14, 3, tzinfo=timezone(timedelta(hours=2)))

    assert chat_from_dialog(dialog(tl_user(), date=local)).last_message_at == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)


def forum_topic(**kw):
    fields = dict(
        id=23, date=None, peer=tl.PeerChannel(10), title="Trips", icon_color=0, top_message=0,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, unread_poll_votes_count=0, from_id=tl.PeerUser(1), notify_settings=None,
    )
    fields.update(kw)
    return tl.ForumTopic(**fields)


def test_forum_topic_maps():
    topic = topic_from_forum_topic(forum_topic(closed=True))

    assert (topic.id, topic.title, topic.is_closed) == (23, "Trips", True)
    assert topic_from_forum_topic(forum_topic()).is_closed is False


def test_deleted_forum_topic_is_none():
    assert topic_from_forum_topic(tl.ForumTopicDeleted(id=9)) is None


# --- messages ------------------------------------------------------------

from tg_bridge.telegram.mapping import message_from_tl  # noqa: E402

SENT = datetime(2026, 10, 25, 12, 3)  # Telethon: naive, UTC


def tl_msg(id=100, text="hi", sender=None, out=False, **kw):
    msg = tl.Message(
        id=id, peer_id=kw.pop("peer_id", tl.PeerChannel(10)), date=kw.pop("date", SENT), message=text,
        out=out, from_id=tl.PeerUser(sender.id) if isinstance(sender, tl.User) else None, **kw,
    )
    msg._sender = sender
    return msg


def tl_doc(*attributes, mime="application/octet-stream", size=1000):
    return tl.MessageMediaDocument(
        document=tl.Document(
            id=1, access_hash=1, file_reference=b"", date=SENT, mime_type=mime, size=size,
            dc_id=1, attributes=list(attributes),
        )
    )


def test_plain_text_message():
    msg = message_from_tl(tl_msg(text="hello", sender=tl_user(id=5), out=True), chat_id=-1000000000010)

    assert (msg.id, msg.chat_id, msg.text, msg.out) == (100, -1000000000010, "hello", True)
    assert msg.date == datetime(2026, 10, 25, 12, 3, tzinfo=UTC)
    assert msg.sender.id == 5 and msg.sender.display_name == "Anna K"
    assert msg.sender_title is None and msg.media is None and msg.action is None


def test_edit_date_mapped():
    msg = message_from_tl(tl_msg(edit_date=datetime(2026, 10, 25, 13, 0)), 1)

    assert msg.edit_date == datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    assert message_from_tl(tl_msg(), 1).edit_date is None


def test_empty_text_is_empty_string():
    assert message_from_tl(tl_msg(text=None), 1).text == ""


def test_reply_to_id():
    msg = message_from_tl(tl_msg(reply_to=tl.MessageReplyHeader(reply_to_msg_id=7)), 1)

    assert (msg.reply_to_id, msg.topic_id) == (7, None)


def test_reply_to_topic_root_is_not_a_reply():
    msg = message_from_tl(tl_msg(reply_to=tl.MessageReplyHeader(forum_topic=True, reply_to_msg_id=12)), 1)

    assert (msg.topic_id, msg.reply_to_id) == (12, None)


def test_reply_inside_topic():
    header = tl.MessageReplyHeader(forum_topic=True, reply_to_msg_id=40, reply_to_top_id=12)

    msg = message_from_tl(tl_msg(reply_to=header), 1)

    assert (msg.topic_id, msg.reply_to_id) == (12, 40)


def test_forward_from_name():
    fwd = tl.MessageFwdHeader(date=SENT, from_name="Rust News")

    assert message_from_tl(tl_msg(fwd_from=fwd), 1).forward_from == "Rust News"


def test_forward_hidden():
    assert message_from_tl(tl_msg(fwd_from=tl.MessageFwdHeader(date=SENT)), 1).forward_from == "hidden"


def test_not_forwarded():
    assert message_from_tl(tl_msg(), 1).forward_from is None


def test_media_photo():
    sizes = [tl.PhotoSize(type="m", w=1, h=1, size=100), tl.PhotoSize(type="x", w=2, h=2, size=5000)]
    photo = tl.Photo(id=1, access_hash=1, file_reference=b"", date=SENT, sizes=sizes, dc_id=1)

    media = message_from_tl(tl_msg(media=tl.MessageMediaPhoto(photo=photo)), 1).media

    assert (media.kind, media.size) == ("photo", 5000)


def test_media_voice_and_audio():
    voice = message_from_tl(
        tl_msg(media=tl_doc(tl.DocumentAttributeAudio(duration=12, voice=True), mime="audio/ogg")), 1
    ).media
    audio = message_from_tl(
        tl_msg(media=tl_doc(tl.DocumentAttributeAudio(duration=200), tl.DocumentAttributeFilename("a.mp3"))), 1
    ).media

    assert (voice.kind, voice.duration) == ("voice", 12)
    assert (audio.kind, audio.file_name, audio.duration) == ("audio", "a.mp3", 200)


def test_media_video_note_video_gif():
    note = tl_doc(tl.DocumentAttributeVideo(duration=8, w=1, h=1, round_message=True))
    video = tl_doc(tl.DocumentAttributeVideo(duration=42, w=1, h=1), size=8_000_000)
    gif = tl_doc(tl.DocumentAttributeVideo(duration=2, w=1, h=1), tl.DocumentAttributeAnimated())

    assert message_from_tl(tl_msg(media=note), 1).media.kind == "video_note"
    v = message_from_tl(tl_msg(media=video), 1).media
    assert (v.kind, v.duration, v.size) == ("video", 42, 8_000_000)
    assert message_from_tl(tl_msg(media=gif), 1).media.kind == "gif"


def test_media_float_video_duration_becomes_int():
    # Telegram's video attribute carries duration as a double (e.g. 41.6); the format code needs whole seconds.
    video = tl_doc(tl.DocumentAttributeVideo(duration=41.6, w=1, h=1))

    duration = message_from_tl(tl_msg(media=video), 1).media.duration
    assert duration == 42
    assert type(duration) is int


def test_media_sticker_label():
    sticker = tl_doc(tl.DocumentAttributeSticker(alt="😀", stickerset=tl.InputStickerSetEmpty()))

    media = message_from_tl(tl_msg(media=sticker), 1).media

    assert (media.kind, media.label) == ("sticker", "😀")


def test_media_document():
    pdf = tl_doc(tl.DocumentAttributeFilename("report.pdf"), mime="application/pdf", size=340_000)

    media = message_from_tl(tl_msg(media=pdf), 1).media

    assert (media.kind, media.file_name, media.size, media.mime_type) == (
        "document", "report.pdf", 340_000, "application/pdf",
    )


def test_media_poll_label():
    poll = tl.Poll(
        id=1, question=tl.TextWithEntities("Pizza?", []),
        answers=[tl.PollAnswer(tl.TextWithEntities("yes", []), b"1")], hash=0,
    )

    media = message_from_tl(tl_msg(media=tl.MessageMediaPoll(poll=poll, results=tl.PollResults())), 1).media

    assert (media.kind, media.label) == ("poll", "Pizza?")


def test_media_location_contact_other():
    geo = tl.MessageMediaGeo(geo=tl.GeoPointEmpty())
    contact = tl.MessageMediaContact(phone_number="1", first_name="A", last_name="B", vcard="", user_id=1)

    assert message_from_tl(tl_msg(media=geo), 1).media.kind == "location"
    assert message_from_tl(tl_msg(media=contact), 1).media.kind == "contact"
    assert message_from_tl(tl_msg(media=tl.MessageMediaDice(value=3, emoticon="🎲")), 1).media.kind == "other"


def test_web_preview_is_not_media():
    web = tl.MessageMediaWebPage(webpage=tl.WebPageEmpty(id=1))

    assert message_from_tl(tl_msg(media=web), 1).media is None


def test_channel_post_sender_title():
    channel = tl_channel(broadcast=True, title="News")

    msg = message_from_tl(tl_msg(sender=channel, post=True), 1)
    signed = message_from_tl(tl_msg(sender=channel, post=True, post_author="Editor"), 1)

    assert (msg.sender, msg.sender_title) == (None, "News")
    assert signed.sender_title == "Editor"


def service(action, sender=None, **kw):
    msg = tl.MessageService(
        id=kw.pop("id", 101), peer_id=tl.PeerChannel(10), date=SENT, action=action,
        from_id=tl.PeerUser(sender.id) if sender else None, **kw,
    )
    msg._sender = sender
    return msg


def test_service_actions():
    anna = tl_user(id=5)

    def action_of(action, sender=anna, **kw):
        return message_from_tl(service(action, sender, **kw), 1).action

    assert action_of(tl.MessageActionChatAddUser(users=[5])) == "joined"
    assert action_of(tl.MessageActionChatAddUser(users=[6, 7])) == "added 2 members"
    assert action_of(tl.MessageActionChatJoinedByLink(inviter_id=1)) == "joined via link"
    assert action_of(tl.MessageActionChatDeleteUser(user_id=5)) == "left"
    assert action_of(tl.MessageActionChatDeleteUser(user_id=6)) == "removed a member"
    assert action_of(
        tl.MessageActionPinMessage(), reply_to=tl.MessageReplyHeader(reply_to_msg_id=4807)
    ) == "pinned #4807"
    assert action_of(tl.MessageActionChatEditTitle(title="T")) == 'renamed to "T"'
    assert action_of(tl.MessageActionChatCreate(title="x", users=[])) == "created chat"
    assert action_of(tl.MessageActionTopicCreate(title="Releases", icon_color=0)) == 'created topic "Releases"'
    assert action_of(tl.MessageActionScreenshotTaken()) == "screenshot_taken"


def test_message_empty_is_none():
    assert message_from_tl(tl.MessageEmpty(id=1, peer_id=tl.PeerChannel(10)), 1) is None


# --- media files ---------------------------------------------------------------

from tg_bridge.telegram.mapping import media_file_from_tl  # noqa: E402


def tl_photo_media(sizes):
    return tl.MessageMediaPhoto(
        photo=tl.Photo(id=1, access_hash=1, file_reference=b"", date=SENT, sizes=sizes, dc_id=2)
    )


def test_media_file_from_photo_has_jpg_extension_and_album_id():
    sizes = [tl.PhotoSize(type="x", w=2, h=2, size=5000)]

    found = media_file_from_tl(tl_msg(id=7, media=tl_photo_media(sizes), grouped_id=99))

    assert (found.message_id, found.album_id, found.extension, found.media.kind) == (7, 99, ".jpg", "photo")


def test_media_file_photo_size_is_largest_real_size():
    sizes = [
        tl.PhotoSize(type="s", w=1, h=1, size=1000),
        tl.PhotoSizeProgressive(type="y", w=3, h=3, sizes=[5000, 90000]),
        tl.PhotoStrippedSize(type="i", bytes=b"\x01\x02"),
    ]

    found = media_file_from_tl(tl_msg(media=tl_photo_media(sizes)))

    assert found.media.size == 90000


def test_media_file_voice_is_ogg_and_sticker_tgs():
    voice = tl_doc(tl.DocumentAttributeAudio(duration=12, voice=True), mime="audio/ogg")
    sticker = tl_doc(
        tl.DocumentAttributeSticker(alt="x", stickerset=tl.InputStickerSetEmpty()), mime="application/x-tgsticker"
    )
    pdf = tl_doc(tl.DocumentAttributeFilename("a.pdf"), mime="application/pdf")

    assert media_file_from_tl(tl_msg(media=voice)).extension == ".ogg"
    assert media_file_from_tl(tl_msg(media=sticker)).extension == ".tgs"
    assert media_file_from_tl(tl_msg(media=pdf)).extension == ".pdf"


def test_media_file_none_for_webpage_and_text():
    assert media_file_from_tl(tl_msg()) is None
    assert media_file_from_tl(tl_msg(media=tl.MessageMediaWebPage(webpage=tl.WebPageEmpty(id=1)))) is None


# --- dialog state for folder rules -------------------------------------------

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FUTURE = datetime(2026, 10, 26, tzinfo=UTC)
PAST = datetime(2026, 10, 24, tzinfo=UTC)


def test_user_is_contact():
    assert chat_from_dialog(dialog(tl_user(contact=True))).peer.is_contact is True
    assert chat_from_dialog(dialog(tl_user())).peer.is_contact is False


def test_missing_raw_dialog_means_default_state():
    chat = chat_from_dialog(dialog(tl_user()), now=NOW)

    assert (chat.is_muted, chat.is_unread, chat.has_unread_mention) == (False, False, False)


@pytest.mark.parametrize(
    "raw",
    [
        raw_dialog(unread_count=3),
        raw_dialog(unread_mark=True),
        raw_dialog(mentions=1),
        raw_dialog(reactions=2),
    ],
)
def test_is_unread_from_each_signal(raw):
    assert chat_from_dialog(dialog(tl_user(), raw=raw), now=NOW).is_unread is True


def test_read_dialog_is_not_unread():
    assert chat_from_dialog(dialog(tl_user(), raw=raw_dialog()), now=NOW).is_unread is False


def test_has_unread_mention():
    assert chat_from_dialog(dialog(tl_user(), raw=raw_dialog(mentions=2)), now=NOW).has_unread_mention is True
    assert chat_from_dialog(dialog(tl_user(), raw=raw_dialog(reactions=2)), now=NOW).has_unread_mention is False


def test_muted_by_own_future_mute_until():
    chat = chat_from_dialog(dialog(tl_user(), raw=raw_dialog(mute_until=FUTURE)), now=NOW)

    assert chat.is_muted is True


def test_expired_own_mute_until_is_not_muted():
    chat = chat_from_dialog(dialog(tl_user(), raw=raw_dialog(mute_until=PAST)), {"users": FUTURE}, now=NOW)

    assert chat.is_muted is False  # an own setting beats the account default


@pytest.mark.parametrize(
    "entity,category",
    [
        (tl_user(), "users"),
        (tl_user(bot=True), "users"),
        (tl.Chat(id=5, title="G", photo=tl.ChatPhotoEmpty(), participants_count=2, date=None, version=1), "chats"),
        (tl_channel(megagroup=True), "chats"),
        (tl_channel(broadcast=True), "broadcasts"),
    ],
)
def test_default_mute_by_category(entity, category):
    raw = raw_dialog(mute_until=None)
    defaults = {"users": None, "chats": None, "broadcasts": None, category: FUTURE}

    assert chat_from_dialog(dialog(entity, raw=raw), defaults, now=NOW).is_muted is True
    assert chat_from_dialog(dialog(entity, raw=raw), {}, now=NOW).is_muted is False


# --- folders ------------------------------------------------------------------


def tl_filter(**kw):
    return tl.DialogFilter(
        **{
            "id": 2,
            "title": tl.TextWithEntities("WAR", []),
            "pinned_peers": [],
            "include_peers": [],
            "exclude_peers": [],
            **kw,
        }
    )


def test_custom_folder_maps_all_fields():
    raw = tl_filter(
        emoticon="🪖",
        contacts=True,
        non_contacts=True,
        groups=True,
        broadcasts=True,
        bots=True,
        exclude_muted=True,
        exclude_read=True,
        exclude_archived=True,
        pinned_peers=[tl.InputPeerUser(user_id=7, access_hash=1)],
        include_peers=[tl.InputPeerChat(chat_id=8), tl.InputPeerChannel(channel_id=5, access_hash=1)],
        exclude_peers=[tl.InputPeerSelf()],
    )

    folder = folder_from_tl(raw, self_id=99)

    assert (folder.id, folder.title, folder.emoticon, folder.kind) == (2, "WAR", "🪖", "custom")
    assert (folder.contacts, folder.non_contacts, folder.groups, folder.broadcasts, folder.bots) == (True,) * 5
    assert (folder.exclude_muted, folder.exclude_read, folder.exclude_archived) == (True,) * 3
    assert folder.pinned_ids == (7,)
    assert folder.include_ids == (-8, -1000000000005)
    assert folder.exclude_ids == (99,)


def test_unset_flags_are_false():
    folder = folder_from_tl(tl_filter(), self_id=1)

    assert (folder.contacts, folder.exclude_muted, folder.emoticon) == (False, False, None)


def test_shared_folder_has_no_rules_or_excludes():
    raw = tl.DialogFilterChatlist(
        id=5,
        title=tl.TextWithEntities("F1", []),
        pinned_peers=[],
        include_peers=[tl.InputPeerChannel(channel_id=5, access_hash=1)],
    )

    folder = folder_from_tl(raw, self_id=1)

    assert folder.kind == "shared"
    assert (folder.contacts, folder.groups, folder.exclude_muted) == (False, False, False)
    assert folder.include_ids == (-1000000000005,)
    assert folder.exclude_ids == ()


def test_default_filter_is_none():
    assert folder_from_tl(tl.DialogFilterDefault(), self_id=1) is None


def test_unsupported_input_peers_are_skipped():
    raw = tl_filter(include_peers=[tl.InputPeerEmpty(), tl.InputPeerChat(chat_id=3)])

    assert folder_from_tl(raw, self_id=1).include_ids == (-3,)


def test_title_drops_custom_emoji_but_keeps_normal_one_before_it():
    text = "🔥 F1 ⭐"
    offset = len(text.encode("utf-16-le")) // 2 - 1  # UTF-16 units: the 🔥 takes two
    title = tl.TextWithEntities(text, [tl.MessageEntityCustomEmoji(offset=offset, length=1, document_id=1)])

    assert folder_from_tl(tl_filter(title=title), self_id=1).title == "🔥 F1"


def test_plain_string_title_passes_through():
    assert folder_from_tl(tl_filter(title="Plain  title"), self_id=1).title == "Plain title"


def test_empty_title_gets_placeholder():
    emoji_only = tl.TextWithEntities("⭐", [tl.MessageEntityCustomEmoji(offset=0, length=1, document_id=1)])

    assert folder_from_tl(tl_filter(id=7, title=emoji_only), self_id=1).title == "(folder 7)"


# --- channel posts and comments ------------------------------------------

from tg_bridge.source import Post  # noqa: E402
from tg_bridge.telegram.mapping import comment_from_tl, post_from_tl  # noqa: E402

DISCUSSION = -1000000000555


def test_comment_replying_to_thread_root_has_no_reply():
    header = tl.MessageReplyHeader(reply_to_msg_id=900, reply_to_top_id=None)

    msg = comment_from_tl(tl_msg(id=5012, reply_to=header), DISCUSSION)

    assert (msg.id, msg.chat_id, msg.reply_to_id) == (5012, DISCUSSION, None)


def test_comment_replying_to_comment_keeps_reply():
    header = tl.MessageReplyHeader(reply_to_msg_id=5008, reply_to_top_id=900)

    assert comment_from_tl(tl_msg(reply_to=header), DISCUSSION).reply_to_id == 5008


def test_comment_without_header_has_no_reply():
    assert comment_from_tl(tl_msg(), DISCUSSION).reply_to_id is None


def test_comment_sender_and_anonymous_title():
    anna = comment_from_tl(tl_msg(sender=tl_user(id=5)), DISCUSSION)
    assert anna.sender.display_name == "Anna K" and anna.sender_title is None
    channel = tl.Channel(id=1, title="Some Channel", photo=tl.ChatPhotoEmpty(), date=SENT)
    anon = comment_from_tl(tl_msg(sender=channel), DISCUSSION)
    assert anon.sender is None and anon.sender_title == "Some Channel"


def test_comment_empty_is_none():
    assert comment_from_tl(tl.MessageEmpty(id=1, peer_id=tl.PeerChannel(10)), DISCUSSION) is None


def test_post_with_comments():
    replies = tl.MessageReplies(replies=137, replies_pts=1, comments=True, channel_id=555)

    post = post_from_tl(tl_msg(id=3186, text="post", replies=replies), -1001234567890)

    assert isinstance(post, Post)
    assert (post.message.id, post.comments_enabled, post.comment_count, post.discussion_id) == (3186, True, 137, DISCUSSION)


def test_post_without_replies_has_comments_off():
    post = post_from_tl(tl_msg(id=3186), -1001234567890)

    assert (post.comments_enabled, post.comment_count, post.discussion_id) == (False, None, None)


def test_post_with_group_style_replies_has_comments_off():
    replies = tl.MessageReplies(replies=3, replies_pts=1, comments=False)

    post = post_from_tl(tl_msg(replies=replies), -1001234567890)

    assert (post.comments_enabled, post.comment_count, post.discussion_id) == (False, None, None)


def test_post_empty_and_service_are_none():
    assert post_from_tl(tl.MessageEmpty(id=1, peer_id=tl.PeerChannel(10)), 1) is None
    service = tl.MessageService(id=2, peer_id=tl.PeerChannel(10), date=SENT, action=tl.MessageActionChatEditPhoto(photo=tl.PhotoEmpty(id=0)))
    assert post_from_tl(service, 1) is None


def test_comment_replying_to_root_explicitly_has_no_reply():
    # A reply aimed at the post's copy itself (msg id == top id) is still a top-level comment.
    header = tl.MessageReplyHeader(reply_to_msg_id=900, reply_to_top_id=900)

    assert comment_from_tl(tl_msg(reply_to=header), DISCUSSION).reply_to_id is None


def test_comment_from_a_channel_keeps_its_username():
    channel = tl.Channel(id=1, title="Some Channel", photo=tl.ChatPhotoEmpty(), date=SENT, broadcast=True, username="somechan")

    msg = comment_from_tl(tl_msg(sender=channel), DISCUSSION)

    assert (msg.sender_title, msg.sender_username) == ("Some Channel", "somechan")


def test_anonymous_admin_of_a_group_gets_no_username():
    group = tl.Channel(id=1, title="Discussion", photo=tl.ChatPhotoEmpty(), date=SENT, megagroup=True, username="disc")

    assert comment_from_tl(tl_msg(sender=group), DISCUSSION).sender_username is None


@pytest.mark.parametrize(
    "replies, expected",
    [
        (tl.MessageReplies(replies=37, replies_pts=1, comments=True, channel_id=555), 37),
        (tl.MessageReplies(replies=0, replies_pts=1, comments=True, channel_id=555), 0),
        (tl.MessageReplies(replies=3, replies_pts=1, comments=False), None),
        (None, None),
    ],
)
def test_message_comment_count_only_for_channel_comments(replies, expected):
    # Telegram sends the count with every channel post, so showing it costs no request.
    assert message_from_tl(tl_msg(replies=replies), -1001234567890).comment_count == expected
