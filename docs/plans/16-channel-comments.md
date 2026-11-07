# 16 — Channel Post Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_messages(chat=<channel>, comments_of=<post id>)` reads the comment thread under one channel post, in time order, without membership in the linked discussion group. Each line names the author with a profile link and links to the comment itself, so the model can attribute "who built what" back to a clickable source.

**Architecture:** Two new read methods on the source: `get_post` (the post plus its thread info, via the already allowed `channels.GetMessages`) and `get_comments` (Telethon `iter_messages(channel, reply_to=post_id)` = `messages.GetReplies`, the same path `topic=` already uses). A pure helper normalises reply markers. The plain cursor gets a `p<post id>` field. Rendering reuses `render_page` with a comment-specific line function. `tools.py`/`server.py` stay thin.

**Tech Stack:** unchanged (Python ≥3.12, Telethon 1.45, `mcp`, `pytest-asyncio`).

**Spec:** `docs/features/16-channel-comments.md` + `docs/design.md` §7 (period args, compact line, budget, errors), §6 (read-only), §9. Patterns to copy: `get_messages` topic path in `tg_bridge/telegram/client.py`, `render_page` in `tg_bridge/format.py`, `cursor.py` (short tokens), `tests/test_tools.py`, `tests/telegram/test_client.py`.

## Global Constraints

All constraints from plans 00–09 still apply. Additionally:

- **Read-only.** No join, no `GetDiscussionMessage`, no read receipts. `ALLOWED_REQUESTS` in `tests/telegram/test_client.py` stays as it is: `get_post` uses `GetMessagesRequest`; `GetReplies` only runs inside `iter_messages`.
- **Pure data provider (D1):** the bridge returns the raw comments; summarising "idea + tools" is the model's job. Nothing here shortens comment text beyond the page budget.
- **Only `tg_bridge/telegram/` imports Telethon.** `Post` is a bridge model; `comment_from_tl` lives in `telegram/mapping.py`.
- **Logging:** counts, chat ids, post ids, durations only (`log.info("read %d comments of post %d in %d in %.2f s", …)`). Never comment text, author names, usernames or phones.
- **Budget:** every result ≤ `settings.max_chars`. A page that does not show everything ends with a `Next:` call.
- No local notes/notebook in the bridge (decided with the maintainer 2026-11-07): progress across dialogs is the model's job, e.g. with a filesystem MCP server and a system prompt. The bridge helps by being resumable: a cursor alone continues a thread in a new dialog.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here (Claude's calls unless marked)

