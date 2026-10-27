from types import SimpleNamespace

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

from tg_bridge.telegram.mapping import chat_from_dialog, topic_from_forum_topic  # noqa: E402


def dialog(entity, id=1, date=datetime(2026, 10, 25, 12, 3, tzinfo=UTC), folder_id=None):
    return SimpleNamespace(id=id, entity=entity, date=date, folder_id=folder_id)


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
