# 05 — Deleted Users Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `find_deleted_users()` lists every visible 1:1 dialog whose peer account is deleted, with message count, first/last date and any identity guess. `export_deleted_users()` saves each of those conversations to its own Markdown file in `<EXPORT_DIR>/deleted-users/`, across as many calls as the 40 s limit needs.

**Architecture:** Detection reuses the cached dialog list (`Chat.deleted_peer` already exists). Two new read methods on the source: `get_history_stats` (count + first message date, one `GetHistory` call via Telethon's `TotalList.total`) and `get_contact_cards` (server-side search with `InputMessagesFilterContacts`, the only identity data that survives account deletion). The per-chat export is 04's `export_chat_file`, which gains a time budget argument and identity lines for the header. A new `tg_bridge/deleted.py` holds the loop; `tools.py` and `server.py` stay thin.

**Tech Stack:** unchanged (Python 3.14, Telethon, `mcp`, `pytest-asyncio`).

**Spec:** `docs/features/05-deleted-users.md` + `docs/design.md` §2 scenario 3, §5 D9, §7 rows `find_deleted_users`/`export_deleted_users`, §11 "Deleted users" and "Exports". Patterns to copy: `export_chat` in `tg_bridge/tools.py` and `tests/test_tools_export.py`, `get_post` in `tg_bridge/telegram/client.py`, `ScopedSource.get_post`, `tests/test_scoped_source.py`.

## Global Constraints

All constraints from earlier plans still apply. Additionally:

- **Read-only.** Only history reads and in-chat search. No new raw request in `ALLOWED_REQUESTS` (`tests/telegram/test_client.py`): both new methods go through `iter_messages`/`get_messages`.
- **Only `tg_bridge/telegram/` imports Telethon.** `HistoryStats` and `ContactCard` are bridge models in `tg_bridge/source/models.py`.
- **Logging:** chat ids, counts, durations only. Never names from contact cards, file paths (they hold the title) or phones.
- **Scope (17):** detection runs on `source.list_dialogs()`, so under `public` only switched-on private chats are seen. `ScopedSource` checks visibility in both new methods.
- **Budget:** `find_deleted_users` output ≤ `settings.max_chars`; one `export_deleted_users` call stops starting new work after `EXPORT_TIME_BUDGET` (40 s) in total, not per chat.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here (Claude's calls)

- **No `dir` argument** (brief had `dir?`). A model-supplied path could write anywhere on the host, and on CT 120 that host is a server. Files always go to `settings.export_dir / "deleted-users"`. Log it as a deviation.
- **Identity guess = contact cards only.** A deleted account's own name is gone everywhere: its messages and forwards of them resolve to the same deleted user. What survives is a contact card (`MessageMediaContact`) whose `user_id` is the peer: its first/last name are as written on the card. Forward `from_name` cannot be tied to the peer (it is set exactly when the id is hidden), mentions in a 1:1 chat are rare, and the file has the full text for a reader to infer the rest. Log it as a deviation from "forwards/replies/mentions". Phone numbers from cards are not taken.
- **Guess text:** `<first> <last> (name on a contact card, #<msg id>, <YYYY-MM-DD>; a guess)`. Up to 3 distinct names per chat, newest card first; cards of other users are ignored.
- **`export_deleted_users` is idempotent, no `resume` flag.** A deleted account can't receive or send new messages, so its history is frozen. Per chat: latest file complete → skip and report it; latest file unfinished (pause note) → resume it; no usable file → start one. The model just calls again while the result says `Next:`.
- **Order:** chats sorted by id, so repeated calls walk the same list.
- **Rate limit or other bridge error while exporting one chat** ends that file with 04's INCOMPLETE footer and resume note, and the call stops there (the next chats would hit the same limit). The result says to call again later.
- **No messages in a chat** (Telegram keeps the dialog but history is empty): no file, reported as `no messages, no file`.
- **Stats failure for one chat** in `find_deleted_users` (`TgBridgeError` other than rate limit): that line shows `count unavailable`; a rate limit fails the whole tool via `_tool_errors` (one retry later is cheaper than a half list).

## Review Focus

1. **No deleted chats at all** → one clear line, plus the scope hint under `public`; not an empty string.
2. **Many deleted chats (50+)** → `find_deleted_users` output cut to `max_chars` with `…`, totals in the first line so the count is never lost.
3. **Second `export_deleted_users` call after everything is done** → every chat reported as already exported with its path, no new files, no `Next:`.
4. **Time runs out between chats** → no chat is half-started without a note; the result names what is left and ends with `Next: call export_deleted_users()`.
5. **Contact card of someone else in the chat** (owner forwarded a friend's contact) → not shown as the peer's guess.

---

### Task 1: Models and source methods (fake, scoped, live)

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/protocol.py`, `tg_bridge/source/live.py`, `tg_bridge/source/scoped.py`, `tg_bridge/source/__init__.py` (exports)
- Modify: `tg_bridge/telegram/client.py`, `tg_bridge/telegram/mapping.py`
- Modify: `tests/fakes.py`
- Test: `tests/test_scoped_source.py`, `tests/telegram/test_client.py`, `tests/telegram/test_mapping.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class HistoryStats: count: int; first_at: datetime | None  # aware UTC; None when empty`
  - `@dataclass(frozen=True) class ContactCard: message_id: int; date: datetime; user_id: int | None; first_name: str | None; last_name: str | None`
  - `MessageSource.get_history_stats(chat: Chat) -> HistoryStats`
  - `MessageSource.get_contact_cards(chat: Chat, limit: int = 20) -> list[ContactCard]` (newest first)
  - `FakeSource(..., stats: dict[int, HistoryStats] | None = None, cards: dict[int, list[ContactCard]] | None = None)`; missing key → `HistoryStats(len(messages[chat]), first message date or None)` and `[]`.
  - mapping: `contact_card_from_tl(raw) -> ContactCard | None` (None unless `raw.media` is `MessageMediaContact`; `user_id` 0 → None).

- [ ] **Step 1: Write the failing tests**

```python
# tests/telegram/test_mapping.py
def test_contact_card_from_tl_reads_card_fields(): ...
    # MessageMediaContact(phone_number="+48…", first_name="Olena", last_name="P", vcard="", user_id=777)
    # → ContactCard(message_id=5, date=<msg date>, user_id=777, first_name="Olena", last_name="P")
def test_contact_card_from_tl_none_for_other_media_and_zero_user_id(): ...

# tests/telegram/test_client.py
async def test_get_history_stats_uses_total_and_oldest_message(wrapper): ...
    # fake get_messages(entity, limit=1, reverse=True) returns TotalList total=1234 with one msg dated 2019-03-01
    # → HistoryStats(1234, datetime(2019,3,1,tzinfo=UTC)); requests ⊆ ALLOWED_REQUESTS
async def test_get_history_stats_empty_history(wrapper): ...  # total=0, no messages → HistoryStats(0, None)
async def test_get_contact_cards_filters_contacts(wrapper): ...
    # iter_messages called with filter=InputMessagesFilterContacts, limit=20; returns cards newest first
async def test_history_stats_flood_becomes_rate_limited(wrapper): ...

# tests/test_scoped_source.py
async def test_hidden_chat_stats_and_cards_raise_chat_not_found(): ...
async def test_visible_chat_stats_and_cards_pass_through(): ...
```

- [ ] **Step 2: Run them, expect FAIL** — `uv run pytest -q tests/telegram/test_mapping.py tests/telegram/test_client.py tests/test_scoped_source.py`

- [ ] **Step 3: Implement.** Client: `get_history_stats` = `await client.get_messages(entity, limit=1, reverse=True)` (Telethon's `TotalList.total` is the server count; `wait_time=0`), wrapped in the same FloodWait → `RateLimitedError` mapping as `get_messages`. `get_contact_cards` = `iter_messages(entity, limit=limit, filter=tl.InputMessagesFilterContacts())` mapped with `contact_card_from_tl`, Nones dropped. `LiveSource` delegates after `_ensure_connected`. `ScopedSource` calls `_require_visible` first. Log `log.info("history stats chat=%s count=%d", …)` only.

- [ ] **Step 4: Run them, expect PASS**, then `uv run pytest -q` (whole suite green; `test_import_boundary.py` still passes).

- [ ] **Step 5: Commit** — `feat(source): history stats and contact cards`

### Task 2: Exporter accepts a time budget and identity lines

**Files:**
- Modify: `tg_bridge/export.py`
- Test: `tests/test_export_file.py`, `tests/test_export_render.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `export_chat_file(..., budget: float = EXPORT_TIME_BUDGET, identity: Sequence[str] = ())` — `budget` replaces the hard-coded `EXPORT_TIME_BUDGET` check; `identity` is passed to the header.
  - `render_header(info, period, tz, now, identity: Sequence[str] = ())` — after the `- Peer:` line, one line per item: `- Identity guess: <text>`.
  - `ExportState = Literal["complete", "unfinished"]`; `latest_export(dest_dir: Path, chat: Chat) -> tuple[Path, ExportState] | None` — newest `*_<id>_*.md`; `complete` when the tail ends with ` complete`, `unfinished` when it has a valid resume note, `None` when no file or neither (old INCOMPLETE file without a note).
  - `_reopen_latest` reuses the same tail reading (one helper, no second copy of the footer parsing).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_export_render.py
def test_header_lists_identity_guesses_after_peer():
    text = render_header(info_with_deleted_peer, Period(None, None), TZ, NOW, identity=["Olena P (name on a contact card, #5, 2020-05-02; a guess)"])
    assert "- Peer: Deleted Account · id 777 · deleted\n- Identity guess: Olena P (name on a contact card, #5, 2020-05-02; a guess)\n" in text

# tests/test_export_file.py
async def test_budget_argument_pauses_earlier(): ...  # budget=5.0, fake clock +6 s per page → paused after page 1
async def test_latest_export_states(tmp_path): ...
    # none → None; complete export → (path, "complete"); paused export → (path, "unfinished");
    # file with "INCOMPLETE: x" footer and no note → None
```

- [ ] **Step 2: Run them, expect FAIL** — `uv run pytest -q tests/test_export_file.py tests/test_export_render.py`
- [ ] **Step 3: Implement** as in Interfaces. `export_chat`'s behaviour does not change (defaults).
- [ ] **Step 4: Run them, expect PASS**, then `uv run pytest -q`.
- [ ] **Step 5: Commit** — `feat(export): time budget, identity lines, latest export state`

### Task 3: Detection and `find_deleted_users`

**Files:**
- Create: `tg_bridge/deleted.py`
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_deleted.py`, `tests/test_tools_deleted.py`

**Interfaces:**
- Consumes: Task 1's `get_history_stats`, `get_contact_cards`, `HistoryStats`, `ContactCard`.
- Produces (in `tg_bridge/deleted.py`):
  - `@dataclass(frozen=True) class DeletedDialog: chat: Chat; stats: HistoryStats | None; guesses: tuple[str, ...]`
  - `deleted_dialogs(chats: Iterable[Chat]) -> list[Chat]` — `kind == "user"` and `chat.deleted_peer`, sorted by id (bots excluded by kind).
  - `identity_guesses(peer_id: int, cards: Sequence[ContactCard], tz: ZoneInfo) -> tuple[str, ...]` — cards with `user_id == peer_id` and a non-empty name, distinct names, newest first, max 3, text as in Decisions.
  - `async def scan_deleted(source: MessageSource, tz: ZoneInfo) -> list[DeletedDialog]` — `list_dialogs()` → `deleted_dialogs` → per chat stats + cards; a non-rate-limit `TgBridgeError` on stats → `stats=None`; on cards → `guesses=()`.
  - `tools.find_deleted_users(source, settings) -> str`
  - server tool `find_deleted_users()` (`READ_ONLY`).

Output format (exact):

```
Found 2 chats with deleted accounts (1:1 chats, archived included).
Deleted Account · id 777 · 1234 messages · 2019-03-01 → 2023-07-14 · archived · guess: Olena P (name on a contact card, #5, 2020-05-02; a guess)
Deleted Account · id 912 · count unavailable · last 2022-01-03
Chats you deleted on your side are gone and cannot be found. To save them as files: export_deleted_users().
```

- None found: `No chats with deleted accounts (1:1 chats, archived included).`
- Under `settings.scope != "all"` add before the last line: `Scope is public: private chats not switched on in the allowlist were not checked.`
- Dates in `settings.timezone`, `YYYY-MM-DD`; `last` from `chat.last_message_at`.
- Whole text cut to `max_chars` like `export_chat` (first line always survives).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_deleted.py
def test_deleted_dialogs_keeps_only_deleted_user_peers_sorted(): ...  # group, bot, normal user, two deleted users → [id 777, id 912]
def test_identity_guesses_ignores_other_users_and_dedupes(): ...
    # cards: (user 777 "Olena P" #9 2021), (user 555 "Ivan"), (user 777 "Olena P" #5 2020), (user 777 "O." #3 2019), (user 777 no name)
    # → ("Olena P (name on a contact card, #9, 2021-…; a guess)", "O. (name on a contact card, #3, 2019-…; a guess)")
async def test_scan_deleted_stats_error_gives_none(): ...

# tests/test_tools_deleted.py
async def test_find_lists_counts_dates_archived_and_guess(): ...  # asserts the exact lines above
async def test_find_none_found(): ...
async def test_find_mentions_scope_when_public(): ...
async def test_find_truncates_to_max_chars_keeping_first_line(): ...  # 60 deleted chats, max_chars=500
async def test_find_rate_limit_is_tool_error(): ...
```

- [ ] **Step 2: Run them, expect FAIL** — `uv run pytest -q tests/test_deleted.py tests/test_tools_deleted.py`
- [ ] **Step 3: Implement.** Server docstring: `List the user's 1:1 chats whose other person deleted their Telegram account: message count, dates and any name guess. Use it for "deleted accounts", "Deleted Account chats". Chats the user deleted on their own side cannot be recovered. To save them as files call export_deleted_users().`
- [ ] **Step 4: Run them, expect PASS**, then `uv run pytest -q`.
- [ ] **Step 5: Commit** — `feat(deleted): find_deleted_users`

### Task 4: `export_deleted_users`

**Files:**
- Modify: `tg_bridge/deleted.py`, `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools_deleted.py`

**Interfaces:**
- Consumes: Task 2's `export_chat_file(budget=, identity=)`, `latest_export`; Task 3's `deleted_dialogs`, `identity_guesses`.
- Produces:
  - `DELETED_DIR = "deleted-users"`; `MIN_START = 5.0` (seconds left needed to start or resume a chat).
  - `@dataclass(frozen=True) class ChatOutcome: chat: Chat; status: Literal["exported", "already", "paused", "stopped", "empty"]; path: Path | None; count: int; reason: str | None`
  - `async def export_deleted(source, dest_dir: Path, tz, now: datetime, *, sleep, clock) -> tuple[list[ChatOutcome], int]` — returns outcomes for the chats it reached and how many were left untouched because time ran out.
  - `tools.export_deleted_users(source, settings, *, now, sleep, clock) -> str`
  - server tool `export_deleted_users()` (`WRITES_LOCAL_FILE`).

Loop per chat (sorted by id): `latest_export` → `complete`: `already` (no Telegram calls); else if `clock()` past `deadline - MIN_START`: stop, count the rest as left; else `get_chat_info` + `get_contact_cards` → `identity_guesses` → `export_chat_file(..., resume=state == "unfinished", budget=deadline - clock(), identity=…)`. Result `paused` → stop; `stopped` → stop; `path is None` → `empty`.

Output format (exact):

```
Deleted-account chats: 3. Files in /…/exports/deleted-users:
- id 777: 1234 messages → /…/deleted-account_777_2019-03-01_2023-07-14_20261107-210000.md
- id 912: already exported earlier → /…/deleted-account_912_….md
- id 990: 2000 messages so far (paused) → /…/deleted-account_990_….md
Not finished: one call has a time limit; 1 chat not started yet. Next: call export_deleted_users()
```

- `empty`: `- id 501: no messages, no file`
- `stopped`: `- id 990: stopped early: <reason> → <path>` and last line `Stopped: <reason>. Call export_deleted_users() again later to continue.`
- All done: last line `All files are complete. They hold the full text; it was not read into this conversation.`
- None found: same text as `find_deleted_users`'s none line (plus the scope line under `public`).

- [ ] **Step 1: Write the failing tests**

```python
async def test_export_writes_one_file_per_deleted_chat_with_identity(tmp_path): ...
    # 2 deleted chats with messages, one contact card for 777 → 2 files in tmp/deleted-users, header has "- Identity guess:"
async def test_second_call_reports_already_exported_and_writes_nothing(tmp_path): ...
async def test_time_runs_out_between_chats_then_next_call_finishes(tmp_path): ...
    # fake clock: first chat eats 38 s → second not started, text ends with "Next: call export_deleted_users()";
    # second call: first "already", second exported
async def test_paused_chat_is_resumed_into_the_same_file(tmp_path): ...
async def test_rate_limit_stops_the_call_with_incomplete_file(tmp_path): ...
async def test_empty_chat_gets_no_file(tmp_path): ...
```

- [ ] **Step 2: Run them, expect FAIL** — `uv run pytest -q tests/test_tools_deleted.py`
- [ ] **Step 3: Implement.** Server docstring: `Save every 1:1 chat with a deleted account to its own Markdown file (in the exports folder, deleted-users/) and return only paths and counts, NOT the messages. Chats already saved are skipped. One call works for about 40 seconds: if the result says "Next: call export_deleted_users()", make exactly that call, and repeat until it no longer says so.` Log `log.info("export deleted chats=%d done=%d left=%d", …)`.
- [ ] **Step 4: Run them, expect PASS**, then `uv run pytest -q`.
- [ ] **Step 5: Commit** — `feat(deleted): export_deleted_users`

### Task 5: Docs

**Files:**
- Modify: `docs/design.md` §7 (two rows: `export_deleted_users()` without `dir`, return shapes), `docs/features/05-deleted-users.md` (smoke checklist: add "a second call writes no new files" and "a contact card, if any, shows as a guess"), `docs/roadmap.md` (Log lines for the two deviations: no `dir`, contact cards only).

- [ ] **Step 1:** Edit the three files.
- [ ] **Step 2:** `uv run pytest -q` still green.
- [ ] **Step 3: Commit** — `docs(05): tool contract, smoke checklist, deviations`
