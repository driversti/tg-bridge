# 17 — Cloud Scope Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. TDD: failing test first, then code, then `uv run pytest -q` green.

**Goal:** `TG_BRIDGE_SCOPE=public` (the default) hides every 1:1 chat, bot and private group/channel the owner has not switched on; `all` keeps today's behaviour. A separate command `tg-bridge-admin` serves an owner-only page to switch private groups/channels on and off.

**Architecture:** A pure policy function `chat_visible(chat, allowlist)` and a `ScopedSource` wrapper around `MessageSource` that filters `list_dialogs` and guards every chat-taking method. All tools resolve chats from `list_dialogs`, so they inherit the filter; global search is the only place that needs extra work (hits from hidden chats, page top-up). The allowlist is a small JSON file next to the session, written only by the admin page and re-read on every call. The admin page is a stdlib `http.server` app with token-link login; it fetches dialogs and chat photos through `LiveSource`/`ReadOnlyTelegram`, then closes the connection.

**Tech Stack:** unchanged, no new dependencies (stdlib `http.server`, `secrets`, `html`, `json`).

**Spec:** `docs/features/17-cloud-scope.md`; `docs/design.md` §6, §7, §8. Patterns to copy: `DialogCache` (`tg_bridge/source/dialog_cache.py`) for an atomic JSON file next to the session; `LiveSource` delegation; `tests/fakes.py` (`FakeSource`, `make_chat`, `make_user_chat`); `tests/telegram/test_client.py` (`ALLOWED_REQUESTS`).

## Global Constraints

All constraints from plans 00–16 still apply. Additionally:

- **Read-only.** The chat photo download is a read (`download_profile_photo`); nothing creates invite links or joins. Only `tg_bridge/telegram/` imports Telethon.
- **Fail closed everywhere.** Unknown kind, missing username, unreadable/corrupt allowlist → hidden. A bug must hide too much, never too little.
- **Hidden = nonexistent.** Hidden chats are never resolution candidates or suggestions, and the not-found text for a hidden chat is the same as for a chat that does not exist (plus the one scope note below). No "exists but forbidden".
- **Logging:** chat ids and counts only. Never titles, names, usernames, message text, the login token or the cookie value in logs. (The login link goes to the terminal with `print(..., file=sys.stderr)` once at start, outside `logging`.)
- **Admin page HTML:** every chat title and folder title goes through `html.escape`. No inline user data in JS.
- Ask the maintainer before every commit.

## Decisions taken here (Claude's calls unless marked)