- **Arguments**
  - `get_messages(chat, comments_of=<int>, period…, cursor?, limit?)`. `comments_of` needs `chat` (the channel). Period args (`period`/`days`/`since`/`until`) filter by comment date.
  - Rejected with `comments_of`, one error each, naming the argument: `folder`, `topic`, `around`, `from_me`, `mode="headlines"`. Text: `comments_of cannot be combined with <arg>.` (`from_me` adds nothing useful in someone else's thread; the brief says reject.)
  - **Always forward in time** (oldest comment first), with or without a period. "Latest comments" has no use in the motivating case and one direction keeps paging simple.
  - `limit`: same clamp as `get_messages` (`MIN_MESSAGES..MAX_MESSAGES`, default `DEFAULT_MESSAGES`).
- **Post lookup first** (`get_post`), then comments. That gives clean errors and the header data in one cheap request.
- **Exact error texts** (all `ToolError` / anticipated errors, no stack traces):
  - non-channel chat: `comments_of works only with channels. "<title>" is a <kind>: its replies are ordinary messages, read them with get_messages.`
  - unknown post id (or a service message): `Post #<id> not found in "<title>". Take post ids from get_messages or search_messages on this channel.`
  - post without a comment thread (`replies` missing or `comments=False`): `Post #<id> has no comments: comments are off for this channel or this post.`
  - Telegram refuses `GetReplies` (`MsgIdInvalidError`, `ChannelPrivateError`, `ChatAdminRequiredError` → `CommentsUnavailableError` in the client): `Telegram does not show the comments of post #<id> to this account.`
  - cursor of another post: `CursorError(other_post=True)`: `This cursor belongs to another post's comments. Use the cursor from this thread's last result, or call get_messages again without cursor.`
  - Empty thread is a normal result, not an error: header + `No comments on post #<id>.` (with a period: `No comments in this period.`).
- **Models** (`tg_bridge/source/models.py`):
  ```python
  @dataclass(frozen=True)
  class Post:
      message: Message            # the channel post itself
      comments_enabled: bool      # replies is MessageReplies with comments=True
      comment_count: int | None   # replies.replies; None when comments are off
      discussion_id: int | None   # marked id of the discussion supergroup (-100N), None when comments are off
  ```
  Comments are ordinary `Message`s with `chat_id = discussion_id` (their ids belong to the discussion group, not the channel).
- **Reply markers:** a top-level comment is technically a reply to the post's copy in the discussion group (the thread root). `comment_from_tl(raw, chat_id)`: root = `reply_to_top_id or reply_to_msg_id`; if `reply_to_top_id` is None the comment replies to the root → `reply_to_id = None`; else `reply_to_id = reply_to_msg_id` (a reply to another comment, shown `↩#id`). No `GetDiscussionMessage` call is needed.
- **Comment text is not cut at `MAX_TEXT_CHARS` (1500).** Check-in comments can be long and the model needs the whole idea; Telegram caps a message at 4096 chars, so the comment line uses `COMMENT_TEXT_CHARS = 4096`. The page budget still applies (fewer comments per page; one oversize comment falls back to `render_page`'s existing cut).
- **Header** (two or three lines):
  ```
  Comments on post #3186 in imatrofAI (-1001234567890) · 137 comments · whole thread (Europe/Warsaw)
  Post 2026-11-01 Sun 10:00: НЕДІЛЬНИЙ AI ЧЕКІН · https://t.me/imatrofAI/3186
  ```
  With a period, `whole thread` becomes `describe_period(...)`. The post line uses `headlines.headline(post.text)` (≤ 140 chars); if None, `[photo]`/`[media]` from the media text, else nothing after the date. The post link uses `chat_link_base(chat)`; a channel without a link base drops it.
- **Comment line** (every line, no "first time only" shortening, since the model copies authors per project):
  ```
  #5012 10:14 Anna K (https://t.me/anna_k): text … [photo 1.2MB] ↩#5008 ✎ https://t.me/imatrofAI/3186?comment=5012
  ```
  - Author: `Name (https://t.me/<username>)`; no username → `Name (id <user id>)`; deleted account → `Deleted Account (id <id>)`; signed by a chat (anonymous admin, channel as sender) → `sender_title` only; own comment → `Me`.
  - Comment link: `<chat_link_base(channel)>/<post id>?comment=<comment id>`; private channels give `https://t.me/c/<N>/<post>?comment=<id>`. No link base → no link.
  - Day headers (`— 2026-11-01 Sun —`) as in `render_page`.
- **Footer:** `More comments remain. Next: call get_messages(chat="<channel id>", cursor="<token>")`; end: `End of comments on post #<id>.`
- **Cursor:** `Cursor` gains `post_id: int | None`, encoded `p<post id>` (e.g. `f5040_p3186_c890`); `topic_id` and `post_id` are never both set. `c` stays the channel's tag. A cursor with `p` routes `get_messages` to the comments path even when `comments_of` is omitted (the cursor carries the scope, like folders in 08). `comments_of` given together with a cursor for another post → `CursorError(other_post=True)`; same post → fine.
- **Media:** shown as usual (`[photo 1.2MB]`). `download_media` for a comment is out of scope (its id lives in the discussion group, which is not a dialog); the docstring does not promise it.

## File Structure

| File | Change |
|---|---|
| `tg_bridge/source/models.py` | `Post` |
| `tg_bridge/source/protocol.py`, `tg_bridge/source/live.py` | `get_post(chat, post_id) -> Post | None`, `get_comments(chat, post_id, query: MessageQuery) -> list[Message]` |
| `tg_bridge/telegram/mapping.py` | `comment_from_tl(raw, chat_id)`, `post_from_tl(raw, chat_id)` |
| `tg_bridge/telegram/client.py` | `get_post`, `get_comments` (iter_messages with `reply_to`), `CommentsUnavailableError` mapping |
| `tg_bridge/errors.py` | `CommentsUnavailableError`; `CursorError(other_post=…)` |
| `tg_bridge/cursor.py` | `Cursor.post_id`, `p` field |
| `tg_bridge/format.py` | `comments_head`, `_comment_line`, `render_page(…, line_of=None)` pass-through |
| `tg_bridge/tools.py`, `tg_bridge/server.py` | `comments_of` on `get_messages`, `_read_comments`, docstring |
| `docs/design.md` §7 | `get_messages` row gains `comments_of` |
| tests | `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`, `tests/test_cursor.py`, `tests/test_format_comments.py` (new), `tests/test_tools_comments.py` (new), `tests/test_server.py`, `tests/fakes.py` |

## Review Focus

1. **Paging is stable:** 250 comments, pages of 100 by cursor: every comment exactly once, in id order (Task 5: `test_comments_pages_cover_everything_once`).
2. **Reply normalisation:** a top-level comment has no `↩`; a reply to a comment shows the right `↩#id` (Task 1).
3. **Cursor scope:** a comments cursor never reads the channel itself, and a channel-history cursor never reads comments (Task 3, Task 5).
4. **Links:** comment links use the **channel** link base and the **post** id with `?comment=<discussion message id>` (Task 4).
5. **Privacy:** no log line contains text, names or usernames (Task 2 asserts on `caplog`).

---

### Task 1: Post model and comment mapping (pure)

**Files:**
- Modify: `tg_bridge/source/models.py`, `tg_bridge/source/__init__.py` (export `Post`), `tg_bridge/telegram/mapping.py`
- Test: `tests/telegram/test_mapping.py`

**Interfaces:**
- Produces: `Post` (see Decisions); `comment_from_tl(raw, chat_id: int) -> Message | None`; `post_from_tl(raw, chat_id: int) -> Post | None` (None for `MessageEmpty`/`MessageService`).

- [ ] **Step 1: Failing tests:**
  - `comment_from_tl`: `MessageReplyHeader(reply_to_msg_id=900, reply_to_top_id=None)` → `reply_to_id is None`; `(reply_to_msg_id=5008, reply_to_top_id=900)` → `5008`; no header → `None`; `chat_id` is the passed discussion id; sender user/anonymous title handled as in `message_from_tl`.
  - `post_from_tl`: `replies=MessageReplies(replies=137, replies_pts=1, comments=True, channel_id=555)` → `comments_enabled=True, comment_count=137, discussion_id=-1000000000555`; `replies=None` → disabled, `None, None`; `comments=False` (group-style replies) → disabled; `MessageEmpty` / `MessageService` → `None`.
- [ ] **Step 2: Run** `uv run pytest -q tests/telegram/test_mapping.py` → FAIL.
- [ ] **Step 3: Implement.** `comment_from_tl` calls `message_from_tl` and then fixes `reply_to_id` from the raw header (`dataclasses.replace`). Marked discussion id: `-(10**12 + channel_id)` (same rule as Telethon `get_peer_id`; reuse an existing helper if mapping has one).
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(comments): post model and comment mapping`.

### Task 2: Source methods (client, live, fake)

**Files:**
- Modify: `tg_bridge/telegram/client.py`, `tg_bridge/source/protocol.py`, `tg_bridge/source/live.py`, `tg_bridge/errors.py`, `tests/fakes.py`
- Test: `tests/telegram/test_client.py` (or a new `tests/telegram/test_client_comments.py` if that file is crowded)

**Interfaces:**
- Produces:
  ```python
  async def get_post(self, chat: Chat, post_id: int) -> Post | None
  async def get_comments(self, chat: Chat, post: Post, query: MessageQuery) -> list[Message]
  ```
  (source protocol takes `post: Post` so the client knows `discussion_id` without a second lookup). `query.direction` is always `"forward"`, `topic_id=None`, `from_me=False`.
- `CommentsUnavailableError(TgBridgeError)`, message `Telegram does not show these comments to this account` (no ids needed; the tool words the final text).

- [ ] **Step 1: Failing tests** with the existing fake Telethon client:
  - `get_post` uses `_fetch_message` (one `GetMessagesRequest`), returns `Post`; missing id → `None`.
  - `get_comments` calls `iter_messages(entity, reply_to=3186, reverse=True, offset_id=<boundary or 0>, offset_date=query.start, limit=None, wait_time=0 for ≤ 3000)`; stops at `query.end`; skips before `query.start`; stops at `query.limit`; messages carry `chat_id = post.discussion_id` and normalised replies.
  - `MsgIdInvalidError` / `ChannelPrivateError` / `ChatAdminRequiredError` from iteration → `CommentsUnavailableError`; `FloodWaitError` → `RateLimitedError` (via `_call`).
  - `{type(r).__name__ for r in fake.requests} <= ALLOWED_REQUESTS` (unchanged set).
  - `caplog`: the info line contains counts and ids, none of the fake texts/names.
  - `FakeSource`: `posts: dict[tuple[int, int], Post]`, `comments: dict[tuple[int, int], list[Message]]` keyed by `(channel id, post id)`; `get_comments` applies period, boundary and limit like `get_messages`; records `comment_queries`; honours `self.error`.
- [ ] **Step 2: Run** the client tests → FAIL.
- [ ] **Step 3: Implement.** Share the period/limit loop with `get_messages` if it stays readable (extract `_collect(iterator, query, map_fn)`); otherwise a small separate loop. Wrap with `_call`. `LiveSource` methods call `_ensure_connected()` first, as the others do.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(comments): read a post and its comment thread`.

### Task 3: Cursor and errors

**Files:**
- Modify: `tg_bridge/cursor.py`, `tg_bridge/errors.py`
- Test: `tests/test_cursor.py`

- [ ] **Step 1: Failing tests:** round-trip `Cursor(chat, "forward", 5040, None, None, None, False, post_id=3186)` → `f5040_p3186_c890`; with a period → `f5040_s…_e…_p3186_c890`; `p` twice → `CursorError`; `t` and `p` together → `CursorError`; old tokens without `p` still decode (`post_id=None`); `CursorError(other_post=True)` text matches Decisions.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_cursor.py` → FAIL.
- [ ] **Step 3: Implement:** `_FIELD = re.compile(r"([setcp])(\d+)|m")`; `post_id` defaults to `None` so existing call sites stay unchanged.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(comments): cursor carries the post id`.

### Task 4: Rendering

**Files:**
- Modify: `tg_bridge/format.py`
- Test: `tests/test_format_comments.py` (new)

**Interfaces:**
- Produces:
  ```python
  COMMENT_TEXT_CHARS = 4096
  def comments_head(chat: Chat, post: Post, period_text: str | None, tz: ZoneInfo) -> str
  def comment_line_of(chat: Chat, post_id: int) -> Callable[[Message, str, int | None], str]
  def render_page(..., line_of: Callable[[Message, str, int | None], str] | None = None)  # None = today's _message_line
  ```
- [ ] **Step 1: Failing tests:**
  - head: exact two lines from Decisions for a public channel; `period_text` replaces `whole thread`; private channel → `https://t.me/c/<N>/3186`; post with only a photo → `Post 2026-11-01 Sun 10:00: [photo …]`; `comment_count=0` → `0 comments`.
  - line: user with username → `Anna K (https://t.me/anna_k)` on **every** line (two comments by Anna, both carry the link); without username → `(id 42)`; deleted → `Deleted Account (id 42)`; `sender_title` → title only; `out` → `Me`; reply → `↩#5008`; edit → `✎`; media text; link `https://t.me/imatrofAI/3186?comment=5012` at the end; a 3000-char comment is **not** cut when the budget allows.
  - page: day headers; budget respected for 50 long comments (`len(text) <= max_chars`), footer `Next:` present; one 4000-char comment with `max_chars=2000` falls back to the cut and still ends with a `Next:` call.
  - `render_page` without `line_of` renders exactly as before (existing tests stay green).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.** `_comment_line` reuses `_media_text`, `one_line` and the `_text` logic with `COMMENT_TEXT_CHARS`. `line_of` is threaded through `_page_text`.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(comments): render comment threads with author and comment links`.

### Task 5: `get_messages(comments_of=…)`

**Files:**
- Modify: `tg_bridge/tools.py`
- Test: `tests/test_tools_comments.py` (new)

- [ ] **Step 1: Failing tests** (FakeSource, a channel `@imatrofAI`, post 3186 with comments):
  - whole thread, oldest first, header + lines + `End of comments on post #3186.`
  - `test_comments_pages_cover_everything_once`: 250 comments, `limit=100`: follow `Next:` cursors (extract with a regex as other paging tests do), passing only `chat` + `cursor`; ids seen == all ids, no duplicates.
  - period filter: `since="2026-11-01", until="2026-11-01"` keeps only that day; the cursor keeps the period.
  - empty thread → `No comments on post #3186.`; empty with period → `No comments in this period.`
  - errors from Decisions: group chat, unknown post, comments off, `CommentsUnavailableError`, each rejected argument (`folder`, `topic`, `around`, `from_me`, `mode="headlines"`), `comments_of` with another post's cursor, a channel-history cursor plus `comments_of` (→ `CursorError(other_post=True)`).
  - a comments cursor without `comments_of` continues the thread; a channel cursor never calls `get_comments`.
  - `RateLimitedError` surfaces as the existing rate-limit text.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `_read_comments(source, settings, channel, post_id, period, boundary_id, limit)` and route to it from `get_messages`: validate rejected args before resolving the chat; after resolving, a decoded cursor with `post_id` also routes here. Fetch `limit + 1` like `get_messages`. Keep `get_messages` readable: the comments branch is one early `return`.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(comments): get_messages(comments_of=…)`.

### Task 6: Server, docs

**Files:**
- Modify: `tg_bridge/server.py`, `docs/design.md` §7, `tests/test_server.py`

- [ ] **Step 1: Failing test:** the `get_messages` tool schema has an optional integer `comments_of`; the call passes it through.
- [ ] **Step 2: Implement.** Add to the docstring (keep it short, small models copy examples):
  > `comments_of`: a post #id of a channel: reads the comments under that post, oldest first (no need to join the discussion group). Each line has the author's link and the comment's link. Period args filter by comment date. For a series of posts (e.g. weekly check-ins): first `search_messages(query=<title words or #hashtag>, chat=<channel>, since=…)` to list the posts, then `get_messages(chat=<channel>, comments_of=<post id>)` for each post, and keep calling until the result ends with "End of comments".
  
  `design.md` §7 table: `get_messages(chat | folder, period…, mode?, topic?, around?, comments_of?, cursor?, limit?)`, purpose gains "; `comments_of=<post id>` reads a channel post's comment thread".
- [ ] **Step 3: Run** `uv run pytest -q` → green.
- [ ] **Step 4: Commit** `docs(comments): tool description and design contract`.

### Finish (maintainer)

Run the brief's smoke checklist from LM Studio, including the motivating flow: search the "НЕДІЛЬНИЙ AI ЧЕКІН" posts since 2025, then read 3–4 threads by `comments_of`. Note in `docs/roadmap.md` "Log": thread sizes and pages per thread, whether the model followed the two-step flow from the docstring, and any FloodWait seen.
