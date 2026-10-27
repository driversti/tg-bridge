# 02 — Reading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_messages` reads any dialog chronologically for a period (or its latest messages), as compact day-grouped lines that never exceed `TG_BRIDGE_MAX_CHARS`, with an opaque `next_cursor` the model passes back to keep paging.

**Architecture:** Four pure modules do the thinking and are unit-tested without Telegram: `timerange` (args → `[start, end)` UTC), `cursor` (opaque paging token), `format` (message lines, day headers, budget fitting) and the tool body in `tools`. Telegram access is one new read method `ReadOnlyTelegram.get_messages(chat, query)` that returns at most `query.limit` domain `Message`s in iteration order; `LiveSource` and `FakeSource` pass it through. Paging is **id-based**, so messages arriving between pages are never duplicated or skipped.

**Tech Stack:** unchanged (Python ≥3.12, `telethon` 1.45, `mcp` v2 `MCPServer`, `pytest-asyncio`, stdlib `zoneinfo`/`base64`/`json`).

**Spec:** `docs/features/02-reading.md` + `docs/design.md` §6 (models), §7 (period args, compact line, budget, errors), §8 (no read receipts, logging), §9, §11 (`from_me`, `topic`, plain text).

## Global Constraints

All constraints from `docs/plans/00-foundation.md` and `docs/plans/01-chats.md` still apply. Additionally:

- **Telegram calls allowed in this feature:** `client.iter_messages(...)` only (it uses `messages.GetHistory`, `messages.Search` with `from_user`, `messages.GetReplies` with `reply_to`; none of them mark anything read). Never `send_read_acknowledge`, `messages.ReadHistory`, `channels.ReadHistory`, `GetMessagesViews` (it increments view counters).
- Logs: chat id, direction, message count, duration. Never message text, sender names, topic titles, the cursor string or the user's date arguments.
- Period maths uses `settings.timezone` (default `Europe/Warsaw`). The model never does date arithmetic.
- Output budget: the **whole** tool result (header + lines + footer) is `≤ settings.max_chars` (default `12000`), always.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here

- **Two reading modes.**
  - **Period mode** (`days`, or `since` given): `direction="forward"`, oldest → newest from `start`; the cursor continues forward. Summaries need the beginning first.
  - **Latest mode** (no `days`/`since`; optional `until`): `direction="backward"`, newest first from `until`/now; the page is shown oldest → newest; the cursor continues to **older** messages.
