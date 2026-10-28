import pytest
from telethon.errors import FloodWaitError
from telethon.tl import types as tl

from tests.fakes import make_chat
from tests.telegram.test_client import FakeTelethonClient, Factory, settings, wrapper  # noqa: F401
from tests.telegram.test_mapping import SENT, tl_doc, tl_msg
from tg_bridge.errors import MessageNotFoundError, RateLimitedError
from tg_bridge.source import MEDIA_CHUNK

CHAT = make_chat(id=-1000000000010)


def photo_msg(id, grouped_id=None):
    photo = tl.Photo(
        id=id, access_hash=5, file_reference=b"ref", date=SENT, dc_id=4,
        sizes=[
            tl.PhotoSize(type="s", w=1, h=1, size=1000),
            tl.PhotoSizeProgressive(type="y", w=3, h=3, sizes=[5000, 90000]),
            tl.PhotoStrippedSize(type="i", bytes=b"\x01\x02"),
        ],
    )
    return tl_msg(id=id, media=tl.MessageMediaPhoto(photo=photo), grouped_id=grouped_id)


def pdf_msg(id):
    return tl_msg(id=id, media=tl_doc(tl.DocumentAttributeFilename("a.pdf"), mime="application/pdf", size=3_000_000))


async def connected(wrapper):  # noqa: F811
    tg, fake = wrapper
    await tg.connect()
    return tg, fake


async def test_get_media_files_single_message(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {50: pdf_msg(50)}

    files = await tg.get_media_files(CHAT, 50)

    assert [(f.message_id, f.media.kind) for f in files] == [(50, "document")]
    assert fake.get_messages_calls == [(f"input:{CHAT.id}", [50])]


async def test_get_media_files_unknown_id_raises_not_found(wrapper):
    tg, fake = await connected(wrapper)

    with pytest.raises(MessageNotFoundError):
        await tg.get_media_files(CHAT, 50)


async def test_get_media_files_album_fetches_neighbours_once(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {
        49: tl_msg(id=49),  # plain message next to the album
        50: photo_msg(50, grouped_id=7),
        51: photo_msg(51, grouped_id=7),
        52: photo_msg(52, grouped_id=8),  # another album
        53: photo_msg(53, grouped_id=7),
    }

    files = await tg.get_media_files(CHAT, 51)

    assert [f.message_id for f in files] == [50, 51, 53]
    assert len(fake.get_messages_calls) == 2
    assert fake.get_messages_calls[1][1] == list(range(42, 61))


async def test_album_window_starts_at_1_for_small_ids(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {3: photo_msg(3, grouped_id=7)}

    await tg.get_media_files(CHAT, 3)

    assert fake.get_messages_calls[1][1] == list(range(1, 13))


async def test_get_media_files_text_message_is_empty(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {50: tl_msg(id=50)}

    assert await tg.get_media_files(CHAT, 50) == []


async def test_read_media_document_passes_offset_and_size(wrapper):
    tg, fake = await connected(wrapper)
    raw = pdf_msg(50)
    fake.messages_by_id = {50: raw}
    fake.download_chunks = [memoryview(b"ab"), memoryview(b"cd")]

    chunks = [c async for c in tg.read_media(CHAT, 50, 1024 * 1024)]

    assert chunks == [b"ab", b"cd"] and all(type(c) is bytes for c in chunks)
    file, offset, file_size, _, request_size = fake.iter_download_calls[0]
    assert file is raw.media.document
    assert (offset, file_size, request_size) == (1024 * 1024, raw.media.document.size, MEDIA_CHUNK)


async def test_read_media_photo_uses_largest_real_size(wrapper):
    tg, fake = await connected(wrapper)
    raw = photo_msg(50)
    fake.messages_by_id = {50: raw}

    [c async for c in tg.read_media(CHAT, 50, 0)]

    file, _, file_size, dc_id, _ = fake.iter_download_calls[0]
    assert isinstance(file, tl.InputPhotoFileLocation)
    assert (file.thumb_size, file_size, dc_id) == ("y", 90000, 4)
    assert (file.id, file.access_hash, file.file_reference) == (50, 5, b"ref")


async def test_read_media_reuses_raw_from_get_media_files(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {50: pdf_msg(50)}
    await tg.get_media_files(CHAT, 50)

    [c async for c in tg.read_media(CHAT, 50, 0)]

    assert len(fake.get_messages_calls) == 1


async def test_read_media_without_prior_fetch_fetches_the_one_id(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {50: pdf_msg(50)}

    [c async for c in tg.read_media(CHAT, 50, 0)]

    assert [ids for _, ids in fake.get_messages_calls] == [[50]]


async def test_read_media_flood_becomes_rate_limited(wrapper):
    tg, fake = await connected(wrapper)
    fake.messages_by_id = {50: pdf_msg(50)}
    fake.download_chunks = [b"ab", b"cd"]
    fake.download_fail_after = (1, FloodWaitError(request=None, capture=42))

    got = []
    with pytest.raises(RateLimitedError) as exc:
        async for c in tg.read_media(CHAT, 50, 0):
            got.append(c)

    assert got == [b"ab"] and exc.value.seconds == 42
