# 08 — Folder Queries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ask about a whole folder in one call: `search_messages(query, folder=…)`, `get_messages(folder=…, period…)`, and a compact `mode="headlines"` digest where the same news reposted across channels collapses into one `×N` line.

**Architecture:** No new Telegram requests: folder membership comes from 07 (`folder_chats`), every chat is read with the existing `get_messages` / per-chat `search_messages` of the source, **sequentially**. New Telethon-free modules: `headlines.py` (headline extraction + repost grouping, pure), `many.py` (multi-chat read and search over a `MessageSource`), `format_many.py` (rendering of multi-chat pages). A new `r…` read cursor pages the merged list; the search cursor gets a folder scope. `tools.py`/`server.py` stay thin.

**Tech Stack:** unchanged (Python ≥3.12, Telethon 1.45, `mcp`, `pytest-asyncio`).

**Spec:** `docs/features/08-folder-queries.md` + `docs/design.md` §7 (period args, compact line, budget, errors), §9. Patterns to copy: `render_page` (budget loop), `export.py` (deadline with injectable `clock`), `cursor.py` (short tokens), `tests/test_tools_search.py`, `tests/test_tools_folders.py`.

## Global Constraints

All constraints from plans 00–09 still apply. Additionally:

- **Read-only, no new Telegram calls.** `ALLOWED_REQUESTS` in `tests/telegram/test_client.py` stays as it is.
- **Pure data provider (D1):** nothing here calls an LLM. Shrinking = headline cut + repost grouping only.
- **Only `tg_bridge/telegram/` imports Telethon.** `headlines.py`, `many.py`, `format_many.py` work on domain models.
- **Sequential:** chats are read one after another (`for chat in chats: await …`), never `asyncio.gather`. FloodWait handling stays in the client (≤30 s sleeps, longer → `RateLimitedError`).
- **Logging:** counts, chat ids and durations only (`log.info("read %d chats, %d messages in %.1f s", …)`). Never titles or text.
- **Budget:** every result ≤ `settings.max_chars`. Never silently truncate: a page that does not show everything says so with totals and a `Next:` call.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here (Claude's calls)

- **Arguments**
  - `get_messages(chat=None, folder=None, …, mode="full")`: exactly one of `chat`/`folder` unless a cursor is given (the cursor carries its scope). `mode` ∈ `full` | `headlines` (casefolded, stripped).
  - Folder reads and every headlines read **need a period** (`period`/`days`/`since`/`until`). "Latest N across 18 chats" has no good meaning, and an unbounded headline digest is what the volume guard exists for.
  - `folder` with `topic` or `around` → error. `from_me` works everywhere.
  - `search_messages(query, chat=None, folder=None, …)`: `chat` and `folder` are mutually exclusive.
  - Folder argument resolution = `_resolve_folder_arg` from 07 (id or title words, same errors).
  - **Max folder size:** `MAX_FOLDER_CHATS = 50`. A bigger folder (e.g. rule-based "Unread") → error, since every page costs one request stream per chat.
- **Exact error texts** (all `ToolError`):
  - both: `Pass either chat or folder, not both.`
  - neither: `Pass chat or folder.`
  - folder without period: `Reading a folder needs a period, e.g. period="today", period="this_week" or days=3.`
  - headlines without period: `mode="headlines" needs a period, e.g. period="this_week" or days=7.`
  - bad mode: `Unknown mode "x". Allowed: full, headlines.`
  - folder + topic/around: `topic and around work with one chat, not with a folder.`
  - too big: `Folder "F1" has 312 chats; folder queries handle up to 50. Use a smaller folder, or search without folder.`
  - empty folder: `Folder "F1" has no chats.`
  - over cap: `More than 2000 messages in this period across 18 chats of folder "F1". Pass a shorter period.` (single chat: `… in "<title>". Pass a shorter period.`)
  - deadline: `Reading took too long: 7 of 18 chats read in 40 s. Pass a shorter period or use a smaller folder.`
- **Multi-chat read ("read-all") pipeline** (folder reads in both modes, and single-chat headlines):
  1. For each chat in folder order: `source.get_messages(chat, MessageQuery("forward", start, end, None, None, from_me, remaining + 1))`, `remaining = MAX_READ_MESSAGES - len(collected)`. `MAX_READ_MESSAGES = 2000`. More than the cap in total → over-cap error (nothing partial).
  2. Deadline: `READ_DEADLINE = 40.0` s on an injectable `clock` (LM Studio cuts a tool call at ~60 s, see 04's log), checked before each chat → deadline error.
  3. Merge key `(date, chat_id, id)` ascending. Every page **re-reads the whole period** and recomputes grouping; the cursor only says where the previous page stopped. Deterministic because the cursor fixes `start`/`end`, and grouping never depends on where a page starts.
  4. Page = items with key > cursor boundary, as many as fit `limit` (default `DEFAULT_MESSAGES` = 100, max `MAX_MESSAGES`) and the budget; the boundary is the **last shown item's** key (for a group: its first post).
- **Headlines**
  - Only messages with `action is None` and `headline(text)` not None. Media-only posts have `text == ""` → skipped; captions are `text`, so they stay.
  - `headline(text)`: the first line that, after removing URLs (`https?://\S+`, `t\.me/\S+`), `#hashtags` and `@mentions`, still has **≥ 3 word characters** (`\w`). Return that line with URLs removed, `one_line`d, whitespace collapsed. Longer than `HEADLINE_CHARS = 140` → cut at the last space in `[100, 140)`, else hard at 139, then `…`.
  - **Repost grouping** (`group_reposts`): chronological greedy clustering.
    - Tokens: casefold the whole text, drop URLs and `@mentions`, `re.findall(r"\w+")`, drop tokens shorter than 2, cut each to its first 6 characters (cheap stemming: `contract`/`contracts` → `contra`; folds Ukrainian/Polish endings well enough), keep the **first 60** as a `frozenset`.
    - `similar(a, b)`: if `min(len(a), len(b)) < 5` → `a == b and bool(a)`; else `len(a & b) / min(len(a), len(b)) >= 0.7` (overlap coefficient: a short repost of a long post still matches).
    - A message joins the **first** existing group that (a) started ≤ 72 h before it, (b) has no message from the same chat yet, and (c) whose **first** post is `similar`. Otherwise it starts a group. Same-chat posts never merge (templated posts like "FP1 results" / "FP2 results" in one channel stay apart).
    - Single-chat headlines get the same function; with one chat nothing can merge.
  - Group line (`HH:MM` under day headers, like `render_page`):
    - single: `14:03 F1 News #4812: Verstappen signs a new contract…`
    - repost: `14:03 ×3 F1 News #4812, Motorsport UA #911, P1 #77: Verstappen signs a new contract…` (headline of the first post; labels in chronological order).
    - single-chat headlines omit the label: `14:03 #4812: …`.
- **Full-mode multi-chat line:** `_message_line` with a chat label: `#4812 14:03 F1 News: text` when the sender text equals the label (channel posts), else `#4812 14:03 F1 Paddock › Anna (@anna_k): text`.
- **Chat labels:** `one_line(title)` cut to `LABEL_CHARS = 20` (`…` when cut). Two chats with the same label get ` ~<last 3 digits of id>` appended to both.
- **Legend** (multi-chat pages): one header line per chat that appears **on this page**: `F1 News = -1001234567890 · Links: https://t.me/f1news/<id>` (the `Links:` part only when `chat_link_base` exists). This is how the model gets chat ids for `around`/`download_media` and links for answers.
- **Head + totals line** (multi-chat):
  ```
  Folder: F1 (5) · 18 chats · this week, 2026-10-26 → now (Europe/Warsaw)
  412 posts in 6 of 18 chats → 300 headlines (61 repeats merged, 51 without text skipped); showing 1–88.
  F1 News = -1001234567890 · Links: https://t.me/f1news/<id>
  …
  ```
  Full mode totals: `412 messages in 6 of 18 chats; showing 1–90.` Parenthesised parts with a zero count are omitted. `showing a–b`: `a` = 1 + number of items with key ≤ the cursor boundary. Single-chat headlines head = `messages_head(...)` + `Mode: headlines` line + `412 posts → 380 headlines (32 without text skipped); showing 1–88.`
  Footer (as `render_page`): `More remain. Next: call get_messages(folder="5", cursor="r…")` or `End of period.`; empty → `No messages in this period.` / `No posts with text in this period.`
- **Read cursor** (`cursor.py`): `r{message_id}_d{epoch}_{u|n}{abs(chat_id)}[_s{epoch}][_e{epoch}][_k{folder_id}][_h][_m]` = boundary (message, date, chat), period, folder (absent = single chat, the boundary's chat), headlines flag, from_me. `get_messages` treats a cursor starting with `r` as a read cursor; anything else goes to the existing `decode_cursor`. Conflicts: `folder` arg resolving to another id → `CursorError(other_folder=True)`; `chat` arg ≠ cursor's chat → `CursorError(other_chat=True)`; a folder cursor with a `chat` arg, or a chat cursor with a `folder` arg → `CursorError()` (invalid). Missing folder (deleted since) → `FolderNotFoundError` text as in 07.
- **Folder search**
  - Per chat, in folder order: `SearchQuery(text, start, end', from_me, limit + 1, after=None)` with `end' = min(end, boundary.date + 1 s)` when continuing (else `end`); hits with key `(date, chat_id, id) >= boundary key` are dropped.
  - Merge newest first by `(date, chat_id, id)` descending. **Horizon rule** (keeps order correct when one chat has more hits than it returned): among chats whose raw answer was full (`len == limit + 1`), take the newest of their oldest returned hits = horizon; drop merged hits older than the horizon (they come on the next page). `more = len(merged) > limit or a horizon cut anything`.
  - Search cursor: new scope `"folder"`, head `o`, extra field `k{folder_id}`; `SearchCursor` gains `folder_id: int | None = None`. A folder cursor with `chat` → `ToolError("This cursor is from a folder search; call search_messages with folder or only the cursor.")`; folder arg mismatch → `CursorError(other_folder=True, tool="search_messages")`; folder omitted → taken from the cursor.
  - Rendering = existing `render_hits` with `chats` = folder members (grouped by chat), head `Search "x" in folder "F1" (18 chats) · <period>`. Hint: `call search_messages(query="x", folder="5", cursor="o…")`.
- **Telethon `wait_time`:** our iterators pass `limit=None` (they stop by hand), and Telethon then sleeps **1 s between every history page** (`wait_time = 1 if limit > 3000`). A folder read would spend most of its 40 s sleeping. `ReadOnlyTelegram.get_messages` and `_search_chat` pass `wait_time=0` when `query.limit <= 3000` (Telethon's own no-wait threshold); FloodWait handling is unchanged. Log it under "Log" in the roadmap.

## File Structure

| File | Change |
|---|---|
| `tg_bridge/headlines.py` (new) | `headline`, `repost_tokens`, `similar`, `Repost`, `group_reposts` |
| `tg_bridge/many.py` (new) | `read_chats`, `search_chats`, `ReadResult`, errors raised as `TgBridgeError` subclasses |
| `tg_bridge/format_many.py` (new) | `chat_labels`, `legend_lines`, `render_many` (full + headlines, budget loop) |
| `tg_bridge/cursor.py` | `ReadCursor` + encode/decode; `SearchCursor.folder_id`, scope `"folder"` |
| `tg_bridge/errors.py` | `CursorError(other_folder=…)`; `TooManyMessagesError`, `ReadTimeoutError` |
| `tg_bridge/format.py` | `_message_line(…, label=None)`; `search_head(…, folder=None, folder_size=None)` |
| `tg_bridge/telegram/client.py` | `wait_time=0` for limits ≤ 3000 |
| `tg_bridge/tools.py`, `tg_bridge/server.py` | `folder`/`mode` on `get_messages`, `folder` on `search_messages`, docstrings |
| `docs/design.md` §7 | table rows for the new arguments |
| tests | `tests/test_headlines.py`, `tests/test_many.py`, `tests/test_format_many.py`, `tests/test_tools_folder_read.py`, `tests/test_tools_folder_search.py` (new); `tests/test_cursor.py`, `tests/telegram/test_client.py`, `tests/telegram/test_client_search.py`, `tests/test_server.py` |

## Review Focus

1. **Paging is stable:** reading page 2 with the cursor from page 1 shows no item twice and skips none, including a repost group whose later members fall after the page boundary (Task 5: `test_folder_headlines_pages_cover_everything_once`).
2. **Same-second ties across chats** in both folder read and folder search: two messages with the same `date` in different chats on a page boundary are both shown exactly once (Task 3: `test_search_chats_same_second_tie_on_boundary`; Task 5 page test uses equal dates).
3. **Horizon rule:** one busy chat with more than `limit` hits plus a quiet chat with old hits: the old hits do not jump ahead of the busy chat's unseen newer hits (Task 3).
4. **No false merges:** two different news items that share names (`Norris wins…` / `Norris signs…`) stay apart; same-chat look-alikes stay apart (Task 1).
5. **A folder with a muted or archived chat** reads it like `list_chats(folder=…)` lists it (Task 5: `test_folder_read_includes_archived_member`).

---

### Task 1: Headlines and repost grouping (pure)

**Files:**
- Create: `tg_bridge/headlines.py`, `tests/test_headlines.py`

**Interfaces:**
- Produces:
  ```python
  HEADLINE_CHARS = 140
  def headline(text: str) -> str | None
  def repost_tokens(text: str) -> frozenset[str]
  def similar(a: frozenset[str], b: frozenset[str]) -> bool

  @dataclass(frozen=True)
  class Repost:
      posts: tuple[Message, ...]   # chronological, first = the one shown; len 1 = no repost
      @property
      def first(self) -> Message: ...

  def group_reposts(messages: Sequence[Message]) -> list[Repost]   # input chronological; output by first post
  ```

- [ ] **Step 1: Failing tests** (`make_message` from `tests/fakes.py`):
  - `headline`: `"BREAKING\nVerstappen signs"` → `"BREAKING"` (≥ 3 word chars); `"🔥🔥\n— — —\nVerstappen signs"` → `"Verstappen signs"`; `"#F1 #news\nhttps://t.me/x/1\nNorris wins"` → `"Norris wins"`; `"Read: https://x.com/a more"` → `"Read: more"`; `""` → `None`; `"🔥 https://t.me/x"` → `None`; a 300-char line with spaces → length ≤ 140, ends with `…`, cut at a space; a 300-char line without spaces → exactly 140 chars incl. `…`.
  - `repost_tokens`: casefolded, URLs/mentions gone, tokens cut to 6 chars, at most 60, 1-char tokens dropped.
  - `similar`: the example pair `"Verstappen signs new contract with Red Bull until 2028"` / `"Max Verstappen has signed a new contract with Red Bull until 2028!"` → True; `"Norris wins the Mexican GP ahead of Leclerc"` / `"Norris signs a new contract with McLaren until 2030"` → False; short `{"ok"}` vs `{"ok"}` → True, `{"ok"}` vs `{"ok","go"}` → False.
  - `group_reposts`: three channels post the contract news with small wording changes within 2 h → one group of 3, first = earliest; the same text 4 days later → a new group; two similar posts **in the same chat** → two groups; a post similar to a group that already has its chat → new group; output ordered by first post; empty input → `[]`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_headlines.py` → FAIL (module missing).
- [ ] **Step 3: Implement** per "Decisions → Headlines". Constants: `HEADLINE_CHARS = 140`, `MIN_WORD_CHARS = 3`, `TOKEN_CHARS = 6`, `MAX_TOKENS = 60`, `MIN_TOKENS = 5`, `OVERLAP = 0.7`, `REPOST_WINDOW = timedelta(hours=72)`. Groups are compared by their first post's tokens (compute tokens once per message).
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(headlines): headline extraction and repost grouping`.

### Task 2: Cursors and errors

**Files:**
- Modify: `tg_bridge/cursor.py`, `tg_bridge/errors.py`
- Test: `tests/test_cursor.py`

**Interfaces:**
- Produces:
  ```python
  @dataclass(frozen=True)
  class ReadCursor:
      after: SearchOffset        # boundary: the last shown item (chat_id, message_id, date)
      start: datetime | None
      end: datetime | None
      folder_id: int | None      # None = single chat, after.chat_id
      headlines: bool
      from_me: bool
  def is_read_cursor(s: str) -> bool                 # stripped/unquoted token starts with "r<digit>"
  def encode_read_cursor(c: ReadCursor) -> str
  def decode_read_cursor(s: str) -> ReadCursor       # CursorError() when malformed
  # SearchCursor gains: folder_id: int | None = None; scope Literal["chat", "global", "folder"]
  # errors.py
  CursorError(other_chat=False, other_query=False, tool="get_messages", other_folder=False)
  class TooManyMessagesError(TgBridgeError)   # message text set by the raiser
  class ReadTimeoutError(TgBridgeError)
  ```
- [ ] **Step 1: Failing tests:** read cursor round trip with and without `s`/`e`/`k`/`h`/`m`, negative and positive chat ids; exact token `r4812_d1792969200_n1001234567890_s1792800000_e1793404800_k5_h` for a fixed example; garbage / duplicate fields / missing `d` → `CursorError`; `is_read_cursor("r12_d1_u5")` True, `is_read_cursor("f6782037_c795")` False, quoted token accepted. Search cursor with `scope="folder", folder_id=5` round-trips with head `o` and `_k5`; a `g…` cursor decodes with `folder_id is None`; `o…` without `k` → `CursorError`. `CursorError(other_folder=True).args[0]` starts with `This cursor belongs to another folder.`
- [ ] **Step 2: Run** `uv run pytest -q tests/test_cursor.py` → FAIL.
- [ ] **Step 3: Implement.** Keep the module docstring style: add a comment block describing the `r` format like the existing ones. Existing tokens must decode unchanged.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(cursor): read cursor and folder search scope`.

### Task 3: Multi-chat read and search over a source

**Files:**
- Create: `tg_bridge/many.py`, `tests/test_many.py`

**Interfaces:**
- Consumes: `MessageSource`, `MessageQuery`, `SearchQuery`, `SearchOffset`; errors from Task 2.
- Produces:
  ```python
  MAX_READ_MESSAGES = 2000
  READ_DEADLINE = 40.0

  @dataclass(frozen=True)
  class ReadResult:
      messages: list[Message]     # merged, key (date, chat_id, id) ascending
      chats_with_messages: int

  async def read_chats(source, chats: Sequence[Chat], start: datetime, end: datetime | None, from_me: bool, *,
                       scope_text: str, clock: Callable[[], float] = time.monotonic) -> ReadResult
  #   scope_text: 'folder "F1"' / '"Family"' — used in the over-cap error text only

  async def search_chats(source, chats: Sequence[Chat], text: str, start, end, from_me: bool, limit: int,
                         after: SearchOffset | None) -> tuple[list[Message], bool]
  #   -> (at most `limit` hits newest first, more)
  ```
- [ ] **Step 1: Failing tests** with `FakeSource` (3 chats, messages over a week):
  - `read_chats` merges by `(date, chat_id, id)`; calls `get_messages` once per chat **in the given order** (`source.message_queries`), all `direction="forward"`, `limit = remaining + 1`; counts chats with messages.
  - over cap: with `MAX_READ_MESSAGES` monkeypatched to 5 and 6 messages → `TooManyMessagesError` whose text is `More than 5 messages in this period across 3 chats of folder "F1". Pass a shorter period.`
  - deadline: a fake `clock` that jumps 41 s after the first chat → `ReadTimeoutError` `Reading took too long: 1 of 3 chats read in 41 s. Pass a shorter period or use a smaller folder.`
  - `search_chats` newest first across chats; continuing with `after` drops the boundary and newer; `test_search_chats_same_second_tie_on_boundary` (two chats, same `date`, boundary = the first of them: the other one is on the next page, exactly once); horizon: chat A has 5 hits on day 7, chat B 1 hit on day 1, `limit=2` → page 1 = A's two newest, `more=True`; walking pages with the cursor returns all 6 hits once in order (`test_search_chats_pages_cover_everything_once`).
  - search queries per chat carry `end = boundary.date + 1 s` when continuing (capped by the period end).
- [ ] **Step 2: Run** `uv run pytest -q tests/test_many.py` → FAIL.
- [ ] **Step 3: Implement** per "Decisions → Multi-chat read" and "→ Folder search" (the horizon rule as written there). Log one `info` line per call with chat count, message count and duration.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): read and search across several chats`.

### Task 4: Multi-chat rendering

**Files:**
- Create: `tg_bridge/format_many.py`, `tests/test_format_many.py`
- Modify: `tg_bridge/format.py` (`_message_line` gets `label: str | None = None`; `search_head` gets `folder: Folder | None = None, folder_size: int | None = None`)

**Interfaces:**
- Consumes: `Repost` (Task 1); `_message_line`, `_clamp`, `_when`, `WEEKDAYS`, `chat_link_base`, `one_line`, `SEP` from `format.py`.
- Produces:
  ```python
  LABEL_CHARS = 20
  def chat_labels(chats: Sequence[Chat]) -> dict[int, str]
  def legend_lines(chat_ids: Iterable[int], chats: Mapping[int, Chat], labels: Mapping[int, str]) -> list[str]

  Item = Message | Repost   # full mode: Message, headlines: Repost

  def render_many(head: str, totals: Callable[[int, int], str], items: Sequence[Item], *, offset: int,
                  chats: Mapping[int, Chat], labels: Mapping[int, str] | None, tz: ZoneInfo, max_chars: int,
                  limit: int, next_hint: Callable[[Message], str], empty_text: str) -> str
  #   items: everything after the cursor, chronological; offset: items before it (for "showing a–b");
  #   totals(a, b) -> the totals line; labels=None = single chat (no labels, no legend).
  #   Shows as many items as fit `limit` and `max_chars`; next_hint gets the last shown item's (first) Message.
  ```
- [ ] **Step 1: Failing tests:**
  - `chat_labels`: long title cut to 20 with `…`; two chats titled "News" → `"News ~890"` and `"News ~123"`.
  - full-mode line: channel post → `#4812 14:03 F1 News: text`; group message → `#7 09:00 F1 Paddock › Anna (@anna_k): hi`.
  - headlines line: single and `×3` forms exactly as in "Decisions"; single-chat form `14:03 #4812: …`.
  - legend lists only chats on the page, with `· Links:` only for chats that have a link base.
  - day headers `— 2026-10-26 Mon —` as in `render_page`.
  - budget: 300 items with `max_chars=2000` → output ≤ 2000, footer `More remain. Next: …`, totals say `showing 1–N` with the N actually shown; with everything shown → `End of period.`; `offset=88` → `showing 89–…`.
  - one huge item that alone exceeds the budget is still shown, cut (as `render_page` does).
  - `search_head` with a folder → `Search "x" in folder "F1" (18 chats) · …`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_format_many.py tests/test_format.py tests/test_format_search.py` → FAIL.
- [ ] **Step 3: Implement.** Same shrinking strategy as `render_page`: try `min(limit, len(items))` down to 1, then cut the single item's text. Existing `_message_line` callers are unchanged (`label=None`).
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): render multi-chat pages and headlines`.

### Task 5: `get_messages(folder=…, mode=…)`

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Create: `tests/test_tools_folder_read.py`; modify `tests/test_server.py`

**Interfaces:**
- Consumes: Tasks 1–4; `_resolve_folder_arg`, `folder_chats`, `folder_label` from 07.
- Produces:
  ```python
  async def get_messages(source, settings, chat: str | None = None, *, folder: str | int | None = None,
                         mode: str = "full", days=None, since=None, until=None, period=None, topic=None,
                         from_me=False, cursor=None, around=None, limit=None, now=..., clock=time.monotonic) -> str
  MAX_FOLDER_CHATS = 50
  ```
  The existing single-chat full path stays as it is; new paths go through one private `_read_many(...)`.
- [ ] **Step 1: Failing tests** (`FakeSource` with a folder "F1" of 3 channels + 1 archived muted channel):
  - each error text from "Decisions → Exact error texts" (both/neither/no period/bad mode/topic/around/too big/empty).
  - `test_folder_read_full_merges_chronologically` with labels and legend.
  - `test_folder_headlines_groups_reposts`: same news in 3 channels → one `×3` line; media-only post absent; totals line counts `posts`, `headlines`, `repeats merged`, `without text skipped`.
  - `test_folder_headlines_pages_cover_everything_once`: `max_chars` small enough for 3 pages; follow `Next:` calls by parsing the hint (cursor only, no other args) until `End of period.`; every group's first `#id` appears exactly once overall; equal timestamps across chats at a boundary.
  - `test_folder_read_includes_archived_member`.
  - `test_single_chat_headlines` (no labels, no legend, cursor scope chat, `Mode: headlines` in head).
  - cursor conflicts: folder cursor + other folder → "belongs to another folder"; chat cursor + other chat → "belongs to another chat"; old `f…` cursors still work for plain reads.
  - `test_server.py`: `get_messages` schema has `folder` and `mode`, `chat` not required; calling with `folder="F1", period="this_week", mode="headlines"` returns text starting with the `Now:` line.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tools_folder_read.py tests/test_server.py` → FAIL.
- [ ] **Step 3: Implement.** Hint text: `call get_messages(folder="<id>", cursor="<token>")` / `call get_messages(chat="<id>", cursor="<token>")`. Server docstring for `get_messages`, replacing its first two lines:
  ```
  Read one chat (`chat`: numeric id, @username, or words from the title) or a whole chat folder (`folder`: id or
  name from list_folders) in time order, as compact lines grouped by day.
  `mode="headlines"`: one short line per post, the same news from several chats merged into one "×N" line; best
  for "what's new in <folder>" questions. Needs a period. Folder reads always need a period.
  ```
  Keep the rest of the docstring; add `folder`/`mode` to the "other arguments come from the cursor" sentence.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(folders): get_messages(folder=, mode="headlines")`.

### Task 6: `search_messages(folder=…)`, Telethon wait, docs

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`, `tg_bridge/telegram/client.py`, `docs/design.md` (§7 table)
- Create: `tests/test_tools_folder_search.py`; modify `tests/telegram/test_client.py`, `tests/telegram/test_client_search.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `search_chats` (Task 3), folder `SearchCursor` (Task 2), `search_head(folder=…)` (Task 4).
- Produces: `search_messages(source, settings, query, chat=None, *, folder: str | int | None = None, …)`.
- [ ] **Step 1: Failing tests:**
  - `chat` + `folder` → `Pass either chat or folder, not both.`; too big / empty folder texts as in Task 5.
  - hits from 2 of 3 folder chats, grouped by chat, newest first; a chat outside the folder with a hit is not shown; `source.search_queries` has one per-chat query per folder member and none global.
  - paging: follow the hint (`folder="5"`, cursor `o…`) to the end, every hit once; a cursor without `folder` arg works; folder cursor + `chat` → the "This cursor is from a folder search…" text; other folder → "belongs to another folder".
  - client: `iter_messages` receives `wait_time=0` for `get_messages` and per-chat search when `query.limit <= 3000`, and no `wait_time` (Telethon default) above it.
  - `test_server.py`: `search_messages` schema has `folder`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_tools_folder_search.py tests/telegram tests/test_server.py` → FAIL.
- [ ] **Step 3: Implement.** Docstring addition for `search_messages`: `` `folder`: search every chat of a folder (id or name from list_folders) instead of one chat; not with `chat`. ``. `design.md` §7 rows:
  `get_messages(chat | folder, period…, mode?, topic?, around?, cursor?, limit?)` — "…`folder` merges all its chats (period required); `mode="headlines"` one line per post, reposts merged `×N`" and `search_messages(query, chat? | folder?, period…, from_me?, cursor?, limit?)`.
- [ ] **Step 4: Run** `uv run pytest -q` → green; `uv run tg-bridge` starts and waits on stdin (Ctrl-C).
- [ ] **Step 5: Commit** `feat(folders): search_messages(folder=); no Telethon page sleeps`.

### Finish (maintainer)

Run the brief's smoke checklist from LM Studio. Add to `docs/roadmap.md` "Log": the F1 × 7 days duration and page count, the `wait_time=0` change, and any tuning of `OVERLAP`/`HEADLINE_CHARS` the smoke test needed.
