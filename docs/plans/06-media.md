# 06 — Media Download Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `download_media(chat, message_id)` saves one message's media (or its whole album) under the download dir and returns only the path, size and mime type. Big files continue across calls, as `export_chat` does.

**Architecture:** Two new `MessageSource` methods: `get_media_files` (the message's file, or every file of its album) and `read_media` (an async byte stream from an offset). `ReadOnlyTelegram` implements them with `client.get_messages(ids=…)` and `client.iter_download` (both pure reads). A new Telethon-free module `tg_bridge/media.py` owns folders, names, the size limit, the `.part` file, the time budget and "already downloaded". The tool in `tools.py` resolves the chat and words the result.

**Tech Stack:** unchanged (Python ≥3.12, `telethon` 1.45, `mcp` v2 `MCPServer`, `pytest-asyncio`).

**Spec:** `docs/features/06-media.md` + `docs/design.md` §7 (`download_media`, errors), §8 (download dir, logging), §9, §11 (never overwrite), D8. Pattern to copy: `tg_bridge/export.py` (time budget, `O_EXCL` files, "Next: call …" wording) and `docs/plans/04-export.md`.

## Global Constraints

All constraints from plans 00–04 still apply. Additionally:

- Telegram calls: only `get_messages(entity, ids=[…])` and `iter_download(…)` (requests `GetMessages`/`channels.GetMessages`, `upload.GetFile`). Add those names to `ALLOWED_REQUESTS` in `tests/telegram/test_client.py`. Nothing downloads unless `download_media` is called (D8).
- Never overwrite: final files are created by `os.link(part, final)` (fails if `final` exists) and then `os.unlink(part)`. `.part` files are opened `O_WRONLY | O_CREAT | O_APPEND`, mode `0o600`. Folders `mkdir(parents=True, exist_ok=True, mode=0o700)`.
- Logs: chat id, message id(s), bytes written, seconds, `complete|paused|existing`. **Never** paths, file names, captions or titles.
- `TG_BRIDGE_MAX_DOWNLOAD_MB` (default `200`, positive integer, else `ConfigError("TG_BRIDGE_MAX_DOWNLOAD_MB must be a positive integer")`). `Settings.max_download_bytes = mb * 1024 * 1024`. Add the row to the table in `docs/setup.md`.
- The tool's returned text obeys `settings.max_chars` (truncate like `export_chat`).
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here

Maintainer's choices (2026-10-28): files only land on disk (no content returned to the model); big files **continue across calls** (option 1: ~40 s per call, same arguments again); an album message downloads **the whole album**. The rest are Claude's calls.

- **Downloadable kinds:** `photo, video, gif, voice, audio, video_note, sticker, document`. `poll, contact, location, other` → ToolError `Message #<id> has a <kind>; there is no file to download.` No media (incl. web-page previews) → `Message #<id> has no media to download.` Unknown id → `MessageNotFoundError`: `Message #<id> not found in this chat.`
- **Album:** the message's `grouped_id`; siblings are found with one `get_messages(entity, ids=range(id-9, id+10))` (albums hold ≤10 items, consecutive ids) filtered by the same `grouped_id`, ordered by id. Non-downloadable siblings are skipped silently.
- **Folders:** chat folder = `<slug>_<chat.id>` under `settings.download_dir`, `slug` exactly as in export filenames (extract `chat_slug(title: str) -> str` from `export_filename` into `export.py` and use it in both). An existing folder matching `*_<chat.id>` is reused, so a renamed chat keeps its downloads (same trick as `_reopen_latest`). Album files go to `<chat folder>/album_<grouped_id>/`.
- **File names:** `<message_id>_<safe original name>` when Telegram has a name, else `<message_id><extension>` (extension from Telethon `utils.get_extension`, `.jpg` for photos, `""` if unknown). The id prefix keeps two `scan.pdf` from different messages apart and makes "already downloaded" a lookup. `safe_name`: drop `/`, `\`, control characters (`export._STRIP`), leading dots and spaces; cap at 120 characters keeping the extension; empty → no name.
- **Extension overrides** (Python's `mimetypes` gives `.oga` / nothing): `audio/ogg → .ogg`, `application/x-tgsticker → .tgs`; otherwise `utils.get_extension`.
- **Already downloaded:** only names this rule generates are checked, in order: `<name>`, `<stem>_2<ext>` … `<stem>_99<ext>` (no globbing). The first that exists with the expected size (any size when unknown) is reused, nothing fetched, reported as existing. Otherwise the first free candidate becomes the target. So a `4812.jpg` of another size leads to `4812_2.jpg`, and a re-call finds `4812_2.jpg` instead of making `_3`.
- **Size limit:** checked before any byte is fetched. Single file over the limit → ToolError `The <kind> in #<id> is <size>, above the <N> MB download limit (TG_BRIDGE_MAX_DOWNLOAD_MB). Nothing downloaded.` In an album any file over the limit refuses the whole album, naming those messages. Unknown size (`None`) is allowed.
- **Continuing:** `<final name>.part` holds the progress. Offset = its size floored to a multiple of `MEDIA_CHUNK = 512 * 1024` (Telegram's `upload.GetFile` needs aligned offsets; a crash can leave a ragged tail), and the `.part` is truncated to that offset first. A `.part` bigger than the expected size is truncated to 0. Bytes are appended chunk by chunk (Telethon's 512 KiB requests); after each chunk, and before each album file, `clock() - start >= DOWNLOAD_TIME_BUDGET` (`40.0`) pauses: the `.part` stays, the result says paused. A stream that ends short of the expected size also counts as paused (call again).
- **Errors mid-download:** `RateLimitedError` and other `TgBridgeError` keep the `.part` and become a ToolError that ends with `Progress is kept: call download_media(chat="<id>", message_id=<id>) again to continue.`. `OSError` → ToolError `Could not write the file: <e.strerror>.` (strerror only: `str(e)` holds the path). Anything else keeps the `.part` and propagates.
- **`read_media` call:** `iter_download(file, offset=offset, file_size=size, request_size=MEDIA_CHUNK, dc_id=…)`; each chunk is converted with `bytes(chunk)` (Telethon's generic iterator yields `memoryview`s). `MEDIA_CHUNK` lives in `source/models.py` so `media.py` and `telegram/client.py` share it.
- **Album window:** `range(max(1, id - 9), id + 10)`.
- **Foreign ids:** in private chats and basic groups message ids are account-wide; Telethon's `get_messages(entity, ids=…)` already returns `None` for an id that belongs to another chat (it compares `peer_id`), so passing the entity is mandatory and `None` → `MessageNotFoundError`.
- **Photo size:** the largest of `photo.sizes` (`PhotoSize.size`, `PhotoSizeProgressive.max(sizes)`; never `PhotoStrippedSize`/`PhotoCachedSize`), downloaded via `tl.InputPhotoFileLocation(id, access_hash, file_reference, thumb_size=size.type)` with `dc_id=photo.dc_id`. `media_file_from_tl` sets `MediaFile.media.size` to that same byte count (Telethon's `msg.file.size` is an estimate that can differ), so the limit, "already downloaded" and short-stream checks compare against what is really fetched. Telethon's own `_get_file_info(Photo)` takes `sizes[-1]`, which can be the stripped thumbnail. Documents: pass `raw.media.document` (Telethon builds the location).
- **Raw messages** fetched by `get_media_files` are kept in `ReadOnlyTelegram._media_raw: dict[tuple[int, int], Any]` so `read_media` needs no second fetch; on a miss `read_media` fetches the one id.

## File Structure

| File | Change |
|---|---|
| `tg_bridge/config.py` | `TG_BRIDGE_MAX_DOWNLOAD_MB`, `Settings.max_download_bytes` |
| `tg_bridge/source/models.py`, `source/__init__.py` | `MediaFile` |
| `tg_bridge/source/protocol.py`, `source/live.py` | `get_media_files`, `read_media` |
| `tg_bridge/errors.py` | `MessageNotFoundError` |
| `tg_bridge/telegram/mapping.py` | `media_file_from_tl` |
| `tg_bridge/telegram/client.py` | `get_media_files`, `read_media` |
| `tg_bridge/export.py` | extract `chat_slug` |
| `tg_bridge/media.py` (new) | `download_message_media`, `safe_name`, results |
| `tg_bridge/tools.py`, `tg_bridge/server.py` | `download_media` tool |
| `tests/fakes.py` | `FakeSource.media` |

## Review Focus

1. **Calling again after it finished** → the same path back as "already downloaded", no duplicate file, no Telegram download (Task 3).
2. **A hostile or odd original file name** (`../../x`, `a/b.pdf`, 300 characters, only dots) → a file inside the chat folder with a sane name (Task 3).
3. **A photo whose `sizes` end with a `PhotoStrippedSize`** → the largest real size is downloaded, not a 1 KB blur (Task 2).
4. **A leftover `.part` larger than the file** (media changed, or a broken earlier run) → restarts from 0 and ends with the right size (Task 3).
5. **The chat was renamed between calls** → the existing `*_<id>` folder is reused and the download continues (Task 3).

---

### Task 1: Domain model, config, fake source

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/__init__.py`, `tg_bridge/source/protocol.py`, `tg_bridge/errors.py`, `tg_bridge/config.py`, `tests/fakes.py`, `docs/setup.md`
- Test: `tests/test_config.py`, `tests/test_fakes_media.py` (new)

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class MediaFile:
      message_id: int
      album_id: int | None      # Telegram grouped_id
      media: MediaInfo          # kind, file_name, size, mime_type, …
      extension: str            # ".jpg", ".pdf", "" when unknown

  class MessageSource(Protocol):  # added
      async def get_media_files(self, chat: Chat, message_id: int) -> list[MediaFile]:
          """The message's media as one file, or every file of its album ordered by id; [] when it has no media.
          Raises MessageNotFoundError for an unknown id."""
      def read_media(self, chat: Chat, message_id: int, offset: int) -> AsyncIterator[bytes]:
          """That message's file content from byte `offset` to the end, in chunks."""

  class MessageNotFoundError(TgBridgeError):
      def __init__(self, message_id: int): ...  # "Message #<id> not found in this chat."

  MEDIA_CHUNK = 512 * 1024  # in source/models.py, exported from tg_bridge.source
  Settings.max_download_bytes: int
  FakeSource(..., media: dict[tuple[int, int], tuple[MediaFile, bytes]] | None = None, chunk: int = 4)
  FakeSource.read_offsets: list[tuple[int, int]]  # (message_id, offset) per read_media call
  ```
  `FakeSource.get_media_files` returns the entry for `(chat.id, message_id)` plus every entry of that chat with the same non-None `album_id`, sorted by id; raises `MessageNotFoundError` when the key is missing **and** `(chat.id, message_id)` is not among `self.messages`; returns `[]` for a known message without media. `read_media` yields `data[offset:]` in `chunk`-byte slices.

- [ ] **Step 1: Failing tests**: `test_max_download_mb_default_is_200_mib` (`== 200 * 1024 * 1024`), `test_max_download_mb_must_be_positive_integer` (`"0"`, `"abc"`, `"-5"` → `ConfigError` naming the variable); in `test_fakes_media.py`: album siblings sorted by id, unknown id raises, known text message gives `[]`, `read_media(offset=6)` over `b"0123456789"` with `chunk=4` → `b"".join(chunks) == b"6789"` and `read_offsets == [(id, 6)]`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_config.py tests/test_fakes_media.py` → FAIL.
- [ ] **Step 3: Implement** the interfaces above; setup.md row: `TG_BRIDGE_MAX_DOWNLOAD_MB | 200 | Largest file download_media fetches (MB)`.
- [ ] **Step 4: Run** `uv run pytest -q` → all green (`FakeSource` still satisfies the protocol in existing tests).
- [ ] **Step 5: Commit** `feat(media): MediaFile model, download size limit, fake media source`.

### Task 2: Telegram side

**Files:**
- Modify: `tg_bridge/telegram/mapping.py`, `tg_bridge/telegram/client.py`, `tg_bridge/source/live.py`
- Test: `tests/telegram/test_mapping.py`, `tests/telegram/test_client_media.py` (new), `tests/telegram/test_client.py` (`ALLOWED_REQUESTS`, fake client), `tests/test_live_source.py`

**Interfaces:**
- Consumes: `MediaFile`, `MessageNotFoundError` (Task 1); existing `_media(msg)` in mapping.
- Produces: `media_file_from_tl(raw) -> MediaFile | None` (None when `_media` is None; `album_id = raw.grouped_id`; `extension = utils.get_extension(raw.media)`); `ReadOnlyTelegram.get_media_files(chat, message_id) -> list[MediaFile]`; `ReadOnlyTelegram.read_media(chat, message_id, offset) -> AsyncIterator[bytes]`; `LiveSource` passes both through after `_ensure_connected()`.
- `FakeTelethonClient` gains `get_messages(entity, ids)` (returns one raw or `None` per id from `self.messages_by_id`, records calls) and `iter_download(file, *, offset, file_size, dc_id=None, **kw)` (records `(file, offset, file_size, dc_id)`, yields `self.download_chunks`, optionally raises `FloodWaitError` after N chunks).

- [ ] **Step 1: Failing tests**
  - `test_media_file_from_photo_has_jpg_extension_and_album_id`, `test_media_file_photo_size_is_largest_real_size`, `test_media_file_voice_is_ogg_and_sticker_tgs`, `test_media_file_none_for_webpage_and_text`.
  - `test_get_media_files_single_message` (one `get_messages` call with the entity and `ids=[id]`), `test_get_media_files_unknown_id_raises_not_found` (fake returns `None`), `test_get_media_files_album_fetches_neighbours_once` (second call `ids == list(range(id-9, id+10))`, result only same `grouped_id`, sorted), `test_album_window_starts_at_1_for_small_ids`, `test_get_media_files_text_message_is_empty`.
  - `test_read_media_document_passes_offset_and_size` (`file is raw.media.document`, `offset == 1024 * 1024`, `file_size == document.size`, `request_size == MEDIA_CHUNK`; memoryview chunks come out as `bytes`), `test_read_media_photo_uses_largest_real_size` (sizes `[PhotoSize("s", 1000), PhotoSizeProgressive("y", [5000, 90000]), PhotoStrippedSize("i", b"..")]` → `InputPhotoFileLocation` with `thumb_size == "y"`, `file_size == 90000`, `dc_id == photo.dc_id`), `test_read_media_reuses_raw_from_get_media_files` (no extra `get_messages`), `test_read_media_flood_becomes_rate_limited`.
  - `test_only_read_requests_used` still passes with the new calls; `test_live_source` passthroughs connect first.
- [ ] **Step 2: Run** `uv run pytest -q tests/telegram tests/test_live_source.py` → FAIL.
- [ ] **Step 3: Implement.** `read_media` is an async generator; wrap its iteration so `FloodError` becomes `RateLimitedError` (same mapping as `_call`). Entities via `_input_entity`. Log `media files=%d chat=%s` only.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(media): read message media from Telegram by offset`.

### Task 3: `media.py`: folders, names, limit, continuing

**Files:**
- Create: `tg_bridge/media.py`
- Modify: `tg_bridge/export.py` (extract `chat_slug`), `tg_bridge/errors.py` (`MediaError`)
- Test: `tests/test_media.py` (new), `tests/test_export_render.py` (filename unchanged)

**Interfaces:**
- Consumes: `MessageSource.get_media_files/read_media`, `FakeSource(media=…)` (Task 1), `export.chat_slug`.
- Produces:
  ```python
  DOWNLOADABLE = frozenset({"photo", "video", "gif", "voice", "audio", "video_note", "sticker", "document"})
  DOWNLOAD_TIME_BUDGET = 40.0

  def safe_name(name: str | None) -> str | None: ...

  @dataclass(frozen=True)
  class SavedFile:
      path: Path
      file: MediaFile
      existed: bool            # found on disk, nothing fetched

  @dataclass(frozen=True)
  class DownloadResult:
      saved: tuple[SavedFile, ...]   # finished files, in album order
      folder: Path                   # chat folder, or the album folder
      album: bool
      paused: bool
      done_bytes: int                # finished + partial bytes of all files on disk now
      total_bytes: int | None        # None when any size is unknown

  async def download_message_media(
      source: MessageSource, chat: Chat, message_id: int, root: Path, max_bytes: int,
      *, clock: Callable[[], float] = time.monotonic,
  ) -> DownloadResult
  ```
  Raises `MediaError(TgBridgeError)` (new, in `errors.py`) with the exact texts from "Decisions" for no media / non-downloadable / over the limit; lets `MessageNotFoundError`, `RateLimitedError` and `OSError` through (the tool words them).

- [ ] **Step 1: Failing tests** (all with `FakeSource(media=…)`, `tmp_path` as root, a fake clock):
  - `test_photo_saved_as_id_jpg_in_chat_folder` → `root/<slug>_<id>/4812.jpg`, bytes equal, mode `0o600`, no `.part` left.
  - `test_document_keeps_original_name_with_id_prefix` → `4812_scan.pdf`.
  - `test_safe_name` table: `"../../x.pdf"→"x.pdf"`-style (no `/`, no leading dots), `"a/b.pdf"`, 300-char name ≤120 keeps `.pdf`, `"..."→None`, control chars dropped.
  - `test_album_goes_to_album_folder_in_id_order` (three entries, same `album_id`).
  - `test_no_media`, `test_poll_is_not_downloadable`, `test_over_limit_refuses_before_reading` (`source.read_offsets == []`), `test_album_with_one_file_over_limit_refuses_whole_album`.
  - `test_pauses_after_budget_and_continues_from_part` (with `MEDIA_CHUNK` monkeypatched to 4 in `media`, as in all `.part` tests: clock jumps past 40 s after 2 chunks → `paused`, `.part` holds 8 bytes; second call reads from offset 8 and finishes; `read_offsets == [(id, 0), (id, 8)]`).
  - `test_second_call_after_finish_is_existing` (`existed=True`, no new `read_offsets`, still one file).
  - `test_name_taken_by_other_size_gets_suffix` (pre-existing `4812.jpg` of other size → `4812_2.jpg`; the old file untouched).
  - `test_oversized_part_restarts_from_zero`.
  - `test_ragged_part_is_floored_to_media_chunk` (monkeypatch `MEDIA_CHUNK` to 4 in `media`; a 10-byte `.part` → truncated to 8, read from 8).
  - `test_recall_finds_suffixed_file` (`4812.jpg` other size + `4812_2.jpg` right size → existing `4812_2.jpg`, no `_3`).
  - `test_renamed_chat_reuses_existing_id_folder` (folder `old-title_<id>` exists → used).
  - `test_short_stream_counts_as_paused`.
  - `test_logs_no_names` (caplog has no file name, title or path).
- [ ] **Step 2: Run** `uv run pytest -q tests/test_media.py` → FAIL.
- [ ] **Step 3: Implement** in `tg_bridge/media.py`; all writes through `os.open`/`os.link` as in Global Constraints.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(media): download into the chat folder, continue big files across calls`.

### Task 4: The `download_media` tool

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools_media.py` (new), `tests/test_server.py`

**Interfaces:**
- Consumes: `download_message_media`, `DownloadResult` (Task 3); `resolve_chat_arg`, `_tool_errors`, `format.size_text`, `one_line`.
- Produces: `async def download_media(source, settings, chat: str, message_id: int, *, clock=time.monotonic) -> str`; server tool `download_media(chat: str, message_id: int)` with `WRITES_LOCAL_FILE` annotations.

Result texts (`<who>` = `one_line(title) (id)`, `<call>` = `download_media(chat="<chat id>", message_id=<id>)`):
- single, finished: `Downloaded <kind> (<size>, <mime>) from <who> #<id> to:\n<path>`; existing: first line `Already downloaded: <kind> (<size>, <mime>) from <who> #<id>:`.
- album, finished: `Downloaded album of <n> files (<total>) from <who> #<id> to folder:\n<folder>` then one line per file `<path> · <size> · <mime>` (add ` (already there)` for existing).
- paused: `Downloaded <done> of <total> so far from <who> #<id> (<n done>/<n> files). Not finished: one call has a time limit. Next: call <call>` (`of <total>` dropped when unknown).
- Then always: `The file was not read into this conversation.` (on finished results only).
- Errors as ToolError: `MediaError`/`MessageNotFoundError` texts as is; `TgBridgeError` mid-download → `<str(e)>. Progress is kept: call <call> again to continue.`; `OSError` → `Could not write the file: <e.strerror>.`

Docstring (server): `Save one message's photo, video, voice, document… to a local file and return only its path, size and type, NOT its content. Only when the user asks for the file. `chat`: id, @username or title words. `message_id`: the #id from get_messages or search_messages. A message in an album downloads the whole album. One call works for about 40 seconds: if the result says "Next: call download_media(...)", make exactly that call until it no longer says so.`

- [ ] **Step 1: Failing tests** per text above (single, existing, album, paused with/without total, over limit, not found, rate limit keeps progress wording, OSError uses strerror and no path, truncation to `max_chars`); `test_download_media_registered_as_local_file_writer`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tools_media.py tests/test_server.py` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` → green; `uv run tg-bridge` starts and lists 7 tools (`test_server_process`).
- [ ] **Step 5: Commit** `feat(media): download_media tool`.

### Task 5: Docs

- [ ] Brief and roadmap already updated in the plan session; tick nothing here unless the implementation deviated (then add a one-line note under roadmap "Log").
- [ ] Commit any doc tweaks with the last feature commit.
