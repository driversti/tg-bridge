import logging
import os
import stat

import pytest

from tests.fakes import FakeSource, make_chat, make_media_file, make_message
from tg_bridge import media
from tg_bridge.errors import MediaError, MessageNotFoundError
from tg_bridge.export import chat_slug
from tg_bridge.media import DOWNLOAD_TIME_BUDGET, download_message_media, safe_name

CHAT = make_chat(id=-1001234567890, title="Family Chat")
MB = 1024 * 1024


class Clock:
    """Advances by `step` seconds per reading, so a test can say "the budget is gone after N reads"."""

    def __init__(self, step: float = 0.0, jump_after: int | None = None, jump: float = 100.0):
        self.now, self.step, self.reads, self.jump_after, self.jump = 0.0, step, 0, jump_after, jump

    def __call__(self) -> float:
        self.reads += 1
        if self.jump_after is not None and self.reads > self.jump_after:
            self.now += self.jump
            self.jump_after = None
        return self.now


def source_for(*entries, messages=()):
    """entries: (MediaFile, bytes)"""
    return FakeSource(
        media={(CHAT.id, f.message_id): (f, data) for f, data in entries},
        messages={CHAT.id: list(messages)},
    )


def chat_dir(root):
    return root / f"{chat_slug(CHAT.title)}_{CHAT.id}"


async def run(source, root, message_id=4812, max_bytes=200 * MB, clock=None):
    return await download_message_media(source, CHAT, message_id, root, max_bytes, clock=clock or Clock())


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(media, "MEDIA_CHUNK", 4)


async def test_photo_saved_as_id_jpg_in_chat_folder(tmp_path):
    source = source_for((make_media_file(4812, size=10), b"0123456789"))

    result = await run(source, tmp_path)

    path = chat_dir(tmp_path) / "4812.jpg"
    assert [s.path for s in result.saved] == [path]
    assert path.read_bytes() == b"0123456789"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(chat_dir(tmp_path).glob("*.part")) == []
    assert (result.paused, result.album, result.folder) == (False, False, chat_dir(tmp_path))
    assert (result.done_bytes, result.total_bytes) == (10, 10)


async def test_document_keeps_original_name_with_id_prefix(tmp_path):
    f = make_media_file(4812, kind="document", name="scan.pdf", size=3, mime="application/pdf", ext=".pdf")

    result = await run(source_for((f, b"abc")), tmp_path)

    assert result.saved[0].path == chat_dir(tmp_path) / "4812_scan.pdf"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("scan.pdf", "scan.pdf"),
        ("../../x.pdf", "x.pdf"),
        ("a/b.pdf", "ab.pdf"),
        ("a\\b.pdf", "ab.pdf"),
        ("...", None),
        ("  .hidden", "hidden"),
        ("", None),
        (None, None),
        ("a\x00b\nc\x7f.txt", "abc.txt"),
        # Bidi overrides and zero-width characters would make "invoice\u202efdp.exe" look like "invoiceexe.pdf".
        ("invoice\u202efdp.exe", "invoicefdp.exe"),
        ("a\u200bb\u2066c\ufeff.pdf", "abc.pdf"),
        ("\u202e", None),
    ],
)
def test_safe_name(raw, expected):
    assert safe_name(raw) == expected


def test_safe_name_caps_length_and_keeps_extension():
    name = safe_name("x" * 300 + ".pdf")

    assert len(name) <= 120 and name.endswith(".pdf")


async def test_hostile_name_stays_inside_chat_folder(tmp_path):
    f = make_media_file(4812, kind="document", name="../../etc/passwd", size=1, ext="")

    result = await run(source_for((f, b"x")), tmp_path)

    assert result.saved[0].path.parent == chat_dir(tmp_path)
    assert result.saved[0].path.name == "4812_etcpasswd"


async def test_name_that_is_only_dots_falls_back_to_id_and_extension(tmp_path):
    f = make_media_file(4812, kind="document", name="...", size=1, mime="application/pdf", ext=".pdf")

    result = await run(source_for((f, b"x")), tmp_path)

    assert result.saved[0].path.name == "4812.pdf"


