# 01 — Chats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The model can find any dialog (personal, group, channel, archived) with `list_chats`, inspect one with `get_chat_info`, and every later tool can turn a chat argument (id / `@username` / title fragment) into exactly one dialog or a readable "ambiguous / not found" answer.

**Architecture:** `ReadOnlyTelegram` gains `list_dialogs()` and `get_chat_details()` (Telethon → domain models via `telegram/mapping`). `LiveSource` caches the dialog list per process (TTL). The **resolver** is a pure function over `Sequence[Chat]` in `tg_bridge/resolve.py`, so it is source-independent and unit-tested without any fake Telegram. Rendering lives in a new `tg_bridge/format.py` (design §6), tools stay thin.

**Tech Stack:** unchanged (Python ≥3.12, `telethon` 1.45, `mcp` v2 `MCPServer`, `pytest-asyncio`). Fuzzy matching uses stdlib only (`unicodedata`, `difflib`).

**Spec:** `docs/features/01-chats.md` + `docs/design.md` §6 (models), §7 (`list_chats`, `get_chat_info`, chat arguments, errors), §8 (logging), §9 (resolver tests), §11, §12.

## Global Constraints

All constraints from `docs/plans/00-foundation.md` still apply. Additionally:

- **Read-only Telegram calls allowed in this feature:** `iter_dialogs`, `get_input_entity`, `users.GetFullUserRequest`, `messages.GetFullChatRequest`, `channels.GetFullChannelRequest`, `messages.GetForumTopicsRequest` (it lives in `messages`, not `channels`, in Telethon 1.45 — verified). Nothing else. No `get_entity` by username (it calls `contacts.ResolveUsername`, which has harsh flood limits and can reach chats outside the user's dialogs).
- Logs: dialog **counts**, chat **ids** and durations only. Never titles, usernames, descriptions, phones, or the user's query string.
- Errors that list candidates contain names: they go to the model as `ToolError` text and are **never logged**.
- All times in tool output are rendered in `settings.timezone`, format `YYYY-MM-DD HH:MM`.
- Ask the maintainer before every commit. The commit steps below are proposals.

## Decisions taken here

- **Chat ids are Telethon "marked" ids** (`dialog.id`: users positive, basic groups `-N`, channels/supergroups `-100N`). They are unique across peer types. The resolver also accepts a **bare** positive id and tries `N`, `-N`, `-100N` in that order, because models often drop the prefix. If more than one matches → ambiguous (practically never happens).
- **Scope of resolution = the user's dialogs only** (incl. archived and all folders). An `@username` or id that is not a dialog → "not found" with closest matches. Resolving public chats the user isn't in is out of scope (possible later feature).
- **`ChatKind`** = `"user" | "bot" | "group" | "supergroup" | "channel"`. Mapping: `User` → `bot` if `bot` else `user`; `Chat`/`ChatForbidden` → `group`; `Channel`/`ChannelForbidden` → `channel` if `broadcast` else `supergroup` (gigagroups are supergroups).
- **`list_chats(kind=...)`** accepts the five kinds plus `"group"` meaning **both** `group` and `supergroup` (that's what a user means by "groups"); `"supergroup"` stays strict. Unknown kind → ToolError listing allowed values.
- **Dialog cache in `LiveSource`:** TTL 300 s, injectable `clock` (default `time.monotonic`), guarded by the existing `asyncio.Lock` so concurrent first calls trigger one fetch. `list_dialogs(refresh=True)` bypasses it. The tools-layer resolver helper refreshes **once** on "not found" if the cache is older than 30 s (catches a chat created a minute ago without hammering Telegram).
- **Added after the smoke test:** the dialog cache is also written to disk (`tg-bridge.dialogs.json` next to the session, `600`), because LM Studio restarts the server per message; `get_chat_info` lists dialogs once and retries if Telethon's entity cache is cold. `list_chats` prints per-kind totals on line 2.
- **`iter_dialogs(ignore_migrated=True)`**: a basic group upgraded to a supergroup would otherwise appear twice.
- **`get_chat_info` is not cached** (it's one chat, the user wants fresh numbers).
- **Forum topics:** first 100 via `GetForumTopicsRequest(peer, offset_date=None, offset_id=0, offset_topic=0, limit=100)`; skip `ForumTopicDeleted`; if Telegram reports more (`count > len`), say so. Topics are listed as `#id title`, closed ones marked `(closed)`.
- **Resolver matching** (normalisation = NFKC → `casefold()` → every non-alphanumeric char becomes a space → collapse spaces; works for Cyrillic and drops emoji):
  1. `-?\d+` → id match (with the bare-id rule above).
  2. Starts with `@` (or looks like `t.me/<name>` — cheap to support, models paste links) → case-insensitive match on `chat.username` or `chat.peer.username`.
  3. Otherwise title fragment against the chat's **search names**: normalised title, plus for 1:1 chats the peer's username. Tiers, first non-empty tier wins:
     a. exact normalised title;
     b. all query tokens occur in the normalised name (token-substring, any order).
     One hit → that chat. Several → `AmbiguousChatError` (never guess). None → `ChatNotFoundError` with up to 5 suggestions ranked by `difflib.SequenceMatcher` ratio between the query and each name (best over the name and its individual tokens), threshold 0.5.
  So "Family" picks the chat titled exactly "Family" even if "Family trip" exists; "fam trip" picks "Family trip" only if no other title contains both tokens.
- **Candidate lists** show at most 10 entries in the `list_chats` line format, then `…and N more; use the id`. Deleted accounts all share the title "Deleted Account", so ambiguity on them is expected and the ids disambiguate.
- **Output budget:** `list_chats` and candidate lists stop adding lines once the next one would exceed `settings.max_chars` and append `(output truncated: narrow with query/kind)`. Real pagination arrives in feature 02; this simple cut is enough for chat lists.
- **`list_chats` order** = Telegram's dialog order (pinned first, then by last message), archived dialogs come after non-archived ones. `limit` default 30, clamped to 1..200.
- `query` in `list_chats` uses the resolver's tier (b) token matching, but returns **all** matches (it's a search, not a resolution).

## Domain model additions (`tg_bridge/source/models.py`)

```python
ChatKind = Literal["user", "bot", "group", "supergroup", "channel"]

@dataclass(frozen=True)
class Chat:
    id: int                         # marked id
    kind: ChatKind
    title: str                      # 1:1 → peer.display_name
    username: str | None            # chat's own public username (groups/channels); for 1:1 = peer.username
    peer: Person | None             # 1:1 dialogs only
    last_message_at: datetime | None  # aware UTC
    is_archived: bool
    is_forum: bool

    @property
    def deleted_peer(self) -> bool: return self.peer is not None and self.peer.is_deleted

@dataclass(frozen=True)
class Topic:
    id: int
    title: str
    is_closed: bool

@dataclass(frozen=True)
class ChatInfo:
    chat: Chat
    description: str | None         # channel/group "about", or user bio
    members_count: int | None       # None when hidden/unknown or 1:1
    topics: tuple[Topic, ...]       # empty unless forum
    topics_total: int | None        # Telegram's count when > len(topics)
```

`MessageSource` protocol gains:

```python
async def list_dialogs(self, *, refresh: bool = False) -> list[Chat]: ...
async def get_chat_info(self, chat: Chat) -> ChatInfo: ...
```

(`get_chat_info` takes a resolved `Chat`, not a raw argument: resolution is the tools layer's job.)

## Review Focus

1. **Ambiguous names never guess.** Two chats "Family" and "family 🏠" (both normalise to `family`) → candidates, not the first one. → Task 2.
2. **Bare channel id** `1234567890` finds `-1001234567890`. → Task 2.
3. **Concurrent first calls** → exactly one `iter_dialogs`. Cache expires after TTL; `refresh=True` refetches. → Task 4.
4. **Kicked/forbidden chats and deleted users** in the dialog list map without crashing (`ChatForbidden`, `ChannelForbidden`, `User(deleted=True, first_name=None)`). → Task 3.
5. **`GetFullChannel` failing** (e.g. `ChannelPrivateError` on a chat you were removed from) → `get_chat_info` still returns the basic `Chat` data with description/members `unknown`, not a crash. → Task 3.
6. **No names in logs**: caplog check in Task 4 and Task 6 that a query and a title don't appear in any log record.
7. **FloodWait during `iter_dialogs`** → `RateLimitedError` → ToolError (Telethon auto-sleeps waits under its `flood_sleep_threshold` of 60 s; only longer ones surface). → Task 3.

---

### Task 1: Models + protocol + FakeSource

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/protocol.py`, `tg_bridge/source/__init__.py` (export `Chat`, `ChatInfo`, `ChatKind`, `Topic`), `tests/fakes.py`, `tests/test_models.py`

**Interfaces:**
- Produces: the dataclasses above; `make_chat(**overrides) -> Chat` and `make_user_chat(person: Person | None = None, **overrides) -> Chat` helpers in `tests/fakes.py`; `FakeSource(dialogs=[...], infos={chat_id: ChatInfo}, error=...)` records `list_dialogs` calls and their `refresh` flags.

- [ ] **Step 1: Write failing tests:** `test_deleted_peer_true_for_deleted_user_chat`, `test_deleted_peer_false_for_group`, `test_fake_source_satisfies_protocol` (extend the existing one to call `list_dialogs` and `get_chat_info`).
- [ ] **Step 2: Run** `uv run pytest -q` → fail. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat: Chat, Topic, ChatInfo models and source protocol`.

### Task 2: Resolver

**Files:**
- Create: `tg_bridge/resolve.py`, `tests/test_resolve.py`
- Modify: `tg_bridge/errors.py`

**Interfaces:**
- Produces:
  - `normalize(text: str) -> str`
  - `resolve_chat(arg: str, chats: Sequence[Chat]) -> Chat` — raises `ChatNotFoundError` / `AmbiguousChatError`
  - `match_chats(query: str, chats: Sequence[Chat]) -> list[Chat]` — tier (b), keeps input order (used by `list_chats`)
  - `errors.ChatNotFoundError(arg: str, suggestions: list[Chat])` and `errors.AmbiguousChatError(arg: str, candidates: list[Chat])`, both `TgBridgeError`. They carry the chats; **message text is built in the tools layer** (Task 5) so `errors.py` stays free of formatting. `str(e)` = `"chat not found"` / `"chat is ambiguous"` (no names, safe if ever logged).

- [ ] **Step 1: Write failing tests** (`make_chat` fixtures, no Telegram):
  - `test_by_marked_id`, `test_by_bare_channel_id` (`"1234567890"` → `-1001234567890`), `test_by_bare_basic_group_id`, `test_negative_id_not_in_dialogs_is_not_found`
  - `test_by_username_case_insensitive` (`"@FamChat"`), `test_by_peer_username_for_user_chat`, `test_by_tme_link` (`"https://t.me/famchat"`), `test_unknown_username_suggests_close_ones`
  - `test_exact_title_beats_fragment` ("Family" vs "Family trip"), `test_unique_fragment` ("trip"), `test_tokens_any_order` ("trip family"), `test_cyrillic_case_insensitive` ("СІМ'Я" finds "Сім'я 🏠"), `test_emoji_and_punctuation_ignored`
  - `test_ambiguous_fragment_returns_candidates` (both chats in `e.candidates`, input order), `test_two_deleted_accounts_ambiguous`
  - `test_not_found_suggestions_ranked_and_capped_at_5`, `test_not_found_without_close_matches_has_empty_suggestions`
  - `test_match_chats_returns_all_in_order`, `test_blank_arg_is_not_found` (whitespace only)
  - `test_error_str_has_no_names`
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** `feat: chat resolver (id, @username, fuzzy title)`.

### Task 3: Telegram wrapper: dialogs and chat details

**Files:**
- Modify: `tg_bridge/telegram/mapping.py`, `tg_bridge/telegram/client.py`, `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`

**Interfaces:**
- Produces:
  - `mapping.chat_from_dialog(dialog) -> Chat` (uses `dialog.id`, `dialog.entity`, `dialog.date`, `dialog.folder_id == 1`, entity's `forum` flag)
  - `mapping.topic_from_forum_topic(t) -> Topic | None` (`None` for `ForumTopicDeleted`)
  - `ReadOnlyTelegram.list_dialogs() -> list[Chat]` — `iter_dialogs(ignore_migrated=True)`, collected inside `_call` so FloodWait maps to `RateLimitedError`; logs count + duration.
  - `ReadOnlyTelegram.get_chat_details(chat: Chat) -> ChatInfo` — `get_input_entity(chat.id)` then by kind: user/bot → `GetFullUserRequest` (`full_user.about`); group → `GetFullChatRequest(-chat.id)` (`full_chat.about`, `participants_count` from the returned chat); channel/supergroup → `GetFullChannelRequest` (`full_chat.about`, `full_chat.participants_count`); forum → `GetForumTopicsRequest(...)`. A Telethon `RPCError` other than flood on the GetFull/topics step → log the error **class name** + chat id, return `ChatInfo` with `description=None, members_count=None, topics=()`.
- Fake Telethon objects: `SimpleNamespace` dialogs/entities; extend `FakeTelethonClient` with `iter_dialogs` (async generator recording kwargs), `get_input_entity`, and `__call__(request)` recording request **type names** and returning canned results.

- [ ] **Step 1: Write failing mapping tests:** user, bot, deleted user (`first_name=None`) → `title == "Deleted Account"`, `deleted_peer`; basic group; supergroup; broadcast channel; forum supergroup; `ChatForbidden` / `ChannelForbidden` (no `username`, no `date` attrs → `None`); archived (`folder_id=1`); `dialog.date` naive → treated as UTC; deleted topic → `None`. Use real Telethon type classes where `isinstance` dispatch needs them (`telethon.tl.types.User`, `Chat`, `Channel`, `ChatForbidden`, `ChannelForbidden`, `ForumTopic`, `ForumTopicDeleted`) — constructing them is cheap and keeps mapping honest.
- [ ] **Step 2: Write failing client tests:** `test_list_dialogs_includes_archived_and_ignores_migrated` (kwargs: no `archived`/`folder`, `ignore_migrated=True`); `test_list_dialogs_flood_becomes_rate_limited`; `test_chat_details_channel_uses_full_channel`; `test_chat_details_basic_group_uses_positive_chat_id`; `test_chat_details_user_bio`; `test_chat_details_forum_lists_topics_and_total`; `test_chat_details_rpc_error_degrades`; `test_only_read_requests_used` (the recorded request type names ⊆ the allowed set from Global Constraints).
- [ ] **Step 3: Run** → fail. **Step 4: Implement.** **Step 5: Run** `uv run pytest -q` → pass, boundary test green.
- [ ] **Step 6: Commit** `feat: read dialogs and chat details from Telegram`.

### Task 4: LiveSource: cached dialogs + chat info

**Files:**
- Modify: `tg_bridge/source/live.py`, `tests/test_live_source.py`

**Interfaces:**
- Consumes: `ReadOnlyTelegram.list_dialogs`, `get_chat_details`.
- Produces: `LiveSource(telegram, clock=time.monotonic, dialogs_ttl=300.0)`; `list_dialogs(refresh=False)`, `get_chat_info(chat)`, `dialogs_age() -> float | None` (seconds since last fetch; used by the tools resolver helper).

- [ ] **Step 1: Write failing tests** (stub telegram counting calls, fake clock): `test_dialogs_cached_within_ttl`, `test_dialogs_refetched_after_ttl`, `test_refresh_bypasses_cache`, `test_concurrent_first_calls_fetch_once` (`asyncio.gather` of 3), `test_list_dialogs_connects_lazily`, `test_get_chat_info_not_cached`, `test_failed_fetch_is_not_cached` (stub raises once, next call retries), `test_logs_have_no_titles` (caplog).
- [ ] **Step 2: Run** → fail. **Step 3: Implement** (one lock for connect + cache is fine; keep `_ensure_connected` callable from inside it without re-locking). **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat: per-process dialog cache in LiveSource`.

### Task 5: Formatting

**Files:**
- Create: `tg_bridge/format.py`, `tests/test_format.py`

**Interfaces:**
- Produces:
  - `chat_line(chat: Chat, tz: ZoneInfo) -> str`, e.g.
    `-1001234567890 · supergroup · Family 🏠 · @famchat · last 2026-10-25 14:03 · forum · archived`
    `299701567 · user · Anna K · @anna_k · last 2026-10-26 09:12`
    `555 · user · Deleted Account · deleted_peer · last 2024-03-01 18:00`
    (omit absent parts; title kept as-is, emoji included)
  - `render_lines(header: str, lines: list[str], max_chars: int) -> str` — stops before exceeding `max_chars`, appends `(output truncated: narrow with query/kind)`.
  - `chat_info_block(info: ChatInfo, tz) -> str`:
    ```
    Family 🏠 — supergroup, forum, archived
    id -1001234567890 · @famchat · last message 2026-10-25 14:03
    Members: 12
    Description: …
    Topics (3 of 41): #1 General · #23 Trips · #40 Recipes (closed)
    ```
    For 1:1: a `Peer:` line with every available identity field (D9): name, `@username`, id, phone, `bot`, `deleted`. Unknown values print as `unknown`, absent ones are omitted.
  - `ambiguous_text(e: AmbiguousChatError, tz, max_chars) -> str` and `not_found_text(e: ChatNotFoundError, tz, max_chars) -> str` — first line `Chat "<arg>" is ambiguous; pass one of these ids:` / `No chat matches "<arg>". Closest:` (or `No chat matches "<arg>".` + hint to use `list_chats`), then up to 10 `chat_line`s and `…and N more; use the id`.
- [ ] **Step 1: Write failing tests** for each function incl. timezone conversion (UTC → Europe/Warsaw across the DST change on 2026-10-25), truncation exactly at the budget, 12 candidates → 10 shown + "and 2 more".
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** → pass.
- [ ] **Step 5: Commit** `feat: compact chat rendering`.

### Task 6: Tools + server registration

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`, `tests/test_tools.py`, `tests/test_server.py`

**Interfaces:**
- Produces:
  - `tools.resolve_chat_arg(source, arg, settings) -> Chat` — shared by every later tool: `resolve_chat(arg, await source.list_dialogs())`; on `ChatNotFoundError`, if `source.dialogs_age()` is `None` or `> 30`, retry once with `refresh=True`. Converts resolver errors to `ToolError(ambiguous_text/not_found_text)`. (`dialogs_age` is optional on the protocol: use `getattr(source, "dialogs_age", None)`; a source without it never refreshes.)
  - `tools.list_chats(source, settings, query=None, kind=None, limit=30) -> str` — header `Chats (N shown of M)` / `Chats matching "q" (N shown of M)`; empty result → `No chats match "q".`
  - `tools.get_chat_info(source, settings, chat: str) -> str`
  - A small `_tool_errors()` context manager replacing the try/except in `whoami`, so every tool maps `TgBridgeError` → `ToolError` the same way.
  - `build_server(source, settings)` registers `list_chats` and `get_chat_info` with `READ_ONLY` annotations and docstrings written for a ~30B model, e.g.:
    - `list_chats`: "List the user's Telegram chats (private, groups, channels, bots, incl. archived), newest first. `query`: words from the chat title or @username. `kind`: user, bot, group (incl. supergroups), supergroup, channel. Returns one line per chat starting with its numeric id; pass that id to other tools."
    - `get_chat_info`: "Details of one chat: type, description, member count, the other person's identity for private chats, forum topics. `chat`: numeric id, @username, or words from the title."
  - `main()` passes `settings` into `build_server`.
- [ ] **Step 1: Write failing `test_tools.py`** with `FakeSource`: list all / with query / with kind `group` covering supergroups / invalid kind → ToolError naming the allowed values / limit clamp (0 → 1, 500 → 200) / empty match; `get_chat_info` by id, by fragment, ambiguous → ToolError text starts with `Chat "fam" is ambiguous`, not found → contains `Closest:`; resolver refresh-once-when-stale and no refresh when fresh (FakeSource with `dialogs_age`); `test_tools_do_not_log_queries` (caplog).
- [ ] **Step 2: Write failing `test_server.py`** additions: tool names == `["whoami", "list_chats", "get_chat_info"]` (order-insensitive), `call_tool("list_chats", {"query": "family"})` returns the tools-layer text. Update the existing `build_server(...)` calls for the new `settings` arg (use `load_settings(env=CREDS, config_file=tmp)`).
- [ ] **Step 3: Run** → fail. **Step 4: Implement.** **Step 5: Run** `uv run pytest -q` → all pass (incl. `test_server_process.py`).
- [ ] **Step 6: Commit** `feat: list_chats and get_chat_info tools`.

### Task 7: Smoke + docs

- [ ] **Step 1:** Restart LM Studio (it relaunches the server), ask "show my chats with 'family' in the name", then "tell me about <an ambiguous name>". Hand the brief's smoke checklist to the maintainer.
- [ ] **Step 2:** If the dialog fetch on the real account is slow (> 5 s), note the observed time in `docs/setup.md` (new line under §6 or a §7 "Performance"), so later features can decide on TTL.
- [ ] **Step 3: Run** `uv run pytest -q` → green. Propose commit `docs: feature 01 notes`.
