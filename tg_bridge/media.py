"""download_media logic: folders, names, size limit, continuing across calls. Telethon-free; bytes come from the source."""

import logging
import os
import re
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path

from tg_bridge.errors import MediaError
from tg_bridge.export import chat_slug
from tg_bridge.format import size_text
from tg_bridge.source import MEDIA_CHUNK, Chat, MediaFile, MessageSource

log = logging.getLogger(__name__)

DOWNLOADABLE = frozenset({"photo", "video", "gif", "voice", "audio", "video_note", "sticker", "document"})
# LM Studio gives up on a tool call after ~60 s; a call pauses after this many seconds and can be repeated.
DOWNLOAD_TIME_BUDGET = 40.0
MAX_NAME_CHARS = 120
MAX_SUFFIX = 99
# Unlike export._STRIP this also drops \n and \t: a file name has no use for them.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_name(name: str | None) -> str | None:
    """A file name Telegram gave us, made harmless: no separators, control characters or leading dots/spaces."""
    if not name:
        return None
    cleaned = _UNSAFE.sub("", name.replace("/", "").replace("\\", "")).lstrip(". ").rstrip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NAME_CHARS:
        stem, ext = os.path.splitext(cleaned)
        if len(ext) > 16:  # not a real extension
            stem, ext = cleaned, ""
        cleaned = stem[: MAX_NAME_CHARS - len(ext)] + ext
    return cleaned


@dataclass(frozen=True)
class SavedFile:
    path: Path
    file: MediaFile
    existed: bool  # found on disk, nothing fetched


@dataclass(frozen=True)
class DownloadResult:
    saved: tuple[SavedFile, ...]  # finished files, in album order
    folder: Path  # chat folder, or the album folder
    album: bool
    paused: bool
    done_bytes: int  # finished + partial bytes of all files on disk now
    total_bytes: int | None  # None when any size is unknown
    file_count: int  # files this call is about (done or not), for "2/3 files"


