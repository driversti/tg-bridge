# 07 — Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The model knows the user's chat folders: `list_folders()`, `list_chats(folder=…)` in the folder's order, and `Folders: …` in `get_chat_info`. Membership (explicit + rule-based) is computed by one pure function over the cached dialog list.

**Architecture:** Telethon-free logic lives in a new `tg_bridge/folders.py` (membership, ordering, resolution, rule summary). `telegram/mapping.py` turns `DialogFilter`/`DialogFilterChatlist` into a domain `Folder` and adds the dialog state the rules need (`is_muted`, `is_unread`, `has_unread_mention`, `Person.is_contact`) to `Chat`. `LiveSource` fetches dialogs and folders in one locked refresh and caches both (memory + disk, same TTL). Tools and format stay thin.

**Tech Stack:** unchanged (Python ≥3.12, Telethon 1.45 = layer 229, `mcp`, `pytest-asyncio`).

**Spec:** `docs/features/07-folders.md` + `docs/design.md` §6 (models), §7 (chat args, budget, errors), §9. Patterns to copy: `resolve.py` (pure resolution + `AmbiguousChatError`/`ChatNotFoundError`), `dialog_cache.py` (atomic JSON), `test_client.py` (`dlg`, `ALLOWED_REQUESTS`).

## Global Constraints

All constraints from plans 00–09 still apply. Additionally:

