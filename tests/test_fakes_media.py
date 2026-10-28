import pytest

from tests.fakes import FakeSource, make_chat, make_media_file, make_message
from tg_bridge.errors import MessageNotFoundError


def source_with(*files_and_data, messages=()):
    chat = make_chat()
    media = {(chat.id, f.message_id): (f, data) for f, data in files_and_data}
    return chat, FakeSource(media=media, messages={chat.id: list(messages)})


async def test_album_siblings_come_back_sorted_by_id():
    files = [make_media_file(i, album_id=7) for i in (12, 10, 11)]
    other = make_media_file(20, album_id=8)
    chat, source = source_with(*[(f, b"x") for f in files], (other, b"y"))

    found = await source.get_media_files(chat, 11)

    assert [f.message_id for f in found] == [10, 11, 12]


async def test_single_file_without_album_is_alone():
    chat, source = source_with((make_media_file(5), b"x"), (make_media_file(6), b"y"))

    assert [f.message_id for f in await source.get_media_files(chat, 5)] == [5]


async def test_unknown_id_raises_not_found():
    chat, source = source_with()

    with pytest.raises(MessageNotFoundError):
        await source.get_media_files(chat, 99)


async def test_known_text_message_has_no_files():
    chat, source = source_with(messages=[make_message(id=3)])

    assert await source.get_media_files(chat, 3) == []


async def test_read_media_streams_from_offset_in_chunks():
    chat, source = source_with((make_media_file(5), b"0123456789"))

    chunks = [c async for c in source.read_media(chat, 5, 6)]

    assert b"".join(chunks) == b"6789"
    assert source.read_offsets == [(5, 6)]
