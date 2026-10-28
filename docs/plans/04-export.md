# 04 — Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `export_chat(chat, period…)` streams a conversation (or a period of it) into a readable Markdown file and returns only the path, message count and date range, so the full text never passes through the model's context.

**Architecture:** A new module `tg_bridge/export.py` with pure renderers (header, day headings, message paragraphs, filename) and one async streamer `export_chat_file` that pages through the **existing** `MessageSource.get_messages` (forward, `boundary_id` paging) and appends each page to the file. No protocol change. The tool in `tools.py` resolves the chat, parses the period, fetches `get_chat_info` for the header and formats a short result. Feature 05 reuses `export_chat_file` with another `dest_dir`.

**Tech Stack:** unchanged (Python ≥3.12, `telethon` 1.45, `mcp` v2 `MCPServer`, `pytest-asyncio`).

**Spec:** `docs/features/04-export.md` + `docs/design.md` §6 (`export`), §7 (period args, errors), §8 (export dir, logging), §9, §11 (exports: slug + id + period + timestamp, never overwrite, no size cap), D9.

## Global Constraints

All constraints from plans 00–03 still apply. Additionally:

- Telegram calls: only what `get_messages` already does (`iter_messages`, a pure read) plus `get_chat_info`. Nothing new in `telegram/`.
- Files are created with mode `0o600` via `os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)`: **never overwrite**. The export dir is created if missing (`mkdir(parents=True, exist_ok=True, mode=0o700)`).
- Logs: chat id, message count, page count, duration, complete/stopped. **Never** the file path (it contains the chat title), titles, names or text.
- No size cap and no text budget in the file. The tool's **returned text** still obeys `settings.max_chars`.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here

Maintainer's choices (2026-10-28): **no period = the whole history**; **compact layout** (one paragraph per message, bold time + sender). The rest are Claude's calls.

- **Arguments:** `export_chat(chat, days=None, since=None, until=None, format="md")`. `format` other than `"md"` (after `strip().casefold()`) → ToolError `Only format="md" is supported.`. Period = `timerange.parse_period` unchanged; `Period(None, None)` means the whole history, `Period(None, end)` everything before `end`.
- **Streaming:** `MessageQuery(direction="forward", start=period.start, end=period.end, boundary_id=<last id or None>, topic_id=None, from_me=False, limit=EXPORT_PAGE)`, `EXPORT_PAGE = 500`. A page shorter than `EXPORT_PAGE` ends the export (forward live reads skip only messages before `start`, which never count against `limit`). Telethon's `reverse=True` with no offset starts at the oldest message, so `start=None` works as is.
- **Pacing (maintainer, 2026-10-28):** keep Telethon's default `wait_time` (1 s between 100-message batches, because `get_messages` passes `limit=None`). Slower (~1 s per 100 messages, ~17 min for 100k) but safer against Telegram limits. Hence the smoke item on LM Studio's tool timeout.
- **FloodWait:** Telethon sleeps through ≤30 s itself. A `RateLimitedError` from a page with `seconds ≤ MAX_FLOOD_WAIT = 300` → `await sleep(seconds + 1)` and retry the same page, at most `MAX_FLOOD_RETRIES = 3` times per export. Longer waits or retries used up → stop, keep the partial file (see footer), report it.
- **Errors mid-export:** any other `TgBridgeError` → stop like a long FloodWait (`stopped = str(e)`). Anything else (incl. `CancelledError`) → write the INCOMPLETE footer, close the file, re-raise.
- **Empty period:** no file is created (open the file only once the first page has messages).
- **Filename:** `{slug}_{chat.id}_{from}_{to}_{stamp}.md`
  - `slug` = `resolve.normalize(chat.title).replace(" ", "-")[:40].strip("-")` or `chat` when empty (Cyrillic stays, emoji drop).
  - `from` = local ISO date of `period.start`, else of the first exported message. `to` = local ISO date of `period.end - 1 µs`, else the local export date.
  - `stamp` = local export time `%Y%m%d-%H%M%S`.
  - On `FileExistsError` try `…_2.md` … `…_99.md`, then raise.