- **Read-only:** the only new Telegram calls are `messages.GetDialogFilters` and `account.GetNotifySettings` (both pure reads). Add both to `ALLOWED_REQUESTS` in `tests/telegram/test_client.py`; nothing else.
- **Only `tg_bridge/telegram/` imports Telethon.** `folders.py` works on domain models only.
- **Logging:** counts and ids only. Folder titles are user content: never log them.
- **Budget:** every new output goes through `render_lines` (or stays tiny) and respects `settings.max_chars`.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here (Claude's calls)

- **Membership rule = Telegram Desktop's `ChatFilter::contains`**, so counts match the app:
  1. chat id in `exclude_ids` → **out** (beats everything, incl. pinned/include);
  2. chat id in `pinned_ids` or `include_ids` → **in** (ignores `exclude_muted/read/archived`);
  3. otherwise in only if its **type flag** is set (`user` → `contacts` or `non_contacts` by `peer.is_contact`; `bot` → `bots`; `group`/`supergroup` → `groups`; `channel` → `broadcasts`) **and**
     - `exclude_muted` → not muted, *or* muted but has an unread mention and is not archived (Desktop keeps those);
     - `exclude_read` → `is_unread`;
     - `exclude_archived` → not archived.
  Shared folders (`DialogFilterChatlist`) have no rules and no excludes: explicit only.
- **Chat state added to the domain** (defaults keep every existing constructor and fake valid):
  - `Person.is_contact: bool = False` (from `tl.User.contact`).
  - `Chat.is_muted: bool = False`: `dialog.dialog.notify_settings.mute_until > now`. When the chat has no own `mute_until`, fall back to the account default for its type, fetched once per dialog fetch with `account.GetNotifySettings(InputNotifyUsers | InputNotifyChats | InputNotifyBroadcasts)` (users+bots / basic groups+supergroups / channels). A failing default fetch logs a warning and counts as "not muted".
  - `Chat.is_unread: bool = False`: `unread_count > 0 or unread_mark or unread_mentions_count > 0 or unread_reactions_count > 0`.
  - `Chat.has_unread_mention: bool = False`: `unread_mentions_count > 0`.
  `chat_line`/`chat_info_block` do **not** print these (no budget creep); `list_chats(folder=)` doesn't either.
- **`Folder` model** (`source/models.py`):
  ```python
  FolderKind = Literal["custom", "shared"]

  @dataclass(frozen=True)
  class Folder:
      id: int
      title: str             # plain text, custom emoji removed
      emoticon: str | None
      kind: FolderKind
      contacts: bool; non_contacts: bool; groups: bool; broadcasts: bool; bots: bool
      exclude_muted: bool; exclude_read: bool; exclude_archived: bool
      pinned_ids: tuple[int, ...]   # marked ids, folder order
      include_ids: tuple[int, ...]
      exclude_ids: tuple[int, ...]
  ```
- **Peer ids:** `utils.get_peer_id(input_peer)` gives marked ids for `InputPeerUser/Chat/Channel`. `InputPeerSelf` → the own id from `client.get_me(input_peer=True)` (Telethon caches it). Unknown peer types are skipped with a warning (count only).
- **Title:** layer 229 gives `TextWithEntities`. Drop the spans of `MessageEntityCustomEmoji` (offsets are UTF-16: work on `telethon.helpers.add_surrogate(text)`, cut the spans back to front, then `del_surrogate`), collapse whitespace, strip. A plain `str` title (older layers / fakes) is used as is. Empty result → `"(folder <id>)"`.
- **Order:** folders in `GetDialogFilters` order (= app sidebar order), `DialogFilterDefault` skipped. The response is `messages.DialogFilters(filters=…)`; also accept a bare list.
- **Chats in a folder** (`folder_chats`): members whose id is in `pinned_ids`, in that order, then the other members by `last_message_at` descending (`None` last; stable). Archived members stay in place (the app shows them inside the folder). Folder peers that are not in the dialog list (left chats) are ignored everywhere, including counts.
- **Folder argument** (`resolve_folder(arg, folders)`): all digits → exact id match; otherwise `normalize()` title: exact normalised title first, then every folder whose title contains all query tokens. One hit → it; several → `AmbiguousFolderError` (candidates = folder lines); none → `FolderNotFoundError` whose text lists all folders (there are few). A digit-only arg that matches no id falls through to title matching (folder named "2025").
- **`list_folders()` output:**
  ```
  Folders (4)
  2 · WAR 🪖 · custom · 34 chats · 7 unread · 30 included, 4 pinned
  3 · Unread · custom · 12 chats · 12 unread · rules: contacts+non-contacts+groups+channels+bots, excl. read+archived
  4 · Work · custom · 20 chats · 0 unread · rules: groups, excl. muted · 2 included, 1 excluded
  5 · F1 · shared · 18 chats · 3 unread · 18 included
  ```
  Line = `id · title [emoticon] · kind · N chats · M unread · summary`. `rule_summary(folder)`: `rules: <flags joined by +>[, excl. <muted+read+archived>]` when any type flag is set, then explicit counts `N included, N pinned, N excluded` (zero parts omitted), parts joined by ` · `. A folder with neither → `empty`. No folders → `No chat folders.`
- **`list_chats(folder=…)`** combines with `query`/`kind`/`limit`: folder members in folder order, then the existing kind/query filters. Header `Chats in folder "WAR" (30 shown of 34)` (+ `matching "x"` when a query is given), then the usual `Totals:` line. No archived-last re-sort inside a folder. Empty folder → `Folder "WAR" has no chats.`
- **`get_chat_info`:** one extra line after the identity lines: `Folders: WAR, F1` or `Folders: none`. Folder titles are joined in folder order. If folders cannot be loaded (`TgBridgeError`), omit the line rather than fail the call.
- **Cache:** folders are fetched in the same refresh as dialogs (one lock, one `refresh` flag) and saved in the same disk file. `dialog_cache.VERSION = 2` (old files are a miss: they lack folders and chat state). `DialogStore.load` returns `(chats, folders, saved_at)`; `save(chats, folders, now)`.
- **Staleness accepted:** rule-based folders ("Unread") reflect state up to the dialog TTL (5 min) old. No `refresh` argument on the tools; the smoke item says "within 5 min". Log it as a deviation if the maintainer finds that annoying in the smoke test.

## File Structure

| File | Change |
|---|---|
| `tg_bridge/source/models.py` | `Folder`, `FolderKind`; `Person.is_contact`; `Chat.is_muted/is_unread/has_unread_mention` |
| `tg_bridge/source/__init__.py` | export `Folder`, `FolderKind` |
| `tg_bridge/folders.py` (new) | `folder_contains`, `folder_chats`, `chat_folders`, `resolve_folder`, `rule_summary` |
| `tg_bridge/errors.py` | `FolderNotFoundError(arg, folders)`, `AmbiguousFolderError(arg, candidates)` |
| `tg_bridge/telegram/mapping.py` | dialog state in `chat_from_dialog`, `is_contact`, `folder_from_tl`, `_plain_title` |
| `tg_bridge/telegram/client.py` | notify defaults in `list_dialogs`, new `list_folders()` |
| `tg_bridge/source/protocol.py`, `source/live.py`, `source/dialog_cache.py` | `list_folders`, shared refresh, cache v2 |
| `tg_bridge/format.py` | `folder_line`, folder header/empty texts, `Folders:` line |
| `tg_bridge/tools.py`, `tg_bridge/server.py` | `list_folders` tool, `folder` param on `list_chats`, folders in `get_chat_info` |
| `tests/fakes.py` | `make_folder`, `FakeSource(folders=…)` + `list_folders` |
| `tests/test_folders.py` (new), `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`, `tests/test_live_source.py`, `tests/test_dialog_cache.py`, `tests/test_tools.py`, `tests/test_server.py` | tests below |

## Review Focus

1. **Precedence:** exclude beats pinned/include; include ignores `exclude_read/muted/archived` (Task 1).
2. **Muted with unread mention** stays in an `exclude_muted` folder only when not archived (Task 1).
3. **Mute default fallback:** a chat without own `mute_until` in a globally muted category counts as muted (Task 2).
4. **UTF-16 offsets** when removing custom emoji from a title that also contains a normal emoji before it (Task 2).
5. **Cache v1 file** is a miss, not a crash; folders survive a disk round trip (Task 3).

---

### Task 1: Domain model and pure folder logic

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/__init__.py`, `tg_bridge/errors.py`, `tests/fakes.py`
- Create: `tg_bridge/folders.py`, `tests/test_folders.py`

**Interfaces:**
- Produces:
  ```python
  def folder_contains(folder: Folder, chat: Chat) -> bool
  def folder_chats(folder: Folder, chats: Sequence[Chat]) -> list[Chat]      # folder order
  def chat_folders(chat: Chat, folders: Sequence[Folder]) -> list[Folder]   # folder order
  def resolve_folder(arg: str, folders: Sequence[Folder]) -> Folder
  def rule_summary(folder: Folder) -> str
  # tests/fakes.py
  def make_folder(**overrides) -> Folder  # default: id=2, "WAR", custom, all flags False, empty id tuples
  ```

- [ ] **Step 1: Failing tests** in `tests/test_folders.py` (chats from `make_chat`/`make_user_chat`):
  - explicit: `test_include_id_is_member`, `test_pinned_id_is_member`, `test_unlisted_chat_not_member_without_rules`.
  - precedence: `test_exclude_beats_include`, `test_exclude_beats_pinned`, `test_exclude_beats_rule`, `test_include_ignores_exclude_read_muted_archived`.
  - each type flag: `contacts` (user with `is_contact=True` in, non-contact out), `non_contacts` (reverse), `bots` (bot in, user out — a bot contact is still a bot), `groups` (group and supergroup in, channel out), `broadcasts` (channel in, supergroup out).
  - filters: `test_exclude_read_keeps_only_unread`, `test_exclude_archived_drops_archived`, `test_exclude_muted_drops_muted`, `test_exclude_muted_keeps_muted_with_unread_mention`, `test_exclude_muted_drops_archived_muted_with_mention`.
  - `test_shared_folder_is_explicit_only`.
  - `folder_chats`: pinned order first, then `last_message_at` desc, `None` last; peers missing from the dialog list ignored.
  - `chat_folders`: a chat in two folders returns both in folder order.
  - `resolve_folder`: by id, by exact title (case/emoji-insensitive: `"war"` → `"WAR 🪖"`), by fragment, ambiguous → `AmbiguousFolderError`, none → `FolderNotFoundError`, `"2025"` title when no id 2025.
  - `rule_summary`: the four example lines' summaries above, and `empty`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_folders.py` → FAIL.
- [ ] **Step 3: Implement** models (new fields with defaults, after existing ones), errors (same shape as the chat errors), `folders.py` (import `normalize` from `resolve`).
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): Folder model and membership rules`.

### Task 2: Telegram mapping and client reads

**Files:**
- Modify: `tg_bridge/telegram/mapping.py`, `tg_bridge/telegram/client.py`
- Test: `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`

**Interfaces:**
- Produces:
  ```python
  # mapping.py
  def chat_from_dialog(dialog, muted_by_default: Mapping[str, datetime | None] = {}, now: datetime | None = None) -> Chat
  #   muted_by_default keys: "users", "chats", "broadcasts" → default mute_until
  def folder_from_tl(raw, self_id: int) -> Folder | None   # None for DialogFilterDefault / unknown types
  # client.py
  async def list_folders(self) -> list[Folder]
  ```
  (Exact signatures are the builder's call; keep `now` injectable for tests and use `datetime.now(UTC)` in production.)

- [ ] **Step 1: Failing tests:**
  - mapping: `is_contact` from `tl.User(contact=True)`; `is_unread` from each of `unread_count`, `unread_mark`, mentions, reactions; `has_unread_mention`; `is_muted` from own `mute_until` in the future / past; fallback to the category default (user→users, bot→users, group & megagroup→chats, broadcast→broadcasts) when own `mute_until` is `None`.
  - `folder_from_tl`: `DialogFilter` with all flags → fields; `DialogFilterChatlist` → `kind="shared"`, rule flags False, `exclude_ids=()`; `DialogFilterDefault` → `None`; `InputPeerSelf` → `self_id`; `InputPeerChannel(channel_id=5)` → `-1000000000005`.
  - title: `TextWithEntities("🔥 F1 ⭐", [MessageEntityCustomEmoji(offset=<utf16 of ⭐>, length=1, document_id=1)])` → `"🔥 F1"` (custom emoji gone, the normal emoji before it kept); plain `str` passes through; empty → `"(folder 7)"`.
  - client: `list_folders` returns folders in response order, skips the default; accepts both `messages.DialogFilters` and a bare list; `list_dialogs` asks `GetNotifySettings` for the three categories and marks a chat muted by default; a failing defaults request still returns dialogs (not muted); `{type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS` with `GetDialogFiltersRequest`, `GetNotifySettingsRequest` added.
  - extend the `dlg()` helper with an optional raw `dialog` namespace (`notify_settings`, `unread_count`, `unread_mark`, `unread_mentions_count`, `unread_reactions_count`); `chat_from_dialog` must tolerate a missing `dialog.dialog` (existing tests) via `getattr` defaults.
- [ ] **Step 2: Run** `uv run pytest -q tests/telegram` → FAIL.
- [ ] **Step 3: Implement.** All RPCs through `self._call` (FloodWait handling). Log `fetched %d folders in %.2f s`.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): read dialog filters and chat state from Telegram`.

### Task 3: Source protocol, LiveSource, disk cache

**Files:**
- Modify: `tg_bridge/source/protocol.py`, `tg_bridge/source/live.py`, `tg_bridge/source/dialog_cache.py`, `tests/fakes.py`
- Test: `tests/test_live_source.py`, `tests/test_dialog_cache.py`

**Interfaces:**
- Produces:
  ```python
  # MessageSource
  async def list_folders(self, *, refresh: bool = False) -> list[Folder]:
      """The user's chat folders in app order (no "All Chats"), from the same cache as list_dialogs."""
  # DialogStore / DialogCache
  def load(self, now: float, max_age: float) -> tuple[list[Chat], list[Folder], float] | None
  def save(self, chats: list[Chat], folders: list[Folder], now: float) -> None
  # FakeSource(folders=[...]) with list_folders recording its refresh flag like list_dialogs
  ```

- [ ] **Step 1: Failing tests:**
  - LiveSource: `list_dialogs()` then `list_folders()` within TTL → one dialog fetch and one folder fetch in total; after TTL both refetched together; a failed refresh leaves the old dialogs **and** folders; concurrent first calls of both → one connect, one fetch each.
  - cache: round trip of chats with the new state fields and folders (tuples come back as tuples); a `version: 1` file → `None`; corrupt folder entry → `None` with a warning.
- [ ] **Step 2: Run** the two test files → FAIL.
- [ ] **Step 3: Implement.** Refactor `list_dialogs` into a private `_snapshot(refresh)` that returns `(chats, folders)` under the lock; both public methods use it. Folder fetch happens right after the dialog fetch; if it raises, nothing of this refresh is stored.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): cache folders with the dialog list`.

### Task 4: Tools, format, server

**Files:**
- Modify: `tg_bridge/format.py`, `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py`, `tests/test_format.py`, `tests/test_server.py`

**Interfaces:**
- Produces:
  ```python
  async def list_folders(source, settings) -> str
  async def list_chats(source, settings, query=None, kind=None, limit=30, folder: str | None = None) -> str
  # format.py
  def folder_line(folder: Folder, chats: Sequence[Chat]) -> str
  ```
  Server docstrings (keep them short, they cost tokens on every request):
  - `list_folders()`: "List the user's chat folders in app order: id, title, chat count, unread chats, and what the folder contains (rules and/or hand-picked chats). Use the id or title with list_chats(folder=…)."
  - `list_chats`: add "`folder`: folder id or title words (see list_folders): only that folder's chats, in the folder's order."
  - `get_chat_info`: add "and the folders the chat is in".

- [ ] **Step 1: Failing tests:**
  - `list_folders`: the example output above (exact text) from a `FakeSource` with four folders; `No chat folders.`; output within `max_chars` for 40 folders (truncation note).
  - `list_chats(folder=…)`: folder order (pinned first), combined with `kind` and `query`, header text, `limit`, unknown folder → `ToolError` listing folders, ambiguous → candidates, empty folder text, archived member not moved to the end.
  - `get_chat_info`: `Folders: WAR, F1`; `Folders: none`; line omitted when `list_folders` raises `RateLimitedError`.
  - server: `list_folders` is registered with `READ_ONLY` annotations and its result starts with `Now:`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** Map `FolderNotFoundError`/`AmbiguousFolderError` to `ToolError` in the tool (same style as `_not_found`/`ambiguous_text`).
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): list_folders tool, list_chats(folder=), folders in get_chat_info`.

### Task 5: Docs and smoke

- [ ] `docs/design.md` §7 table: add `list_folders()` and `folder?` on `list_chats`; `get_chat_info` returns folders.
- [ ] Brief smoke checklist: the "Unread" item reads "…its count shrinks within 5 min (dialog cache TTL) after reading one in the app"; add "A chat muted only through the global notification defaults is left out of an `exclude_muted` folder, as in the app."
- [ ] `docs/roadmap.md`: row 07 already links the plan and is `[~]`; it becomes `[x]` after the maintainer's smoke run.
- [ ] Commit `docs: feature 07 smoke checklist and tool contract`.
