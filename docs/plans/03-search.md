# 03 — Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `search_messages` finds messages by text in one chat or across the user's own dialogs, newest first, as compact budgeted lines with a copy-back cursor; `get_messages(chat, around=<id>)` then shows the conversation around any hit.

**Architecture:** One new source method `search_messages(query, chat | None) -> SearchPage` (per-chat = Telethon `iter_messages(search=…)` → `messages.Search`; global = raw `messages.SearchGlobal` requests, because resuming a global search needs `offset_rate/offset_peer/offset_id`, which `iter_messages` can't take). A separate `SearchCursor` in `cursor.py`, a hit renderer in `format.py`, a thin tool in `tools.py`. `around` reuses the existing `get_messages` source method twice (older side + newer side), no protocol change.

**Tech Stack:** unchanged (Python ≥3.12, `telethon` 1.45, `mcp` v2 `MCPServer`, `pytest-asyncio`, stdlib `zlib`).

**Spec:** `docs/features/03-search.md` + `docs/design.md` §7 (period args, compact line, budget, errors), §8 (read-only, logging), §9, §11 (keyword-driven scenario 1, `from_me`, global = own dialogs only).

## Global Constraints

All constraints from plans 00–02 still apply. Additionally:

- **Telegram calls allowed in this feature:** `client.iter_messages(entity, search=…)` (→ `messages.Search`) and `client(functions.messages.SearchGlobalRequest(...))`. Both are pure reads (no read receipts, no view counters). Never `SearchPublicPosts`, `contacts.Search` or anything that searches outside the user's dialogs (design §11).
- Logs: mode (`chat <id>` or `global`), hit count, scanned count, duration. **Never the query text**, message text, titles or the cursor.
- Output budget: the whole result `≤ settings.max_chars`, always (same rule as 02).
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here

Maintainer's choices (2026-10-28): **`around` in `get_messages`** for context, **one query per call**, **global results grouped by chat**. The rest are Claude's calls.

- **Direction:** search is always newest → oldest (Telegram's order). The cursor continues to **older** hits. No forward mode.
- **Arguments:** `search_messages(query, chat=None, days=None, since=None, until=None, from_me=False, cursor=None, limit=30)`. Period parsing = `timerange.parse_period` unchanged (no period = all time). `limit` clamped `1..100`. `query` is `strip()`ped. Empty → ToolError `query is empty: pass the words to search for.`. Longer than 256 chars → ToolError `query is too long (max 256 chars).`.
- **Source contract:**
  ```python
  @dataclass(frozen=True)
  class SearchOffset:          # "continue with hits older than this one"
      chat_id: int
      message_id: int
      date: datetime           # aware UTC

  @dataclass(frozen=True)
  class SearchQuery:
      text: str
      start: datetime | None
      end: datetime | None
      from_me: bool
      limit: int               # the tool asks for limit + 1, as in 02
      after: SearchOffset | None

  @dataclass(frozen=True)
  class SearchPage:
      messages: list[Message]  # newest first; Message.chat_id is the hit's chat
      resume: SearchOffset | None  # set only when the source stopped on the scan cap (see from_me) before `limit`
  ```
  `MessageSource.search_messages(self, query: SearchQuery, chat: Chat | None) -> SearchPage`.
- **Per-chat (live):** `iter_messages(entity, search=text, limit=None, offset_date=query.end, offset_id=after.message_id or 0, from_user="me" if from_me else None)`. Stop on `msg.date < start` (break), skip `msg.date >= end`, stop at `limit`. Forum topics are searched as one chat (no `topic` arg: YAGNI).
- **Global (live):** loop `SearchGlobalRequest(q=text, filter=InputMessagesFilterEmpty(), min_date=start, max_date=end, offset_rate=…, offset_peer=…, offset_id=…, limit=min(100, remaining))`. First request: `0, InputPeerEmpty(), 0`; with `after`: `offset_rate=int(after.date.timestamp())`, `offset_peer=await self._input_entity(client, after.chat_id)`, `offset_id=after.message_id`. Between internal requests advance the same way from the last raw message (date, peer, id), the way Pyrogram pages it; this makes a cursor from any **shown** hit resumable. Stop when a response has fewer messages than asked, or is `messages.Messages` (not a slice). Map with `message_from_tl(raw, utils.get_peer_id(raw.peer_id))`; raw results need `raw._finish_init(client, entities, None)` with `entities = {utils.get_peer_id(e): e for e in r.users + r.chats}` so senders resolve (the builder checks the exact call against Telethon's `_MessagesIter`). Skip `date >= end` and stop on `date < start` client-side too.
- **`from_me`:** per-chat → server-side `from_user="me"`. Global → `SearchGlobal` has no sender filter, so filter `msg.out` client-side and scan at most `GLOBAL_SCAN_CAP = 500` raw hits per call. If the cap is hit before `limit`, return what was found plus `resume` = offset of the last **scanned** hit. The tool then still prints a `Next:` cursor (built from `resume`) with the text `Scanned 500 hits, more may remain.`, so "few own hits" never reads as "no hits".
- **Cursor** (`SearchCursor`, `encode_search_cursor` / `decode_search_cursor` in `cursor.py`, same style as 02: `_`-joined, one letter per field, digits only):
  `{h|g}{message_id}_d{epoch}_{u|n}{abs(chat_id)}[_s{epoch}][_e{epoch}][_m]_q{tag}`
  - `h` = per-chat, `g` = global. `u`/`n` = positive/negative chat id (the hit's chat; for per-chat it's also the searched chat).
  - `q` = `zlib.crc32(resolve.normalize(query).encode()) % 1000`, 3 digits: a cursor re-sent with a different query is caught.
  - With `cursor`, the period/`from_me` args are ignored (taken from the cursor), `query` is required and checked, `limit` is honoured.
  - Per-chat cursor and `chat` omitted → search that cursor's chat (models drop args). Per-chat cursor whose chat ≠ the resolved `chat` → `CursorError(other_chat=True)`. Global cursor with `chat` given → ToolError `This cursor is from a search in all chats; call search_messages without chat.`.
  - `CursorError` gains `tool: str = "get_messages"` (used in its "call … again without cursor" text) and `other_query: bool` → `This cursor belongs to another query. Use the cursor from this query's last result, or call search_messages again without cursor.`.
- **Output** (per-chat):
  ```
  Search "адреса" in Anna K (123456) · all time (Europe/Warsaw)
  From me only
  #4812 2026-10-25 14:03 Me: …моя адреса Хмільна 7, кв. 12…
  #4790 2026-10-20 09:10 Anna (@anna_k): скинь адресу
  Shown 2 hits (2026-10-25 14:03 → 2026-10-20 09:10). Older hits remain. Next: call search_messages(query="адреса", chat="123456", cursor="h4790_d1792…_u123456_q417")
  Context: call get_messages(chat="<id>", around=<#id>).
  ```
  Global: head `Search "адреса" in all chats · <period> (tz)`; hits grouped by chat in order of each chat's newest hit; group header `— {title} ({id}) —`, plus ` · Links: {base}/<id>` when `chat_link_base` has one. Per-chat head gets the usual `Links:` line instead.
  - Hit line = 02's message line with a full `YYYY-MM-DD HH:MM` stamp instead of `HH:MM`; no day headers. Sender/`@username` once per page, media, `↩`, `✎`, `⤷fwd` as in 02.
  - Text = `snippet(text, query, width=SEARCH_TEXT_CHARS)`, `SEARCH_TEXT_CHARS = 300`: whole `one_line` text if it fits; else a window starting `width // 3` before the earliest casefolded occurrence of any query token (≥ 2 chars), with `…` on each cut side; no occurrence (Telegram also matches word forms) → first `width` chars + `…`.
  - Period text: `describe_period` except no-period → `all time`.
  - Query quoting in head and footer: `json.dumps(one_line(query), ensure_ascii=False)`.
  - Footer: `Shown N hit(s) (newest → oldest).` then `Older hits remain. Next: call search_messages(…)` / `No more hits.`; the `Context:` line always follows when ≥ 1 hit. Empty: `No hits. Telegram search matches words and word starts, not meaning: try other word forms, synonyms and the other languages of this chat.` (no cursor).
  - Global titles come from `source.list_dialogs()` by id; unknown id → `chat {id}`, no links.
  - Budget: keep the newest hits that fit (prefix), exact fitting like `render_page`; if not even one fits, cut that hit's text. Reuse `FOOTER_RESERVE` (a test checks the longest real footer, with a ~60-char query, still fits it; raise the reserve if not).
- **`get_messages(..., around: int | None = None)`:**
  - `limit` becomes `int | None = None`: `None` → 100 normally, `AROUND_LIMIT = 20` with `around`. Clamp unchanged.
  - With `cursor`, `around` is ignored. With `around`, `days/since/until/topic/from_me` are ignored (docstring says so).
  - `before = (limit - 1) // 2`, `after = limit - 1 - before`. Two source calls: backward `boundary_id=around + 1`, `limit=before + 2` (target + `before` + 1 to detect more); forward `boundary_id=around`, `limit=after + 1`.
  - Head: `Chat: … · around #4812 (tz)` (+ `Links:`); if no message with that id came back, `around #4812 (not found; nearest shown)`.
  - Lines: day headers as in 02; the target line is prefixed `▶` (`▶#4812 14:03 Me: …`).
  - Budget: drop from whichever end is farther (in count) from the target until it fits; the target is never dropped (cut its text if alone).
  - Footer: `Shown N messages around #4812 (first → last).` then, when more exist or were dropped on that side, `Older: call get_messages(chat="<id>", cursor="b…")` and/or `Newer: call get_messages(chat="<id>", cursor="f…")`, using 02's `Cursor` with `start=end=None`, `topic_id=None`, `from_me=False`. Empty chat → `No messages.`.

## Review Focus

1. A query containing `"`, `\` or a newline: head and footer stay one line each and the `Next:` call is valid JSON-quoted text → Task 3 test `test_query_with_quotes_is_quoted_once`.
2. A cursor re-sent with a reworded query, or into another chat, or a global cursor with `chat` → named errors, not silent wrong pages → Task 2 + Task 4 tests.
3. A global hit in a chat missing from the dialog cache (just created) → shown under `chat {id}`, no crash → Task 4 `test_global_hit_in_unknown_chat`.
4. `around` on the first/last message of a chat or a deleted id → window still renders, only the existing side gets a cursor → Task 5 tests.
5. Global `from_me` with own hits spread thinly → scan cap returns a resumable cursor, not "No hits" → Task 4 `test_scan_cap_still_offers_next` (FakeSource honours a `scan_cap` argument) and Task 6 live test.

---

### Task 1: Search models, protocol, FakeSource

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/protocol.py`, `tg_bridge/source/__init__.py`, `tests/fakes.py`
- Test: `tests/test_fakes_search.py` (new; the fake's behaviour is what later tests trust)

**Interfaces:**
- Produces: `SearchOffset`, `SearchQuery`, `SearchPage` (exact fields above, exported from `tg_bridge.source`); `MessageSource.search_messages(query, chat)`; `FakeSource.search_messages` + `FakeSource.search_queries: list[tuple[SearchQuery, int | None]]`; `FakeSource(..., scan_cap: int | None = None)`.

- [ ] **Step 1: Failing tests** — `test_per_chat_matches_casefolded_substring_newest_first`, `test_global_spans_chats_sorted_by_date_desc`, `test_after_offset_excludes_it_and_newer` (by `(date, chat_id, message_id)` ordering), `test_period_and_from_me_filter`, `test_scan_cap_sets_resume_to_last_scanned` (with `from_me` in global mode only).
- [ ] **Step 2:** `uv run pytest tests/test_fakes_search.py -q` → FAIL (import error).
- [ ] **Step 3:** Add the dataclasses and protocol method (docstring: "newest first; `chat=None` searches all the user's dialogs"); implement the fake.
- [ ] **Step 4:** `uv run pytest -q` → all PASS.
- [ ] **Step 5:** Commit `feat(source): search_messages contract and fake`.

### Task 2: Search cursor

**Files:**
- Modify: `tg_bridge/cursor.py`, `tg_bridge/errors.py`
- Test: `tests/test_cursor.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) SearchCursor(scope: Literal["chat", "global"], after: SearchOffset, start: datetime | None, end: datetime | None, from_me: bool)`; `encode_search_cursor(c: SearchCursor, query: str) -> str`; `decode_search_cursor(s: str, query: str) -> SearchCursor` (raises `CursorError()` / `CursorError(other_query=True, tool="search_messages")`); `query_tag(query: str) -> str`. Chat checks happen in the tool (Task 4), since only it knows `chat`.

- [ ] **Step 1: Failing tests** — round trip for both scopes incl. negative chat id `-1001234567890` (`n1001234567890`) and optional fields absent; `test_query_tag_ignores_case_and_spacing` (`"Моя  Адреса"` ≡ `"моя адреса"`); `test_other_query_rejected` (message mentions "another query"); garbage/duplicate fields/missing `d` or chat field → `CursorError` whose text says `search_messages`; existing `get_messages` cursor tests still pass and their error text is unchanged.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat(cursor): search cursor with query tag`.

### Task 3: Hit rendering

**Files:**
- Modify: `tg_bridge/format.py`
- Test: `tests/test_format_search.py` (new)

**Interfaces:**
- Consumes: `Message`, `Chat`, `chat_link_base`, `one_line`, `_sender`/`_media_text` internals.
- Produces:
  - `snippet(text: str, query: str, width: int = SEARCH_TEXT_CHARS) -> str`
  - `search_head(query: str, chat: Chat | None, period_text: str, tz: ZoneInfo, from_me: bool) -> str`
  - `render_hits(head: str, hits: Sequence[Message], *, chats: Mapping[int, Chat] | None, query: str, tz: ZoneInfo, max_chars: int, more: bool, next_hint: Callable[[Message], str], scanned_note: str | None = None) -> str` — `hits` newest first; `chats` given = global grouping, `None` = per-chat flat list. `next_hint(boundary)` is called with the oldest shown hit; the tool may ignore it and use `resume` instead.
  - `_message_line` takes a preformatted `stamp: str` instead of the local time (02 passes `HH:MM`).

- [ ] **Step 1: Failing tests** — exact per-chat and global outputs matching the examples in "Decisions" (2 chats, one with links, one without); `test_snippet_centres_on_late_match` (match at char 1000 of 2000 → contains the word, starts and ends with `…`, `len ≤ 302`); `test_snippet_no_match_takes_head`; `test_query_with_quotes_is_quoted_once` (`'say "hi"\nnow'` → head and footer each one line, contains `"say \"hi\" ⏎ now"` or the `one_line` equivalent); `test_budget_keeps_newest_hits` (60 hits, `max_chars=2000` → output ≤ 2000, first shown = newest, footer has `Next:`); `test_single_huge_hit_is_cut`; `test_empty_hint_suggests_variants`; 02's format tests unchanged.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat(format): search hits, snippets, grouped global output`.

### Task 4: `search_messages` tool + server registration

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py` (or new `tests/test_tools_search.py`), `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `async def search_messages(source, settings, query: str, chat: str | None = None, *, days=None, since=None, until=None, from_me=False, cursor=None, limit=30, now=...) -> str`.

- [ ] **Step 1: Failing tests** (FakeSource) — per-chat hits with dates; global grouped; `from_me`; period passed through to `SearchQuery`; `limit + 1` requested and `more` detection; cursor round trip gives the next page with no duplicates/gaps (3 pages over 7 hits, `limit=3`); cursor + changed query → "another query"; per-chat cursor without `chat` searches the cursor's chat; per-chat cursor into another chat → other-chat error; global cursor with `chat` → the global-cursor error; empty/too-long query errors; `test_global_hit_in_unknown_chat`; `test_scan_cap_still_offers_next` (`FakeSource(scan_cap=5)`, global `from_me`, 1 own hit in the first 5 → 1 hit shown + `Scanned … more may remain` + a working `Next:`); ambiguous `chat` → candidate list (reuses `resolve_chat_arg`). Server test: tool is registered read-only with the docstring below.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement the tool; register it:
  ```python
  async def search_messages(query: str, chat: str | None = None, days: int | None = None, since: str | None = None,
                            until: str | None = None, from_me: bool = False, cursor: str | None = None,
                            limit: int = 30) -> str:
      """Find messages containing words, in one chat (`chat`: id, @username or title words) or, without `chat`,
      in all the user's chats. Newest hits first, each with its date and #id.
      Telegram matches whole words and word starts, NOT meaning: if nothing useful comes back, search again with
      other word forms, synonyms, and the other languages people use in that chat (e.g. Ukrainian, Russian,
      Polish, English). One query per call; prefer 1–2 distinctive words.
      Period: `days` OR `since`/`until`, as in get_messages. `from_me`: only messages the user sent.
      To see the conversation around a hit: get_messages(chat=<chat id>, around=<#id>).
      If the result ends with "Next: call search_messages(...)", copy that call exactly to get older hits."""
  ```
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat: search_messages tool`.

### Task 5: `get_messages(around=…)`

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/format.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py`, `tests/test_format_messages.py`

**Interfaces:**
- Consumes: existing `MessageSource.get_messages`, `Cursor`, `encode_cursor`, `_page_text` internals.
- Produces: `render_around(head: str, messages: Sequence[Message], target_id: int, *, tz, max_chars, more_older: bool, more_newer: bool, older_hint: Callable[[Message], str], newer_hint: Callable[[Message], str]) -> str` (`messages` chronological); `tools.get_messages(..., around: int | None = None, limit: int | None = None)`.

- [ ] **Step 1: Failing tests** — `test_around_centres_on_target` (ids 1..50, `around=25`, default limit → ids 16..35 shown, `▶#25`, both `Older:` and `Newer:` cursors; the two `MessageQuery`s have the boundaries/limits from "Decisions"); `test_around_first_message_has_no_older`; `test_around_missing_id_says_not_found`; `test_around_budget_trims_far_ends_keeps_target`; `test_around_ignores_period_and_topic`; `test_cursor_wins_over_around`; `test_default_limit_still_100_without_around`; the `Newer:` cursor continues forward from the newest shown id.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement; add `around: int | None = None` and `limit: int | None = None` to the server signature; append to its docstring: `around: a message #id (e.g. from search_messages): shows the messages just before and after it; period, topic and from_me are then ignored.`
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** Commit `feat(reading): get_messages around a message id`.

### Task 6: Live search in `ReadOnlyTelegram` + `LiveSource`

**Files:**
- Modify: `tg_bridge/telegram/client.py`, `tg_bridge/source/live.py`
- Test: `tests/telegram/test_client.py`, `tests/test_live_source.py`

**Interfaces:**
- Consumes: `SearchQuery`, `SearchPage`, `SearchOffset`, `message_from_tl`, `_input_entity`, `_call`.
- Produces: `ReadOnlyTelegram.search(query: SearchQuery, chat: Chat | None) -> SearchPage`; `LiveSource.search_messages` passes through. Constant `GLOBAL_SCAN_CAP = 500` in `client.py`.

- [ ] **Step 1: Failing tests** (mocked client, like the existing `iter_messages` fakes) —
  - per-chat: `iter_messages` called with `search`, `offset_date=end`, `offset_id=after.message_id`, `from_user="me"` iff `from_me`; stops at `start`; skips `>= end`; stops at `limit`.
  - global: first request has `offset_rate=0`, `InputPeerEmpty`, `offset_id=0`, `min_date/max_date` from the query; with `after`, the request carries `int(after.date.timestamp())`, the input peer of `after.chat_id`, `after.message_id`; a second internal request advances from the last raw message; stops on a short page or a non-slice response; `Message.chat_id` comes from each raw `peer_id`.
  - global `from_me`: non-`out` hits dropped; after 500 scanned raw hits returns early with `resume` = last scanned.
  - FloodWait inside the loop → `RateLimitedError` (whole loop inside one `_call`).
  - Logging test: captured stderr contains the hit count and not the query text.
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest -q` → PASS, incl. `test_import_boundary`.
- [ ] **Step 5:** Commit `feat(telegram): per-chat and global search`.

### Task 7: Docs

**Files:**
- Modify: `docs/features/03-search.md`, `docs/design.md` §7 table

- [ ] **Step 1:** Brief: add `around` to Scope; extend the smoke checklist:
  - [ ] Paging: page 2 of a global search has no duplicates or gaps against page 1 (validates the `offset_rate` = date choice).
  - [ ] A hit inside an **archived** chat shows up in global search (if not: note it; `folder_id=1` would need a second request).
  - [ ] `get_messages(around=<hit id>)` from a search result shows the context with `▶` on the hit.
  - [ ] Global `from_me` finds an old message of yours.
- [ ] **Step 2:** design §7 table: `get_messages(chat, period…, topic?, around?, cursor?, limit?)`; `search_messages(query, chat?, period…, from_me?, cursor?, limit?)`.
- [ ] **Step 3:** `uv run pytest -q` → PASS. Commit `docs: feature 03 smoke checklist and contract`.

At finish (maintainer, after smoke): roadmap `[x]`, Log line: "03: `get_messages` gained `around` for search context; global search pages by the boundary hit's date as `offset_rate`."
