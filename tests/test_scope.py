from dataclasses import replace

import pytest

from tests.fakes import make_chat, make_person, make_user_chat
from tg_bridge.scope import chat_visible, is_public, needs_allowlist

NONE: frozenset[int] = frozenset()


def group(**kw):
    return make_chat(id=-555, kind="group", username=None, **kw)


def supergroup(username=None, **kw):
    return make_chat(id=-1001, kind="supergroup", username=username, **kw)


def channel(username=None, **kw):
    return make_chat(id=-1002, kind="channel", username=username, **kw)


def user_chat(**kw):
    return make_user_chat(**kw)


def test_user_hidden_when_off_visible_when_on():
    chat = make_user_chat()
    assert not chat_visible(chat, NONE)
    assert chat_visible(chat, frozenset({chat.id}))
    assert needs_allowlist(chat)


def test_username_never_makes_a_user_or_bot_public():
    anna = make_user_chat(make_person(id=42, username="anna_k"))
    bot = make_user_chat(make_person(id=77, username="helper_bot", is_bot=True))
    assert anna.username and bot.kind == "bot" and bot.username
    for chat in (anna, bot):
        assert not is_public(chat)
        assert not chat_visible(chat, NONE)
        assert chat_visible(chat, frozenset({chat.id}))
        assert needs_allowlist(chat)


def test_saved_messages_off_and_on():
    chat = make_user_chat(make_person(id=99), is_self=True)
    assert not chat_visible(chat, NONE)
    assert chat_visible(chat, frozenset({99}))
    assert needs_allowlist(chat)


def test_basic_group_follows_allowlist():
    chat = group()
    assert not chat_visible(chat, NONE)
    assert chat_visible(chat, frozenset({-555}))
    assert needs_allowlist(chat)


@pytest.mark.parametrize("factory", [supergroup, channel])
def test_public_username_is_visible_without_allowlist(factory):
    chat = factory(username="pub")
    assert chat_visible(chat, NONE)
    assert not needs_allowlist(chat)


@pytest.mark.parametrize("factory", [supergroup, channel])
def test_private_supergroup_and_channel_follow_allowlist(factory):
    chat = factory()
    assert not chat_visible(chat, NONE)
    assert chat_visible(chat, frozenset({chat.id}))
    assert needs_allowlist(chat)


def test_forum_supergroup_private_follows_allowlist():
    chat = supergroup(is_forum=True)
    assert not chat_visible(chat, NONE)
    assert chat_visible(chat, frozenset({chat.id}))
    assert needs_allowlist(chat)


def test_empty_username_counts_as_private():
    chat = supergroup(username="")
    assert not chat_visible(chat, NONE)


def test_unknown_kind_is_hidden_even_if_allowlisted():
    chat = replace(group(), kind="secret")
    assert not chat_visible(chat, frozenset({chat.id}))
    assert not needs_allowlist(chat)
    assert not is_public(chat)


def test_public_kinds_need_no_allowlist():
    assert is_public(channel(username="pub")) and is_public(supergroup(username="pub"))
    assert not is_public(group())