async def download_message_media(
    source: MessageSource,
    chat: Chat,
    message_id: int,
    root: Path,
    max_bytes: int,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> DownloadResult:
    """Saves the message's file (or its whole album) under `root`; never overwrites. After DOWNLOAD_TIME_BUDGET
    seconds it stops with `paused=True` and keeps a `.part` file, so the same call continues where it left off."""
    start = clock()
    files = await source.get_media_files(chat, message_id)
    if not files:
        raise MediaError(f"Message #{message_id} has no media to download.")
    asked = next((f for f in files if f.message_id == message_id), files[0])
    if asked.media.kind not in DOWNLOADABLE:
        raise MediaError(f"Message #{message_id} has a {asked.media.kind}; there is no file to download.")
    files = [f for f in files if f.media.kind in DOWNLOADABLE]  # siblings that are not files are skipped silently
    album = asked.album_id is not None and len(files) > 1
    _check_limit(files, album, message_id, max_bytes)

    chat_folder = _chat_folder(root, chat)
    folder = chat_folder / f"album_{asked.album_id}" if album else chat_folder
    sizes = [f.media.size for f in files]
    total = None if None in sizes else sum(sizes)

    saved: list[SavedFile] = []
    done_bytes = fetched = 0
    paused = False
    for file in files:
        if saved and clock() - start >= DOWNLOAD_TIME_BUDGET:
            paused = True
            break
        target, existing = _target(folder, file)
        if existing:
            saved.append(SavedFile(target, file, True))
            done_bytes += target.stat().st_size
            continue
        written, finished = await _fetch(source, chat, file, target, start, clock)
        fetched += written.fetched
        done_bytes += written.on_disk
        if not finished:
            paused = True
            break
        saved.append(SavedFile(target, file, False))
    status = "paused" if paused else ("existing" if fetched == 0 and all(s.existed for s in saved) else "complete")
    log.info(
        "download chat=%s messages=%s bytes=%d seconds=%.1f %s",
        chat.id, ",".join(str(f.message_id) for f in files), fetched, clock() - start, status,
    )
    return DownloadResult(tuple(saved), folder, album, paused, done_bytes, total, len(files))


def _check_limit(files: list[MediaFile], album: bool, message_id: int, max_bytes: int) -> None:
    limit_mb = max_bytes // (1024 * 1024)
    too_big = [f for f in files if f.media.size is not None and f.media.size > max_bytes]
    if not too_big:
        return
    if not album:
        f = too_big[0]
        raise MediaError(
            f"The {f.media.kind} in #{message_id} is {size_text(f.media.size)}, above the {limit_mb} MB download "
            f"limit (TG_BRIDGE_MAX_DOWNLOAD_MB). Nothing downloaded."
        )
    names = ", ".join(f"{f.media.kind} in #{f.message_id} ({size_text(f.media.size)})" for f in too_big)
    raise MediaError(
        f"The album of #{message_id} has files above the {limit_mb} MB download limit "
        f"(TG_BRIDGE_MAX_DOWNLOAD_MB): {names}. Nothing downloaded."
    )


def _chat_folder(root: Path, chat: Chat) -> Path:
    exact = root / f"{chat_slug(chat.title)}_{chat.id}"
    if exact.is_dir() or not root.is_dir():
        return exact
    # A renamed chat keeps its downloads: any folder ending in this chat's id is ours.
    others = [p for p in root.glob(f"*_{chat.id}") if p.is_dir()]
    return max(others, key=lambda p: p.stat().st_mtime) if others else exact


def _file_name(file: MediaFile) -> str:
    name = safe_name(file.media.file_name)
    return f"{file.message_id}_{name}" if name else f"{file.message_id}{file.extension}"


def _target(folder: Path, file: MediaFile) -> tuple[Path, bool]:
    """(path, True) of an earlier download of this file, else (first free path, False). Only names this function
    generates are looked at: `<name>`, then `<stem>_2<ext>` ... `<stem>_99<ext>`."""
    name = _file_name(file)
    stem, ext = os.path.splitext(name)
    expected = file.media.size
    free: Path | None = None
    for n in range(1, MAX_SUFFIX + 1):
        path = folder / (name if n == 1 else f"{stem}_{n}{ext}")
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            free = free or path
            continue
        if expected is None or size == expected:
            return path, True
    if free is None:
        raise MediaError("Too many different files with this name are already downloaded; nothing was saved.")
    return free, False


@dataclass
class _Progress:
    fetched: int  # bytes fetched by this call
    on_disk: int  # bytes of this file on disk afterwards


async def _fetch(
    source: MessageSource, chat: Chat, file: MediaFile, target: Path, start: float, clock: Callable[[], float]
) -> tuple[_Progress, bool]:
    """Appends to `<target>.part` from where it stopped. Returns (progress, finished); a finished file is moved to
    `target` without ever replacing an existing one."""
    expected = file.media.size
    part = target.with_name(target.name + ".part")
    offset = 0
    if part.exists():
        have = part.stat().st_size
        # Telegram's GetFile wants chunk-aligned offsets, and a crash can leave a ragged tail.
        offset = 0 if expected is not None and have > expected else have // MEDIA_CHUNK * MEDIA_CHUNK
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    written = offset
    ended = False
    try:
        os.ftruncate(fd, offset)
        if expected is None or offset < expected:
            async with aclosing(source.read_media(chat, file.message_id, offset)) as stream:
                async for chunk in stream:
                    _write_all(fd, chunk)
                    written += len(chunk)
                    if clock() - start >= DOWNLOAD_TIME_BUDGET:
                        break
                else:
                    ended = True
        else:
            ended = True
    finally:
        os.close(fd)
    finished = written >= expected if expected is not None else ended
    progress = _Progress(fetched=written - offset, on_disk=written)
    if not finished:
        return progress, False
    try:
        os.link(part, target)  # fails if `target` exists: nothing is ever overwritten
    except FileExistsError:
        raise MediaError("A file with this name appeared while downloading. Call download_media again.") from None
    os.unlink(part)
    return progress, True


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]