- **Period arguments** (`timerange.parse_period`):
  - `days=N`, `1 ≤ N ≤ 3650`: start = local 00:00 of `today − (N−1)` (date arithmetic on the local date, never `timedelta` on aware datetimes), end = `None` (= now, open).
  - `since`/`until`: `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM[:SS]` (space instead of `T` allowed), optional offset (`+02:00`, `Z`), or the keywords `today`/`yesterday` (case-insensitive; cheap, and saves small models from knowing the date). No offset = local zone. Date-only/keyword `since` = that day 00:00; date-only/keyword `until` = **next** day 00:00 (inclusive day); datetime `until` is exclusive as given.
  - Errors (`PeriodError`, readable, quoting nothing but the bad value's role): `days` together with `since`/`until`; `days` out of range; unparsable `since`/`until`; `start ≥ end`. A `since` in the future is not an error (empty result).
- **Cursor** = `base64url(compact JSON)` without padding, version field `v=1`, carrying `chat_id, direction, boundary_id, start, end, topic_id, from_me` (datetimes as integer UTC epoch seconds). When `cursor` is given, the period/`topic`/`from_me` arguments are **ignored** (models re-send them), `limit` is honoured, and the resolved chat must have the cursor's `chat_id`, else ToolError. Garbage, wrong version or wrong shape → ToolError `Invalid cursor. Call get_messages again without cursor.`
- **Paging boundary:** forward → ids `> boundary_id` (last shown id); backward → ids `< boundary_id` (oldest shown id). `end=None` stays open on later pages, so a busy chat may keep growing: accepted.
- **Fetch size:** `limit` default 100, clamped `1..300`. The tool asks the source for `limit + 1` to know whether more exist; the budget then decides how many of those are shown.
- **Line format** (maintainer chose "day headers"):
  ```
  Chat: Dev Talk (-1001234) · 2026-10-26 00:00 → now (Europe/Warsaw)
  Topic: #12 Releases · from me only
  Links: https://t.me/c/1234/<id>
  — 2026-10-26 Mon —
  #4807 09:12 Anna (@anna_k): хто дивився реліз?
  #4812 14:03 Bohdan: я ⏎ там зламали API [photo 1.2MB] ↩#4807
  #4813 14:05 Anna: ⤷fwd Rust News: 1.80 released ✎
  #4815 14:10 Olena: [joined]
  — 2026-10-27 Tue —
  #4820 08:40 Me: доброго ранку
  Shown 5 messages (2026-10-26 09:12 → 2026-10-27 08:40). More: call get_messages(chat="-1001234", cursor="eyJ2…")
  ```
  - Line: `#{id} {HH:MM} {sender}: {⤷fwd {origin}: }{text}{ [media]}{ ↩#{reply_to_id}}{ ✎}`; parts that are absent are omitted with their separators.
  - Day header `— YYYY-MM-DD Ddd —` (English weekday abbreviation, local zone) before the first message of each local day on the page.
  - Sender: `Me` when `message.out`; else `Person.display_name`, with ` (@username)` appended only at that person's **first** line on the page; else `sender_title` (channel / anonymous admin / post author); else `?`.
  - Text: `\n` → ` ⏎ `; cap at 1500 chars per message → `text[:1500] + "…(+N chars)"`.
  - Media summary: `[photo 1.2MB]`, `[video 0:42 8.1MB]`, `[voice 0:12]`, `[video_note 0:08]`, `[audio name.mp3 3.4MB]`, `[document report.pdf 340KB]`, `[gif]`, `[sticker 😀]`, `[poll: question]`, `[location]`, `[contact]`, `[media]` (other). Sizes: `<1024` → `NB`, `<1 MiB` → `NKB` (integer), else `N.NMB`. Duration `M:SS`.
  - Service messages get a short line: `[joined]`, `[joined via link]`, `[left]`, `[added N members]`, `[removed a member]`, `[pinned #4807]`, `[renamed to "T"]`, `[changed photo]`, `[created chat]`, `[created topic "T"]`, `[voice chat]`, otherwise `[<action class name without "MessageAction", snake_case>]`.
  - `Links:` line only when a link exists: public chat (`chat.username`, kind `supergroup`/`channel`) → `https://t.me/{username}/<id>`; private supergroup/channel → `https://t.me/c/{internal}/<id>` where `internal = -chat.id - 10**12` (i.e. `-100N` → `N`). Users, bots, basic groups: no line. This replaces design §6 `Message.link` (no per-message field): note it in the roadmap Log at finish.
  - Footer: `Shown N messages (first → last).` then `More: call get_messages(chat="<id>", cursor="<c>")` in forward mode, `Older: call get_messages(chat="<id>", cursor="<c>")` in backward mode, or `End of period.` / `No older messages.` when nothing remains. Empty page: `No messages in this period.` (or `No messages.` in latest mode), no cursor.
- **Budget fitting** (`format.render_page`): forward keeps the **oldest** messages that fit, backward keeps the **newest**. Fitting is exact (final text `≤ max_chars`), re-rendering candidate subsets is fine (n ≤ 301). If not even one message fits, show one message with its text cut to fit (ends with `…`). `FOOTER_RESERVE = 300` chars are kept free for the footer; a test asserts a real footer with a 10-digit id and a full cursor stays under it.
- **FloodWait:** construct the client with `flood_sleep_threshold=30`, so Telethon itself sleeps on waits ≤ 30 s and raises above; the existing `_call` turns that into `RateLimitedError` ("Telegram rate limit: retry in N s"). The whole fetch runs inside one `_call`, so a wait in the middle of paging is caught too.
- **Telethon specifics (verified in 1.45 source):** with `reverse=True`, `offset_id` means "ids greater than"; `offset_id` takes priority over `offset_date`. `reply_to` (topic) takes priority over `from_user` and silently drops it, so **topic + from_me filters `msg.out` client-side**. Iterate with `limit=None` and stop manually (end date, client-side filter, `query.limit`).
- **`topic` argument:** numeric (int or digit string) → used as the topic id as given; otherwise a title fragment resolved against `get_chat_info(...).topics` with `resolve.normalize` + all-tokens-contained matching (same rule as chat titles: exact normalised title wins, several → ToolError listing `#id title` candidates, none → ToolError listing all topics). `topic` on a non-forum chat → ToolError `"<title>" is not a forum; omit topic.`

## Review Focus

1. **Empty period / `since` in the future / chat with no history** → `No messages in this period.`, no cursor, no crash. *(Task 5 `test_empty_page`, Task 6 `test_get_messages_empty_period`)*
2. **Model mangles or truncates the cursor, or uses it with another chat** → one clear ToolError telling it to start again, never a stack trace. *(Task 3 `test_decode_rejects_*`, Task 6 `test_cursor_for_other_chat_rejected`)*
3. **One huge message or a tiny `TG_BRIDGE_MAX_CHARS` (e.g. 300)** → result still `≤ max_chars` and still shows ≥ 1 message. *(Task 5 `test_single_huge_message_is_cut_to_fit`)*
4. **New messages arrive between pages of a `days=N` read** → page 2 starts right after page 1's last id, no duplicates. *(Task 6 `test_forward_paging_round_trip`)*
5. **Model sends `days=2` again together with the cursor** → cursor wins, no "days with cursor" error. *(Task 6 `test_period_args_ignored_with_cursor`)*

---

### Task 1: `timerange`

**Files:**
- Create: `tg_bridge/timerange.py`
- Modify: `tg_bridge/errors.py` (add `PeriodError(TgBridgeError)`)
- Test: `tests/test_timerange.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Period:
      start: datetime | None  # aware UTC, inclusive; None = from the latest backwards
      end: datetime | None    # aware UTC, exclusive; None = now (open)

  def parse_period(days: int | None, since: str | None, until: str | None,
                   tz: ZoneInfo, now: datetime) -> Period
  def describe_period(period: Period, tz: ZoneInfo) -> str
      # "2026-10-26 00:00 → now", "2026-10-20 00:00 → 2026-10-22 00:00", "latest messages", "latest before 2026-10-22 00:00"
  ```

- [ ] **Step 1: Write the failing tests** (`W = ZoneInfo("Europe/Warsaw")`, `now` aware)
  - `test_days_1_is_today_midnight`: now `2026-10-27 15:00 W` → `start == 2026-10-26T23:00Z`, `end is None`.
  - `test_days_2_is_yesterday_midnight`: same now → `start == 2026-10-25T23:00Z`.
  - `test_days_across_dst_start`: now `2026-03-29 12:00 W` → `days=1` start `2026-03-28T23:00Z`; `days=2` start `2026-03-27T23:00Z`.
  - `test_days_across_dst_end`: now `2026-10-25 12:00 W` → `days=1` start `2026-10-24T22:00Z`; `days=3` start `2026-10-22T22:00Z`.
  - `test_until_date_is_inclusive`: `until="2026-10-25"` → `end == 2026-10-25T23:00Z` (Oct 26 00:00 CET); `start is None`.
  - `test_since_until_dates`: `since="2026-10-20", until="2026-10-21"` → `2026-10-19T22:00Z`, `2026-10-21T22:00Z`.
  - `test_datetime_without_offset_is_local` (`"2026-10-20 14:30"` → `12:30Z`), `test_datetime_with_offset_respected` (`"2026-10-20T14:30+00:00"` → `14:30Z`), `test_z_suffix`.
  - `test_keywords_today_yesterday`: now `2026-10-27 15:00 W`, `since="yesterday", until="Yesterday"` → `2026-10-25T23:00Z`, `2026-10-26T23:00Z`.
  - `test_nothing_given_is_latest`: → `Period(None, None)`.
  - `test_days_with_since_rejected`, `test_days_zero_rejected`, `test_days_too_large_rejected` (3651), `test_garbage_since_rejected` (message contains `since`), `test_start_not_before_end_rejected` — all `PeriodError`.
  - `test_future_since_is_allowed`.
  - `test_describe_period` for the four shapes in the interface comment.
- [ ] **Step 2: Run** `uv run pytest tests/test_timerange.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `parse_period` / `describe_period` in `tg_bridge/timerange.py`. Local midnight = `datetime.combine(d, time(0), tzinfo=tz).astimezone(UTC)` (Warsaw transitions are at 02:00/03:00, so midnight always exists). Parse with `datetime.fromisoformat` after keyword check; date-only = string length 10.
- [ ] **Step 4: Run** `uv run pytest tests/test_timerange.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(timerange): period arguments to UTC ranges in the local zone`

### Task 2: Message models and Telethon mapping

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/__init__.py` (export new names), `tg_bridge/telegram/mapping.py`
- Test: `tests/test_models.py`, `tests/telegram/test_mapping.py`

**Interfaces:**
- Produces (in `tg_bridge.source`):
  ```python
  MediaKind = Literal["photo", "video", "gif", "voice", "audio", "video_note", "sticker",
                      "document", "poll", "location", "contact", "other"]

  @dataclass(frozen=True)
  class MediaInfo:
      kind: MediaKind
      file_name: str | None
      size: int | None        # bytes
      mime_type: str | None
      duration: int | None    # seconds
      label: str | None       # sticker emoji, poll question

  @dataclass(frozen=True)
  class Message:
      id: int
      chat_id: int
      date: datetime              # aware UTC
      edit_date: datetime | None  # aware UTC
      out: bool                   # sent by the logged-in account
      sender: Person | None       # user senders
      sender_title: str | None    # channel / anonymous admin title, or channel post_author
      text: str                   # "" when none
      reply_to_id: int | None     # None when it only points at the forum topic root
      forward_from: str | None    # display name of the original sender/chat, "hidden" if unknown
      topic_id: int | None
      media: MediaInfo | None
      action: str | None          # short service-message label, e.g. "joined", "pinned #4807"

  @dataclass(frozen=True)
  class MessageQuery:
      direction: Literal["forward", "backward"]
      start: datetime | None
      end: datetime | None
      boundary_id: int | None     # forward: ids > it; backward: ids < it
      topic_id: int | None
      from_me: bool
      limit: int
  ```
- Produces (in `tg_bridge.telegram.mapping`): `message_from_tl(msg, chat_id: int) -> Message | None` (None for `MessageEmpty`).

- [ ] **Step 1: Write the failing tests** in `tests/telegram/test_mapping.py`, building real `telethon.tl.types` objects (`Message`, `MessageService`, `PeerUser`, `MessageReplyHeader`, `MessageFwdHeader`, `MessageMediaPhoto`/`MessageMediaDocument` with `DocumentAttribute*`, `MessageMediaPoll`, `MessageActionChatAddUser`, `MessageActionPinMessage`, `MessageActionChatEditTitle`) and attaching a sender via `msg._sender` / `_finish_init` the way `test_client.py` already fakes entities:
  - `test_plain_text_message`: id, UTC date (naive input → `tzinfo=UTC`), `out`, text, sender person.
  - `test_edit_date_mapped`, `test_empty_text_is_empty_string`.
  - `test_reply_to_id` and `test_reply_to_topic_root_is_not_a_reply` (`forum_topic=True`, no `reply_to_top_id` → `topic_id == reply_to_msg_id`, `reply_to_id is None`); `test_reply_inside_topic` (`reply_to_top_id=12, reply_to_msg_id=40` → `topic_id 12`, `reply_to_id 40`).
  - `test_forward_from_name` (`fwd_from.from_name`), `test_forward_hidden` (no resolvable origin → `"hidden"`).
  - `test_media_kinds`: photo → `photo` with size of the largest size; voice doc (`DocumentAttributeAudio(voice=True, duration=12)`) → `voice, duration 12`; round video → `video_note`; animated → `gif`; sticker → `sticker` with `label` = alt emoji; pdf document → `document`, `file_name`, `size`, `mime_type`; poll → `poll` with `label` = question text.
  - `test_channel_post_sender_title`: sender is a `Channel` → `sender is None`, `sender_title` = channel title; `post_author` set → `sender_title` = post_author.
  - `test_service_actions`: add-user where added id == sender id → `"joined"`, add two others → `"added 2 members"`, pin with `reply_to_msg_id=4807` → `"pinned #4807"`, edit title → `'renamed to "T"'`, unknown action class → snake_case name (e.g. `MessageActionScreenshotTaken` → `"screenshot_taken"`).
  - `test_message_empty_is_none`.
- [ ] **Step 2: Run** `uv run pytest tests/telegram/test_mapping.py -q` → FAIL.
- [ ] **Step 3: Implement** the dataclasses in `models.py` and `message_from_tl` in `mapping.py`. Use Telethon's convenience properties for media in this order: `gif`, `video_note`, `voice`, `audio`, `sticker`, `video`, `photo`, `poll`, `geo`/`venue` → `location`, `contact`, `document`, else `other`; size/name/mime/duration from `msg.file`. Ignore `web_preview` (not media). Forward origin: `fwd_from.from_name`, else `forward.chat.title`, else `forward.sender` display name, else `"hidden"`.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS (whole suite: `Chat`/`Person` untouched).
- [ ] **Step 5: Commit** `feat(source): Message model and Telethon message mapping`

### Task 3: Cursor

**Files:**
- Create: `tg_bridge/cursor.py`
- Modify: `tg_bridge/errors.py` (add `CursorError(TgBridgeError)` with message `Invalid cursor. Call get_messages again without cursor.`)
- Test: `tests/test_cursor.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class Cursor:
      chat_id: int
      direction: Literal["forward", "backward"]
      boundary_id: int
      start: datetime | None
      end: datetime | None
      topic_id: int | None
      from_me: bool

  def encode_cursor(c: Cursor) -> str
  def decode_cursor(s: str) -> Cursor   # raises CursorError
  ```

- [ ] **Step 1: Write the failing tests**
  - `test_round_trip` (all fields set; datetimes compare equal, aware UTC), `test_round_trip_nones`.
  - `test_is_url_safe_ascii_without_padding` (`re.fullmatch(r"[A-Za-z0-9_-]+", s)`), `test_length_under_200` for `chat_id=-1001234567890, boundary_id=2**31`, both dates set.
  - `test_decode_rejects_garbage` (`"hello!"`), `test_decode_rejects_truncated` (`s[:-5]`), `test_decode_rejects_wrong_version`, `test_decode_rejects_bad_direction`, `test_decode_rejects_non_int_boundary`, `test_decode_strips_surrounding_whitespace_and_quotes` (`' "abc" '` style input from a model is accepted).
- [ ] **Step 2: Run** `uv run pytest tests/test_cursor.py -q` → FAIL.
- [ ] **Step 3: Implement** with `json.dumps(..., separators=(",", ":"))`, keys `v,c,d,b,s,e,t,m`, `base64.urlsafe_b64encode(...).rstrip(b"=")`; decode re-pads and validates every field's type; any exception → `CursorError`.
- [ ] **Step 4: Run** `uv run pytest tests/test_cursor.py -q` → PASS.
- [ ] **Step 5: Commit** `feat(cursor): opaque base64url paging cursor`

### Task 4: Read messages from Telegram

**Files:**
- Modify: `tg_bridge/telegram/client.py`, `tg_bridge/source/protocol.py`, `tg_bridge/source/live.py`, `tests/fakes.py`
- Test: `tests/telegram/test_client.py`, `tests/test_live_source.py`

**Interfaces:**
- Consumes: `Message`, `MessageQuery`, `message_from_tl` (Task 2).
- Produces:
  - `ReadOnlyTelegram.get_messages(chat: Chat, query: MessageQuery) -> list[Message]` — at most `query.limit` messages in iteration order (forward = ascending ids, backward = descending).
  - `MessageSource.get_messages(chat: Chat, query: MessageQuery) -> list[Message]` (protocol, `LiveSource` = ensure connected + delegate).
  - `FakeSource(messages: dict[int, list[Message]] | None = None)`: stores per-chat messages; `get_messages` applies direction, `start`/`end`, `boundary_id`, `topic_id`, `from_me` (= `out`) and `limit` in plain Python, and records each query in `self.message_queries`. Tool tests in Task 6 rely on it behaving like Telegram.

- [ ] **Step 1: Write the failing tests** (fake Telethon client in `test_client.py`, recording `iter_messages` kwargs and yielding prepared tl messages):
  - `test_client_built_with_flood_threshold_30`: factory kwargs contain `flood_sleep_threshold=30`.
  - `test_forward_query_kwargs`: `start` set, no boundary → `reverse=True, offset_date=start, offset_id=0, limit=None`; with boundary 50 → `offset_id=50`.
  - `test_backward_query_kwargs`: `end` set → `reverse=False, offset_date=end`; boundary → `offset_id`.
  - `test_from_me_uses_from_user_me` (no topic) and `test_topic_uses_reply_to_and_filters_out_client_side` (topic 12 + from_me → `reply_to=12`, no `from_user`, only `out=True` messages returned).
  - `test_forward_stops_at_end`, `test_backward_stops_before_start`, `test_skips_messages_before_start_in_forward` (safety filter), `test_stops_at_limit`, `test_message_empty_skipped`.
  - `test_flood_mid_iteration_becomes_rate_limited`: the async generator raises `FloodWaitError(seconds=45)` after two messages → `RateLimitedError` with `seconds == 45`.
  - `test_get_messages_cold_entity_cache` reuses the existing `_input_entity` fallback (first `get_input_entity` raises `ValueError`).
  - `test_no_read_acknowledge`: after a `get_messages` call the fake client saw no attribute access to `send_read_acknowledge` (fake raises if touched).
  - In `test_live_source.py`: `test_get_messages_connects_and_delegates`.
- [ ] **Step 2: Run** `uv run pytest tests/telegram/test_client.py tests/test_live_source.py -q` → FAIL.
- [ ] **Step 3: Implement** `get_messages` in `ReadOnlyTelegram`: resolve entity via `_input_entity`, build kwargs per the decisions above, collect inside one `_call`, map with `message_from_tl(msg, chat.id)`, apply end/start/out filters and the limit while iterating; `log.info("read %d messages from %d (%s) in %.2f s", ...)`. Add `flood_sleep_threshold=30` to the factory call. Then protocol, `LiveSource`, `FakeSource`.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(telegram): read-only message iteration with period, topic and from_me`

### Task 5: Message rendering and budget

**Files:**
- Modify: `tg_bridge/format.py`
- Test: `tests/test_format_messages.py` (new; `tests/test_format.py` stays for chats)

**Interfaces:**
- Consumes: `Message`, `MediaInfo`, `Chat`, `Topic`, `describe_period` (Task 1).
- Produces:
  ```python
  FOOTER_RESERVE = 300

  def chat_link_base(chat: Chat) -> str | None
  def messages_head(chat: Chat, period_text: str, tz: ZoneInfo,
                    topic: Topic | None = None, from_me: bool = False) -> str
  def render_page(head: str, messages: Sequence[Message], *, tz: ZoneInfo, max_chars: int,
                  keep: Literal["oldest", "newest"], more: bool,
                  next_hint: Callable[[Message], str], end_text: str, empty_text: str) -> str
      # `messages` are chronological; `next_hint(boundary)` builds the "More:/Older: …" sentence,
      # boundary = last shown (keep="oldest") or first shown (keep="newest");
      # called when `more` or when the budget dropped messages.
  ```
  (`Topic` with only `id` known is passed as `Topic(id, title="", is_closed=False)` and rendered `Topic: #12`.)

- [ ] **Step 1: Write the failing tests** (tz Warsaw, messages built with a local `make_message(**overrides)` helper added to `tests/fakes.py`):
  - `test_line_shape`: reproduces the exact sample from "Decisions" (head, two day headers, reply, forward, edit mark, `[joined]`, `Me`, photo `1.2MB`) given `more=True` and a `next_hint` returning `More: call get_messages(chat="-1001234", cursor="X")`.
  - `test_username_only_on_first_appearance` (two lines from Anna → `(@anna_k)` once).
  - `test_newlines_become_return_symbol`, `test_long_text_capped_at_1500` (ends `…(+500 chars)` for a 2000-char text).
  - `test_media_summaries`: each example from Decisions, incl. `340KB`, `0:42`, `[sticker 😀]`, `[poll: Q]`, `[media]`.
  - `test_sender_fallbacks`: `sender_title` used; neither → `?`.
  - `test_link_base`: public supergroup → `https://t.me/devtalk/<id>`; private `-1001234` → `https://t.me/c/1234/<id>`; user/basic group → `None` and no `Links:` line.
  - `test_budget_keeps_oldest` and `test_budget_keeps_newest`: 50 messages, `max_chars=1500` → `len(text) <= 1500`, footer present, the boundary passed to `next_hint` is the last/first shown message, shown ids are a prefix/suffix.
  - `test_single_huge_message_is_cut_to_fit`: `max_chars=300`, one 1400-char message → `len(text) <= 300`, the line ends with `…`.
  - `test_end_text_when_all_fit_and_no_more`, `test_empty_page` (`empty_text`, no hint call).
  - `test_footer_reserve_is_enough`: a real footer with `chat="-1001234567890"` and a cursor from `encode_cursor` (Task 3) with 10-digit ids and both dates fits in `FOOTER_RESERVE`.
  - `test_day_header_uses_local_date`: message at `2026-10-25T22:30Z` (23:30 CET) → header `— 2026-10-25 Sun —`; message at `2026-10-25T23:30Z` → `— 2026-10-26 Mon —`.
- [ ] **Step 2: Run** `uv run pytest tests/test_format_messages.py -q` → FAIL.
- [ ] **Step 3: Implement** the three functions plus private `_message_line`, `_media_text`, `_size`, `_duration` in `format.py`, reusing `_when`/`SEP`.
- [ ] **Step 4: Run** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `feat(format): day-grouped message lines with exact char budget`

### Task 6: `get_messages` tool

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: everything above; `resolve_chat_arg`, `resolve.normalize`, `source.get_chat_info`.
- Produces:
  ```python
  async def get_messages(source: MessageSource, settings: Settings, chat: str, *,
                         days: int | None = None, since: str | None = None, until: str | None = None,
                         topic: str | int | None = None, from_me: bool = False,
                         cursor: str | None = None, limit: int = 100,
                         now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> str
  ```
  Flow: resolve chat → (cursor ? decode + check chat id : parse period + resolve topic) → `MessageQuery(limit=limit+1)` → `source.get_messages` → `more = len > limit` → chronological order → `render_page(keep="oldest" if forward else "newest", next_hint=… encode_cursor(boundary) …)`. `PeriodError`/`CursorError`/`RateLimitedError` reach the model as ToolError text via `_tool_errors()`.

- [ ] **Step 1: Write the failing tests** (FakeSource with a supergroup chat and 250 messages over three local days; `now` fixed at `2026-10-27 15:00 W`):
  - `test_days_2_reads_forward_from_yesterday_midnight`: first shown message is the first one at/after `2026-10-25T23:00Z`, lines ascending, query `direction="forward"`, `limit=101`.
  - `test_forward_paging_round_trip`: page 1 → extract cursor from the `More:` line → page 2 starts at the next id; add a new message to FakeSource between calls → no duplicates, nothing skipped, finally `End of period.`
  - `test_latest_mode_pages_backward`: no period → newest messages shown oldest→newest, `Older:` cursor; page 2 contains only older ids; eventually `No older messages.`
  - `test_period_args_ignored_with_cursor` (`days=2` resent with the cursor → same page as without), `test_cursor_for_other_chat_rejected`, `test_invalid_cursor_message`.
  - `test_limit_clamped` (`0` → query limit 2, `1000` → 301).
  - `test_period_error_is_tool_error` (`days=2, since="today"`).
  - `test_get_messages_empty_period` (`since="2026-11-01"`).
  - `test_from_me_passed_through`.
  - `test_topic_numeric`, `test_topic_by_title_fragment`, `test_topic_ambiguous_lists_candidates`, `test_topic_unknown_lists_topics`, `test_topic_on_non_forum_rejected`.
  - `test_rate_limited_is_readable`: FakeSource raising `RateLimitedError(45)` → ToolError `Telegram rate limit: retry in 45 s`.
  - `test_output_never_exceeds_budget` over `max_chars` in `(300, 2000, 12000)` for both modes.
  - In `test_server.py`: `get_messages` is registered read-only with the parameters above.
- [ ] **Step 2: Run** `uv run pytest tests/test_tools.py tests/test_server.py -q` → FAIL.
- [ ] **Step 3: Implement** `tools.get_messages` (+ private `_resolve_topic`) and register it in `server.py`. Tool docstring (exact):
  ```
  Read one chat's messages in time order, as compact lines grouped by day.
  `chat`: numeric id, @username, or words from the title.
  Period: `days` (1 = today, 2 = since yesterday 00:00, …) OR `since`/`until` (YYYY-MM-DD, "YYYY-MM-DD HH:MM",
  "today", "yesterday"; `until` dates include that whole day). No period = the latest messages.
  `topic`: forum topic id or title words (see get_chat_info). `from_me`: only messages the user sent.
  If the result ends with "More:" or "Older:", call get_messages again with exactly that cursor to continue;
  keep the same chat, other arguments are taken from the cursor.
  ```
- [ ] **Step 4: Run** `uv run pytest -q` → PASS (whole suite green).
- [ ] **Step 5: Commit** `feat(tools): get_messages with periods, topics, budget and cursor paging`

### Task 7: Docs and smoke checklist

**Files:**
- Modify: `docs/features/02-reading.md`, `docs/roadmap.md`

- [ ] **Step 1:** Extend the brief's smoke checklist with:
  - `- [ ] Forum: topic by title and by id; topic #1 (General) works or gives a readable error.`
  - `- [ ] since="yesterday" until="yesterday" returns only yesterday.`
  - `- [ ] LM Studio shows the full ~12 000-char result (design §11; if it truncates, lower TG_BRIDGE_MAX_CHARS default).`
  - `- [ ] A FloodWait (if one happens) is reported as "retry in N s".`
- [ ] **Step 2:** In `docs/roadmap.md` add under Log: `- 02: chat link base shown once in the page header ("Links:") instead of a per-message Message.link; days shown under day headers.`
- [ ] **Step 3: Run** `uv run pytest -q` → PASS.
- [ ] **Step 4: Commit** `docs: feature 02 smoke checklist and log`