async def test_album_goes_to_album_folder_in_id_order(tmp_path):
    entries = [(make_media_file(i, album_id=77, size=2), f"{i % 100}".encode()) for i in (4813, 4811, 4812)]

    result = await run(source_for(*entries), tmp_path, message_id=4812)

    folder = chat_dir(tmp_path) / "album_77"
    assert result.album and result.folder == folder
    assert [s.path for s in result.saved] == [folder / f"{i}.jpg" for i in (4811, 4812, 4813)]
    assert (folder / "4811.jpg").read_bytes() == b"11"
    assert result.done_bytes == 6 and result.total_bytes == 6


async def test_album_skips_non_downloadable_siblings(tmp_path):
    poll = make_media_file(4811, kind="poll", album_id=77, ext="")
    entries = [(poll, b""), (make_media_file(4812, album_id=77, size=1), b"x"), (make_media_file(4813, album_id=77, size=1), b"y")]

    result = await run(source_for(*entries), tmp_path)

    assert [s.file.message_id for s in result.saved] == [4812, 4813]


async def test_no_media(tmp_path):
    source = source_for(messages=[make_message(id=4812)])

    with pytest.raises(MediaError, match=r"^Message #4812 has no media to download\.$"):
        await run(source, tmp_path)


async def test_poll_is_not_downloadable(tmp_path):
    source = source_for((make_media_file(4812, kind="poll", ext=""), b""))

    with pytest.raises(MediaError, match=r"^Message #4812 has a poll; there is no file to download\.$"):
        await run(source, tmp_path)

    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


async def test_unknown_message_propagates_not_found(tmp_path):
    with pytest.raises(MessageNotFoundError):
        await run(source_for(), tmp_path)


async def test_over_limit_refuses_before_reading(tmp_path):
    source = source_for((make_media_file(4812, kind="video", size=3 * MB, mime="video/mp4", ext=".mp4"), b"x"))

    with pytest.raises(MediaError) as exc:
        await run(source, tmp_path, max_bytes=2 * MB)

    assert str(exc.value) == (
        "The video in #4812 is 3.0MB, above the 2 MB download limit (TG_BRIDGE_MAX_DOWNLOAD_MB). Nothing downloaded."
    )
    assert source.read_offsets == []
    assert list(tmp_path.iterdir()) == []


async def test_unknown_size_is_allowed_through_the_limit(tmp_path):
    source = source_for((make_media_file(4812, size=None), b"abc"))

    result = await run(source, tmp_path, max_bytes=1)

    assert result.total_bytes is None and result.saved[0].path.read_bytes() == b"abc"


async def test_album_with_one_file_over_limit_refuses_whole_album(tmp_path):
    entries = [
        (make_media_file(4811, album_id=7, size=1), b"x"),
        (make_media_file(4812, album_id=7, size=3 * MB), b"y"),
    ]
    source = source_for(*entries)

    with pytest.raises(MediaError, match=r"#4812") as exc:
        await run(source, tmp_path, max_bytes=2 * MB)

    assert "#4811" not in str(exc.value) and "Nothing downloaded" in str(exc.value)
    assert source.read_offsets == []


async def test_pauses_after_budget_and_continues_from_part(tmp_path, small_chunks):
    source = source_for((make_media_file(4812, size=10), b"0123456789"))
    final = chat_dir(tmp_path) / "4812.jpg"

    # One read at start; the 2nd and 3rd happen after chunks 1 and 2.
    first = await run(source, tmp_path, clock=Clock(jump_after=2))

    assert first.paused and first.saved == ()
    assert not final.exists()
    assert final.with_name("4812.jpg.part").read_bytes() == b"01234567"
    assert (first.done_bytes, first.total_bytes) == (8, 10)

    second = await run(source, tmp_path)

    assert not second.paused and [s.path for s in second.saved] == [final]
    assert final.read_bytes() == b"0123456789"
    assert not final.with_name("4812.jpg.part").exists()
    assert source.read_offsets == [(4812, 0), (4812, 8)]


async def test_budget_constant_is_forty_seconds():
    assert DOWNLOAD_TIME_BUDGET == 40.0


async def test_second_call_after_finish_is_existing(tmp_path):
    source = source_for((make_media_file(4812, size=3), b"abc"))
    first = await run(source, tmp_path)

    again = await run(source, tmp_path)

    assert again.saved[0].existed and not first.saved[0].existed
    assert again.saved[0].path == first.saved[0].path
    assert source.read_offsets == [(4812, 0)]
    assert len(list(chat_dir(tmp_path).iterdir())) == 1