- **Setting:** `TG_BRIDGE_SCOPE` in `DEFAULTS` = `"public"` (maintainer's call). Values `all|public`, case-insensitive, stripped; anything else → `ConfigError("TG_BRIDGE_SCOPE must be all or public")`. `Settings.scope: Literal["all", "public"]` (no dataclass default; `load_settings` always sets it).
- **Policy** (`tg_bridge/scope.py`, pure):
  ```python
  def chat_visible(chat: Chat, allowlist: frozenset[int]) -> bool:
      if chat.kind in ("user", "bot"): return False          # incl. Saved Messages (kind "user")
      if chat.kind in ("channel", "supergroup") and chat.username: return True
      if chat.kind in ("group", "channel", "supergroup"): return chat.id in allowlist
      return False
  def needs_allowlist(chat: Chat) -> bool:   # the chats the admin page lists
      return chat.kind == "group" or (chat.kind in ("channel", "supergroup") and not chat.username)
  ```
- **Usernames:** `chat_from_dialog` currently takes `entity.username` only; channels with only collectible usernames (`entity.usernames`, active ones) get `None` and would count as private. Fix the mapping: `username = entity.username or <first active entry of entity.usernames>`. (Also improves their links.)
- **Allowlist file:** `session_path.with_name(f"{stem}.allowlist.json")`, content `{"version": 1, "chat_ids": [..]}` sorted. Class `Allowlist(path)` in `tg_bridge/source/allowlist.py` with `load() -> frozenset[int]` (missing → empty; unreadable/corrupt/wrong version → empty + `log.warning` with the exception class name only) and `set(chat_id, on: bool)` (read-modify-write, write to a temp file in the same dir with mode `0o600`, `os.replace`). Parent dir created with `0o700` if missing. Re-read on every `ScopedSource.list_dialogs` call (a tiny file; no mtime cache needed).
- **ScopedSource** (`tg_bridge/source/scoped.py`), wraps any `MessageSource`:
  - `list_dialogs(refresh=)` → inner dialogs filtered by `chat_visible(c, allowlist.load())`.
  - `list_folders(refresh=)` → passthrough (folder counts are computed in tools from `list_dialogs`, so they only count visible chats; verify with a test).
  - `dialogs_age` → passthrough when the inner source has it (tools use `getattr(source, "dialogs_age", None)`; keep that working: define it only by delegation, returning `None` if the inner lacks it).
  - `get_chat_info`, `get_messages`, `get_post`, `get_comments`, `get_media_files`, `read_media`: first `self._require_visible(chat)` → raises `ChatNotFoundError(<chat id as str>, suggestions=[])` (use the existing error type and its real signature) if `chat.id` is not in the visible set. Defence in depth: cursors carry chat ids.
  - `search_messages(query, chat)`: with `chat` → `_require_visible` then passthrough. Global (`chat is None`) → keep only hits whose `chat_id` is in the visible id set; if fewer than `query.limit` visible hits remain and the raw page was full (`len(raw) == query.limit`), fetch again with `after=SearchOffset(last_raw.chat_id, last_raw.id, last_raw.date)` and append, up to `MAX_TOPUP_ROUNDS = 5` extra rounds. Return at most `query.limit` visible hits. Log (`info`) the number of hidden hits dropped and rounds used.
  - `whoami`, `close` → passthrough.
- **Wiring:** `server.main()` wraps `LiveSource` in `ScopedSource` when `settings.scope == "public"`; `log.info("starting tg-bridge (stdio, scope=%s)", scope)`.
- **Model-facing note** (only when `settings.scope == "public"`): append to the not-found text of `resolve_chat_arg` (`_not_found`) one line: `This bridge runs in public scope: 1:1 chats, bots and private groups that the owner has not switched on are not available.` `list_chats` gets the same sentence as the last line of its result. No change for `all`, so existing tests stay valid when they run with `all`.
- **Tests and the default:** test helpers that call `load_settings(env=CREDS, …)` keep working; where a test asserts exact not-found or `list_chats` text, give it `TG_BRIDGE_SCOPE=all` in `env`. New tests cover `public` explicitly.
- **Admin command** (`tg_bridge/admin.py`, entry `tg-bridge-admin = "tg_bridge.admin:main"` in `pyproject.toml`; also runnable as `python -m tg_bridge.admin`):
  - Settings: `TG_BRIDGE_ADMIN_PORT` default `8765` (int 1–65535 else `ConfigError`). Bind `127.0.0.1` only, hard-coded.
  - **Snapshot fetch** (`async def fetch_snapshot(settings) -> Snapshot`): build `LiveSource(ReadOnlyTelegram(settings), dialog_cache=DialogCache(...))` exactly as `server.main` does, `list_dialogs(refresh=True)`, `list_folders()`, then for each chat with `needs_allowlist(chat)` fetch its small photo via the new `ReadOnlyTelegram.download_chat_photo(chat_id) -> bytes | None` (Telethon `client.download_profile_photo(entity, file=bytes, download_big=False)`; `None` when no photo; catch `RPCError` → `None` + log class name), then `close()`. Photos are kept **in memory** in the snapshot (dict id → bytes), not on disk. `Snapshot = (chats: list[Chat], folders: list[Folder], photos: dict[int, bytes], fetched_at: datetime)`. Run it with `asyncio.run` at start and on "Refresh". `SessionBusyError` → page shows "Telegram session is busy (LM Studio is using it); try Refresh in a moment." and keeps the previous snapshot.
  - **Auth:** at start `login_token = secrets.token_urlsafe(32)`, `session_cookie = secrets.token_urlsafe(32)`, `csrf = secrets.token_urlsafe(32)`. Print once to stderr: `Open http://127.0.0.1:<port>/login?token=<login_token>` and a hint for the phone (`tailscale serve --bg <port>`, then the same path on the tailnet name). `GET /login?token=` with the right token (compare with `hmac.compare_digest`) → `303` to `/` with `Set-Cookie: tgb_admin=<session_cookie>; HttpOnly; SameSite=Strict; Path=/`. The token stays valid for the process lifetime (laptop and phone). Every other route without the right cookie → `403` plain text "Open the login link printed in the terminal." Wrong token → `403`.
  - **Routes:** `GET /` (list; query params `q` = title filter, case-insensitive via `resolve.normalize`; `folder` = folder id, membership via `folders.folder_contains`); `GET /photo/<id>` (`image/jpeg` from the snapshot, `404` if none; only ids with `needs_allowlist`); `POST /toggle` (form fields `chat_id`, `on` = `1|0`, `csrf`; wrong csrf → `403`; unknown or not-`needs_allowlist` chat id → `400`; then `Allowlist.set`, `303` back to `/` keeping `q`/`folder`); `POST /refresh` (csrf-checked, re-fetch snapshot, `303`). Everything else `404`.
  - **Page:** server-rendered HTML, one small inline `<style>`, light/dark via `prefers-color-scheme`, mobile-friendly (one column, 16 px gutter). Header: counts "N private chats · M on", snapshot time, Refresh button, search form, folder `<select>`. One row per `needs_allowlist` chat, sorted: on first, then by title: photo (`/photo/<id>`) or a circle with the first letter, title, kind label (`group`, `supergroup`, `forum`, `channel`; `archived` tag), link `https://t.me/c/<N>` for `-100N` ids with a Copy button (tiny inline script: `navigator.clipboard.writeText(this.dataset.link)`; data attribute holds only the link), and an on/off form button. Basic groups show "no link". Nothing for public chats or DMs.
  - Uses `http.server.ThreadingHTTPServer`? **No:** plain `HTTPServer` (single thread) so snapshot refresh and allowlist writes never race. `Ctrl+C` exits cleanly (closes nothing else; the Telegram connection is already closed).

## File Structure

- Create `tg_bridge/scope.py` (policy), `tg_bridge/source/allowlist.py`, `tg_bridge/source/scoped.py`, `tg_bridge/admin.py`.
- Modify `tg_bridge/config.py` (`scope`, `admin_port`), `tg_bridge/telegram/mapping.py` (collectible usernames), `tg_bridge/telegram/client.py` (`download_chat_photo`), `tg_bridge/tools.py` (scope note), `tg_bridge/server.py` (wiring), `pyproject.toml` (script), `tests/fakes.py` (if needed: `FakeSource` search returning raw pages by offset for top-up tests).
- Tests: `tests/test_scope.py`, `tests/test_allowlist.py`, `tests/test_scoped_source.py`, `tests/test_tools_scope.py`, `tests/test_admin.py`, additions to `tests/test_config.py`, `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`, `tests/test_server.py`.
- Docs: `docs/design.md` §7 (one paragraph "Scope"), §8 (scope + admin page), `docs/setup.md` (env table: `TG_BRIDGE_SCOPE`, `TG_BRIDGE_ADMIN_PORT`; LM Studio `mcp.json` example gets `"TG_BRIDGE_SCOPE": "all"` in `env` with a one-line warning that without it LM Studio sees no private chats; a short "Allowlist page" section: run `uv run tg-bridge-admin`, open the printed link, phone via `tailscale serve`).

## Review Focus

- Any path where a hidden chat's title, username or messages could reach tool output (resolution suggestions, ambiguous candidates, search group headers, folder lists, cursors).
- Global search top-up: no duplicates, no infinite loop, cursor of the next page still continues after the last *shown* hit.
- Admin: XSS (titles), CSRF, cookie flags, `127.0.0.1` bind, token never logged, session released after fetch.

### Task 1: Policy and settings (pure)
- [ ] `tests/test_scope.py`: `chat_visible`/`needs_allowlist` for user, bot, self (user), basic group (on/off), supergroup with/without username (on/off), forum supergroup, channel with/without username (on/off), and an unexpected kind (construct with `kind="secret"` via `dataclasses.replace` or a cast) → hidden.
- [ ] `tests/test_config.py`: default `public`; `all`, ` ALL ` accepted; `private` → `ConfigError` naming the variable; admin port default 8765, `0`/`70000`/`abc` → `ConfigError`.
- [ ] Implement `scope.py`, config changes.

### Task 2: Allowlist file
- [ ] `tests/test_allowlist.py`: missing file → empty; set on/off round-trip; file mode `0o600`; corrupt JSON / wrong version / non-int ids → empty and a warning that contains no file content; atomic write leaves no temp files.
- [ ] Implement `source/allowlist.py`.

### Task 3: Collectible usernames
- [ ] `tests/telegram/test_mapping.py`: channel with `username=None, usernames=[inactive "a", active "b"]` → `username == "b"`; none active → `None`; plain `username` wins.
- [ ] Implement in `chat_from_dialog`.

### Task 4: ScopedSource
- [ ] `tests/test_scoped_source.py` with `FakeSource`: `list_dialogs` filtering and allowlist re-read between calls (toggle file → next call differs); every chat-taking method refuses a hidden chat with `ChatNotFoundError`; `dialogs_age` delegation (present / absent); global search drops hidden hits; top-up fetches the next raw page from the last raw hit and stops at `limit` visible hits, at an exhausted source, and after `MAX_TOPUP_ROUNDS`; chat-scoped search passthrough.
- [ ] Implement `source/scoped.py`.

### Task 5: Tools through ScopedSource
- [ ] `tests/test_tools_scope.py` (real `tools.*` over `ScopedSource(FakeSource(...))`, settings with `public`), dialogs: one DM "Anna K", one bot, one public channel, one private group off, one private group on, one folder containing all of them:
  - `list_chats` shows only the public channel and the allowed group, ends with the scope note; `list_chats(kind="user")` → none + note.
  - `get_messages(chat="Anna")` / by id / by `@anna_k` → not-found text without "Anna" in it (no suggestion leaks), with the scope note.
  - Ambiguous name shared by a hidden and a visible chat → resolves to the visible one (no candidates list).
  - `list_folders` count and `list_chats(folder=…)` count only visible chats; `get_messages(folder=…)` and `search_messages(folder=…)` never show hidden chats.
  - Global `search_messages` with hits in the DM and in the channel → only the channel hit; `from_me=True` likewise.
  - `get_chat_info`, `export_chat`, `download_media` on the hidden DM → not found.
  - A cursor built for the hidden chat (encode one with `cursor.py`) → not found / cursor error, never its messages.
  - `get_comments` on a public channel works.
- [ ] Implement the scope note in `tools.py`; fix exact-text tests elsewhere by passing `TG_BRIDGE_SCOPE=all` where needed.

### Task 6: Server wiring
- [ ] `tests/test_server.py`: `main`-level helper (extract `build_source(settings) -> MessageSource` from `main` to make it testable) returns `ScopedSource` for `public`, the bare `LiveSource` for `all`.
- [ ] Implement.

### Task 7: Chat photo download
- [ ] `tests/telegram/test_client.py`: `download_chat_photo` returns bytes from a fake `download_profile_photo`, `None` when Telethon returns `None`, `None` on an `RPCError`; requests stay within `ALLOWED_REQUESTS` (add only what the photo download really issues, e.g. `GetFileRequest`, and say so in the commit).
- [ ] Implement in `ReadOnlyTelegram`.

### Task 8: Admin page
- [ ] `tests/test_admin.py`: start the server on port 0 in a thread with an injected snapshot (no Telegram: `fetch_snapshot` is a parameter) and a temp allowlist; use `http.client`:
  - no cookie → 403; `/login` wrong token → 403; right token → 303 + cookie flags `HttpOnly`, `SameSite=Strict`.
  - `/` lists only `needs_allowlist` chats; a title `<script>alert(1)</script>` comes back escaped; DMs/public channels absent; `t.me/c/<N>` link for a `-100N` chat; basic group "no link"; `q` and `folder` filters.
  - `POST /toggle` without/with wrong csrf → 403; valid → allowlist file updated, 303; a DM id or public channel id → 400.
  - `/photo/<id>` → bytes and `image/jpeg`; unknown → 404.
  - Refresh with a fetcher raising `SessionBusyError` → page shows the busy message, old snapshot kept.
  - Server binds `127.0.0.1` (assert on `server.server_address[0]`).
- [ ] Implement `admin.py` + script entry.

### Task 9: Docs
- [ ] `design.md` §7/§8, `setup.md` as listed above. Brief: tick nothing (smoke is the maintainer's).
- [ ] Full suite green: `uv run pytest -q`.

### Finish (maintainer)
- Smoke checklist from the brief, including: LM Studio without `TG_BRIDGE_SCOPE` → no DMs; with `all` → everything.
- Roadmap: 17 `[x]`, Log line(s) for deviations.