- **File layout** (`tz` = settings timezone, times local):
  ```markdown
  # Anna K

  - Chat: user · id 123 · @anna_k · archived
  - Peer: Anna K · first name: Anna · last name: K · @anna_k · id 123 · phone: +48123456789 · bot · deleted
  - Members: 42
  - Description: Our team chat
  - Period: 2026-10-01 00:00 → 2026-11-01 00:00 (Europe/Warsaw)
  - Exported: 2026-10-28 14:03:12 (Europe/Warsaw) by tg-bridge
  - Links: https://t.me/c/123/<id>

  ## 2026-10-25 Sat

  **14:03 Anna (@anna_k)** #4812
  Привіт! Ось лог помилки:
  \# not a heading

  **14:05 Me** #4813 ↩#4812 ✎
  Дякую, гляну

  **14:07 Anna** #4814 ⤷fwd Bob
  [photo · 1.2MB · https://t.me/c/123/4814]

  ---
  Exported 3 messages · 2026-10-25 14:03 → 2026-10-25 14:07 (Europe/Warsaw) · complete
  ```
  - Header lines appear only when there is data: `Chat:` flags = kind, `id`, `@username`, `forum`, `archived`. `Peer:` only for 1:1 (`display_name`, then each non-empty raw field of D9; `bot`/`deleted` flags). `Members:` only without a peer and when known. `Description:` via `format.one_line`. `Links:` via `format.chat_link_base`.
  - `Period:` text: `whole history`, `everything before <end>`, `<start> → <end or now>` (minutes, local).
  - Message first line: `**HH:MM <sender>**` + ` #id` + (` topic #N` when `chat.is_forum and topic_id`) + (` ↩#r`) + (` ⤷fwd <name>`) + (` ✎` when edited). Sender as in `format._sender` (`Me`, display name + `(@username)` the first time that person appears **in the file**, `sender_title`, `?`), with `\ * _ [ ] ` ` escaped.
  - Body lines: `[action]` for service messages; else the full text (no truncation), `\r\n`/`\r` → `\n`, control chars except `\n`/`\t` removed, each line block-escaped (below); then the media line.
  - **Block escape:** a text line whose `lstrip()` starts with `#`, `>`, `-`, `+`, `*`, `=`, `|`, ```` ``` ````, `~~~`, or matches `\d+[.)](\s|$)` gets a `\` inserted before its first non-space char. Inline markup is left alone.
  - **Media line:** `[` + ` · `.join(kind, label (sticker/poll), file_name, duration, size, link) + `]`, skipping empty parts; sizes/durations via `format`'s helpers; link = `{chat_link_base}/{id}` when the chat has one.
  - **Footer** after `---`: `Exported N message(s) · <first> → <last> (<tz>) · complete`, or `… · INCOMPLETE: <reason>` with reason = the `stopped` text, or `interrupted` for the re-raise case.
- **Tool result** (all lines `format.one_line`-safe, final text clamped to `max_chars`):
  - Done: `Exported 1234 messages from Anna K (123) to:\n<path>\nRange: 2026-10-01 09:12 → 2026-10-31 22:40 (Europe/Warsaw). The file has the full text; it was not read into this conversation.`
  - Stopped: same first two lines, then `Stopped early: <reason>. The file ends at <last local>. To get the rest later, call export_chat(chat="<id>", since="<last + 1 s, local ISO YYYY-MM-DDTHH:MM:SS>"[, until=…]).` (`until` repeated as the original local ISO when the period had an end.)
  - Empty: `No messages from Anna K (123) in this period (<period text>); no file written.`

## Review Focus

1. **Empty period / empty chat** → no file, the "no file written" text (Task 2, Task 3 tests).
2. **Same export twice in the same second** → a second file `…_2.md`, the first untouched (Task 2).
3. **Message text with Markdown markers, CRLF, control chars, blank lines** → appears literally, no stray headings/quotes (Task 1).
4. **Message count an exact multiple of the page size** → no duplicate or missing message, one extra empty fetch, then done (Task 2, page size patched to 2).
5. **Long FloodWait mid-export** → a valid partial file ending with an INCOMPLETE footer, and a `since=` hint that starts right after the last exported message (Task 2, Task 3).

---

### Task 1: Markdown renderers

**Files:**
- Create: `tg_bridge/export.py`
- Modify: `tg_bridge/format.py` (rename `_size` → `size_text`, `_duration` → `duration_text`; update callers)
- Test: `tests/test_export_render.py`