async def test_name_taken_by_other_size_gets_suffix(tmp_path):
    folder = chat_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "4812.jpg").write_bytes(b"old")
    source = source_for((make_media_file(4812, size=5), b"01234"))

    result = await run(source, tmp_path)

    assert result.saved[0].path == folder / "4812_2.jpg"
    assert (folder / "4812.jpg").read_bytes() == b"old"
    assert (folder / "4812_2.jpg").read_bytes() == b"01234"


async def test_recall_finds_suffixed_file(tmp_path):
    folder = chat_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "4812.jpg").write_bytes(b"old")
    (folder / "4812_2.jpg").write_bytes(b"01234")
    source = source_for((make_media_file(4812, size=5), b"01234"))

    result = await run(source, tmp_path)

    assert result.saved[0].path == folder / "4812_2.jpg" and result.saved[0].existed
    assert not (folder / "4812_3.jpg").exists()
    assert source.read_offsets == []


async def test_oversized_part_restarts_from_zero(tmp_path):
    folder = chat_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "4812.jpg.part").write_bytes(b"x" * 50)
    source = source_for((make_media_file(4812, size=5), b"01234"))

    result = await run(source, tmp_path)

    assert result.saved[0].path.read_bytes() == b"01234"
    assert source.read_offsets == [(4812, 0)]


async def test_ragged_part_is_floored_to_media_chunk(tmp_path, small_chunks):
    folder = chat_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / "4812.jpg.part").write_bytes(b"01234567XX")
    source = source_for((make_media_file(4812, size=12), b"0123456789AB"))

    result = await run(source, tmp_path)

    assert source.read_offsets == [(4812, 8)]
    assert result.saved[0].path.read_bytes() == b"0123456789AB"


async def test_renamed_chat_reuses_existing_id_folder(tmp_path):
    old = tmp_path / f"old-title_{CHAT.id}"
    old.mkdir()
    source = source_for((make_media_file(4812, size=3), b"abc"))

    result = await run(source, tmp_path)

    assert result.saved[0].path == old / "4812.jpg"
    assert [p.name for p in tmp_path.iterdir()] == [old.name]


async def test_short_stream_counts_as_paused(tmp_path):
    source = source_for((make_media_file(4812, size=10), b"0123"))

    result = await run(source, tmp_path)

    assert result.paused and result.saved == () and result.done_bytes == 4
    assert (chat_dir(tmp_path) / "4812.jpg.part").exists()


async def test_pauses_between_album_files(tmp_path):
    entries = [(make_media_file(i, album_id=7, size=2), b"xx") for i in (1, 2, 3)]
    source = source_for(*entries)

    # clock reads: start, after file 1's chunk, then before file 2 (the jump) -> paused
    result = await run(source, tmp_path, message_id=1, clock=Clock(jump_after=2))

    assert result.paused and [s.file.message_id for s in result.saved] == [1]
    assert result.done_bytes == 2 and result.total_bytes == 6


async def test_logs_no_names(tmp_path, caplog):
    f = make_media_file(4812, kind="document", name="secret-plan.pdf", size=3, ext=".pdf")
    source = source_for((f, b"abc"))

    with caplog.at_level(logging.DEBUG):
        await run(source, tmp_path)

    text = caplog.text
    assert "secret-plan" not in text and "Family" not in text and str(tmp_path) not in text
    assert "complete" in text


async def test_part_is_kept_when_the_stream_fails(tmp_path, small_chunks):
    from tg_bridge.errors import RateLimitedError

    class Flaky(FakeSource):
        async def read_media(self, chat, message_id, offset):
            yield b"0123"
            raise RateLimitedError(5)

    f = make_media_file(4812, size=10)
    source = Flaky(media={(CHAT.id, 4812): (f, b"0123456789")})

    with pytest.raises(RateLimitedError):
        await run(source, tmp_path)

    assert (chat_dir(tmp_path) / "4812.jpg.part").read_bytes() == b"0123"


def test_final_file_is_never_overwritten_by_link(tmp_path):
    # os.link fails when the target exists; the download code relies on it.
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"1")
    b.write_bytes(b"2")

    with pytest.raises(FileExistsError):
        os.link(a, b)