**Interfaces:**
- Produces (all pure, in `tg_bridge/export.py`):
  - `export_filename(chat: Chat, period: Period, first: datetime, tz: ZoneInfo, now: datetime, n: int = 1) -> str` (`n > 1` → `_{n}` suffix)
  - `period_text(period: Period, tz: ZoneInfo) -> str`
  - `render_header(info: ChatInfo, period: Period, tz: ZoneInfo, now: datetime) -> str` (ends with a blank line)
  - `render_messages(messages: Sequence[Message], chat: Chat, tz: ZoneInfo, state: RenderState) -> str` — day headings + paragraphs; `RenderState` (mutable dataclass: `day: date | None`, `seen_people: set[int]`) carries day/sender memory across pages
  - `render_footer(count: int, first: datetime | None, last: datetime | None, tz: ZoneInfo, stopped: str | None) -> str`
  - `escape_block(line: str) -> str`

- [ ] **Step 1: Write the failing tests** (expected strings literal in the test, as snapshots):
  - `test_full_snapshot`: a 1:1 chat with a peer having all D9 fields + `deleted`, three messages over two local days, incl. a reply, an edit, a forward, a photo in a supergroup-linked chat (use a second chat for the link case if simpler) → `render_header + render_messages + render_footer` equals the layout above.
  - `test_group_header_has_members_and_no_peer_line`; `test_header_skips_empty_fields`.
  - `test_period_text`: `Period(None, None)` → `whole history`; `Period(None, end)` → `everything before 2026-10-01 00:00`; start only → `… → now`.
  - `test_escape_block` parametrized: `"# x"`, `"  > q"`, `"- a"`, `"1. a"`, `"2) a"`, `"```py"`, `"|a|"`, `"-"` get `\` before the first non-space char; `"10 apples"`, `"a # b"`, `"plain"` unchanged.
  - `test_text_is_full_and_normalised`: 5 000-char text appears whole; `"a\r\nb\x07c"` → lines `a` / `bc`.
  - `test_render_state_spans_pages`: two calls with messages of the same day → one day heading; `(@username)` only on the first appearance.
  - `test_forum_topic_marker`; `test_service_message_and_media_label` (sticker label, poll question).
  - `test_footer_incomplete`: `stopped="Telegram rate limit: retry in 900 s"` → `· INCOMPLETE: Telegram rate limit: retry in 900 s`.
  - `test_export_filename`: title `"Аня / Work 🚀"` id `-100123`, period `2026-10-01..2026-10-31` (inclusive `until`) → `аня-work_-100123_2026-10-01_2026-10-31_20261028-140312.md`; empty title → `chat_…`; no period → first message date and export date; `n=2` → `…_2.md`.
- [ ] **Step 2:** Run `uv run pytest tests/test_export_render.py -q`; expect FAIL (module missing).
- [ ] **Step 3:** Implement per "Decisions → File layout". Reuse `format.one_line`, `chat_link_base`, `WEEKDAYS`, `size_text`, `duration_text`, `resolve.normalize`.
- [ ] **Step 4:** Run `uv run pytest -q`; expect all PASS.
- [ ] **Step 5:** Commit `feat(export): Markdown renderers`.

### Task 2: Streaming exporter

**Files:**
- Modify: `tg_bridge/export.py`
- Test: `tests/test_export_file.py`

**Interfaces:**
- Consumes: Task 1 renderers; `MessageSource.get_messages`; `RateLimitedError.seconds`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ExportResult:
      path: Path | None        # None: no messages, no file
      count: int
      first: datetime | None   # aware UTC
      last: datetime | None
      stopped: str | None      # None = complete

  async def export_chat_file(
      source: MessageSource, info: ChatInfo, period: Period, dest_dir: Path, tz: ZoneInfo, now: datetime,
      *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
  ) -> ExportResult
  ```
  Constants `EXPORT_PAGE = 500`, `MAX_FLOOD_WAIT = 300`, `MAX_FLOOD_RETRIES = 3` (module level, so tests monkeypatch `EXPORT_PAGE`).

- [ ] **Step 1: Write the failing tests** (FakeSource, `tmp_path`):
  - `test_exports_period_to_file`: file exists under `dest_dir`, content = header + messages of the period only + complete footer; `count/first/last` right.
  - `test_creates_dir_and_file_mode_600`: `dest_dir` missing beforehand; `stat().st_mode & 0o777 == 0o600`.
  - `test_pages_without_gaps_or_duplicates`: `EXPORT_PAGE = 2`, messages with ids 1–4 → all 4 once; `source.message_queries` boundaries `None, 2, 4`; 5 messages → `None, 2, 4`.
  - `test_whole_history_when_no_period`: `Period(None, None)` → first query has `start=None, direction="forward"`.
  - `test_empty_period_writes_nothing`: `path is None`, `dest_dir` has no files.
  - `test_never_overwrites`: two exports with the same `now` → two files, `_2` suffix, first content unchanged.
  - `test_short_flood_wait_retried`: a FakeSource subclass raising `RateLimitedError(40)` once on page 2 → fake `sleep` called with `41`, full file, `stopped is None`.
  - `test_long_flood_wait_stops_with_partial_file`: `RateLimitedError(900)` on page 2 → `stopped == "Telegram rate limit: retry in 900 s"`, file has page 1 and the INCOMPLETE footer, `last` = last message of page 1.
  - `test_unexpected_error_writes_footer_and_reraises`: `RuntimeError` on page 2 → re-raised; file ends with `INCOMPLETE: interrupted`.
  - `test_logs_no_title_or_path`: `caplog` has no chat title and no `dest_dir` path text.
- [ ] **Step 2:** Run `uv run pytest tests/test_export_file.py -q`; expect FAIL.
- [ ] **Step 3:** Implement: fetch page → (first non-empty page) pick a free filename and open it → write header → per page write `render_messages` and `flush()` → footer in a `finally` that knows the outcome. Log one INFO line at the end.
- [ ] **Step 4:** Run `uv run pytest -q`; expect all PASS.
- [ ] **Step 5:** Commit `feat(export): stream a chat into a Markdown file`.

### Task 3: `export_chat` tool

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`, `docs/features/04-export.md` (smoke checklist)
- Test: `tests/test_tools_export.py`, `tests/test_server.py` (tool listed)

**Interfaces:**
- Consumes: `export_chat_file`, `ExportResult`, `period_text` (Task 1/2); `resolve_chat_arg`, `parse_period`, `_tool_errors`.
- Produces: `async def export_chat(source, settings, chat: str, *, days=None, since=None, until=None, format: str = "md", now=lambda: datetime.now(UTC), sleep=asyncio.sleep) -> str` in `tools.py`; dest dir = `settings.export_dir`.

- [ ] **Step 1: Write the failing tests:** result texts exactly as in "Decisions → Tool result" for done / stopped (incl. the `since=` value = last message + 1 s in local time, and `until` repeated when given) / empty; `format="pdf"` → ToolError text; ambiguous chat → candidate list (same path as `get_messages`); `days`+`since` → PeriodError text; server lists `export_chat` with `READ_ONLY` annotations.
- [ ] **Step 2:** Run the two test files; expect FAIL.
- [ ] **Step 3:** Implement the tool and register it in `server.py` (`structured_output=False`). Docstring:
  ```
  Save a chat (or a period of it) to a Markdown file and return only the file path, message count and date range,
  NOT the messages. Use it when the user wants a file, a full copy, or a whole long conversation for another
  tool or model. Without a period it exports the whole history. `chat`: id, @username or title words.
  Period: `days` OR `since`/`until`, as in get_messages. Large chats can take minutes.
  ```
- [ ] **Step 4:** Run `uv run pytest -q`; expect all PASS.
- [ ] **Step 5:** Add to the brief's smoke checklist: `[ ] Export of the largest chat finishes within LM Studio's tool timeout (note the duration and message count in the Log).` and `[ ] A forum/supergroup export has working t.me links.`
- [ ] **Step 6:** Commit `feat: export_chat tool (feature 04)`.

## Addendum (2026-10-28, after the first smoke run)

- LM Studio ends a tool call after ~60 s (`MCP error -32001: Request timed out`); a whole-history export of a busy channel got ~5 500 messages in and was cut (`INCOMPLETE: interrupted`). Maintainer's choice: **one file, continued by cursor**.
- `EXPORT_TIME_BUDGET = 40` s per call: after a full page past that, the call pauses (footer `INCOMPLETE: paused, to be continued`) and returns `Next: call export_chat(chat="<id>", cursor="x…")`. The model repeats until the result has no `Next:`.
- Export cursor (`cursor.py`): `x{boundary}_f{file epoch}[_n{suffix}]_k{count}_a{first}_d{last}[_s][_e]_c{tag}`. The continuation finds the file by chat id + creation stamp, cuts the footer and appends; a missing or complete file → `ExportFileError`. A FloodWait stop also returns this cursor instead of a `since=` hint.
- Message `#id` and reply `↩#id` are t.me links where the chat has them; `export_chat` is annotated not read-only (it writes a local file).
