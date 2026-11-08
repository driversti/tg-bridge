# tg-bridge — Design

**Date:** 2026-10-26 · **Status:** Approved 

## 1. Intent

A **read-only bridge between a local LLM and the user's personal Telegram account**.
The user runs a local model in LM Studio (or another MCP client) on their Mac and asks
questions in natural language; the model calls tg-bridge tools to read their chats,
groups and channels (including private ones) and answers, summarises or produces
extracts.

**Why:** today, getting information out of Telegram into an AI workflow means
screenshots and copy-paste (e.g. forwarding a bug report
from a chat to a coding agent). Cloud models (Claude, Gemini, ChatGPT) must
**never** get direct access, because the chats contain sensitive personal data. A local
model + a local bridge keeps all data on the user's own machines.

tg-bridge contains **no agent and no LLM**. It only exposes tools; the agent is the
MCP client (LM Studio).

## 2. Target scenarios (stage 1)

1. **Find information** in a chat with a specific person.
   *"Find in my chat with B every time I shared personal data or wrote that I love someone."*
2. **Summarise** a group/channel over a period.
   *"Short summary of chat C for the last two days."* (= from yesterday 00:00 local
   time until the latest message today)
3. **Deleted users:** list dialogs whose peer account is now deleted and export each
   conversation to its own file.
4. **Extract for another model:** read a conversation for a period and produce a
   brief, translation or other extract.
   *"Read my chat with A and write a prompt for a coding agent describing the bug."*

## 3. Goals, non-goals, anti-goals

**Goals**
- Cover scenarios 1–4 for the **full existing history** (live reads, no local archive).
- Work with a mid-size local model (~30B, ≥32k context): tool outputs are compact,
  bounded and paginated.
- Modular: future modules (archive, semantic search, bot) plug in without rewriting tools.

**Non-goals (stage 1)**
- Local message archive/database, backfill, capturing edit history or deleted messages.
- Semantic (meaning-based) search.
- Telegram bot interface.
- Discovering new public channels/groups.
- Storing media. Only metadata and links are kept, and download happens on explicit request.
- Secret chats (device-bound, not reachable via the API; out of scope).

**Anti-goals: unacceptable even if everything else works**
- **Any write side effect on the account:** sending, editing, deleting, reacting,
  joining/leaving, **marking messages as read**, typing indicators, online-status
  updates. The bridge must be invisible to the user's contacts (exception: appearing
  "online"/"last seen recently" is acceptable if it cannot be avoided; the owner's call).
- Private data leaving the user's machines (no cloud LLM calls, no telemetry, no
  third-party services).
- Session file or API credentials committed to git, logged, or world-readable.
- Message contents written to logs.
- Tool outputs so large they blow the local model's context.

## 4. Constraints

- **Hardware:** MacBook Pro M1 Max 64 GB runs LM Studio + tg-bridge (stage 1).
  An always-on home server (Proxmox, LXC containers) hosts the networked bridge and,
  later, the recorder. The owner's agent Hermes (LXC 110, cloud model) is the phone-side
  client. The Jetson Orin Nano planned earlier was dropped (2026-11-07): an 8 GB box can't
  run a model good enough for multi-step tool use.
- **Telegram API:** MTProto user client with the user's own `api_id`/`api_hash`
  (see setup.md §1) and a **new, dedicated session** (own login).
- **Rate limits:** Telegram returns `FloodWait`; the bridge must respect it.
- **Time zone:** `Europe/Warsaw` for all user-facing dates and period maths.
- **Tokens:** development runs in short agent sessions with limited budgets, so docs are
  split per feature and the code stays small and conventional.

## 5. Decisions (made in the design conversation)

| # | Decision | Rationale |
|---|---|---|
| D1 | Stage 1 = **live bridge**: tools read Telegram on demand via Telethon | Covers all 4 scenarios incl. old history with zero storage |
| D2 | Archive (SQLite) is **stage 2**, added behind the same interface | Needed only for edit/delete capture, semantic search, speed |
| D3 | Archive recording starts "from now"; **backfill is a separate later feature** | Keep stage 2 small |
| D4 | Archive covers **all chats** by default (exclusion list later if needed) | Owner's choice |
| D5 | Storage when archive arrives: **SQLite** (+FTS5, later `sqlite-vec`) | Single file, no server, fast filtering; files are only an export format |
| D6 | **Python** + Telethon | Most mature MTProto lib; simplest MCP SDK. The coding agent writes the code, so language familiarity doesn't matter |
| D7 | Stage 1 runs **on the Mac**, MCP over **stdio**, launched by LM Studio | No network exposure, no auth needed |
| D8 | Media: store/return metadata + link only; download only via explicit tool | The user downloads as needed |
| D9 | Keep max available identity data for users (id, names, usernames, phone if visible) | Deleted accounts lose their names, so we need whatever identifies them |
| D10 | Docs live in this repo (`docs/`); GitHub issues only if it becomes worth it | Cheapest for agents to read |

## 6. Architecture

```
LM Studio (Mac, local model)
   │  MCP over stdio
   ▼
tg_bridge.server ── registers tools ──► tg_bridge.tools
                                          │  depends only on the interface
                                          ▼
                                 MessageSource (Protocol)
                                   ├─ LiveSource     — Telethon, read-only wrapper  [stage 1]
                                   └─ ArchiveSource  — SQLite                       [stage 2]
                                          │
                     tg_bridge.format  (compact text rendering, budgets, cursors)
                     tg_bridge.export  (Markdown files on disk)
                     tg_bridge.timerange (period parsing, Europe/Warsaw)
```

### Module responsibilities

- **`config`**: loads settings from env / `~/.config/tg-bridge/config.env`: api_id,
  api_hash, session path, export dir, download dir, timezone, output budget.
- **`telegram/client`**: the **only** place that touches Telethon. Exposes a
  `ReadOnlyTelegram` wrapper with read methods only. Nothing else in the code base imports
  Telethon (enforced by a test that scans imports).
- **`source`**: the `MessageSource` protocol + domain models (`Chat`, `Message`,
  `Person`, `MediaInfo`, `Page`). `LiveSource` implements it on top of `ReadOnlyTelegram`.
- **`timerange`**: turns tool arguments into `[start, end)` UTC datetimes using
  Europe/Warsaw day boundaries. The **model never does date arithmetic**.
- **`format`**: renders messages as compact lines and enforces the per-call
  character budget. Produces `next_cursor` for pagination.
- **`export`**: writes Markdown files and returns path + stats, not contents.
- **`tools`**: thin MCP tool functions: validate args, call source, format.
- **`server`**: MCP bootstrap (official `mcp` Python SDK), stdio transport.

### Domain model (sketch, builder refines)

- `Person`: `id`, `first_name`, `last_name`, `username`, `phone` (if visible),
  `is_deleted`, `is_bot`.
- `Chat`: `id`, `kind` (`user|group|supergroup|channel|bot`), `title`, `username`,
  `peer: Person | None` (for 1:1), `last_message_at`, `is_archived`, `is_forum`.
- `Message`: `id`, `chat_id`, `date`, `edit_date`, `sender: Person | None`, `text`,
  `reply_to_id`, `forward_from` (name/chat), `topic_id`, `media: MediaInfo | None`,
  `link: str | None`.
- `MediaInfo`: `kind` (photo/video/document/voice/sticker/…), `file_name`, `size`,
  `mime_type`, `duration`.

### Network mode (feature 11)

`tg-bridge-net serve` is a second entry point for a homelab LXC: one long-running asyncio process with one
`LiveSource`, hence one Telegram client and one session file used by this process only. MCP is served over
streamable HTTP (`ScopedSource` behind a bearer-auth ASGI middleware, `uvicorn`) on an explicit LAN address. The admin
page runs in the same process on `127.0.0.1` in its own thread and reads the unscoped source through
`asyncio.run_coroutine_threadsafe`, so the page and the tools never use a second client. stdio (`tg-bridge`, LM
Studio) and standalone `tg-bridge-admin` on the Mac are unchanged. Startup connects and checks authorization first;
only then the page opens and the login link goes out through the Homelab bot (Bot API, not Telethon).

```
Hermes (CT 110) ──HTTP + bearer──► tg-bridge-net (CT 120, LAN IP:8766) ──► LiveSource ──► Telegram
phone ──Tailscale HTTPS──► tailscale serve ──► 127.0.0.1:8765 (admin page, same process) ──┘
```

## 7. Tool contract (stage 1)

All tools are read-only. All periods use the shared period arguments:

- `days: int`: "last N calendar days": from local 00:00 of (today − N + 1) to now.
  `days=1` = today, `days=2` = yesterday 00:00 → now.
- or `since` / `until`: ISO date (`2026-10-20`) or datetime; date-only `until` is
  inclusive of that whole day. Local time zone unless an offset is given.
- If neither is given, the latest messages are returned (tool-specific default limit).

Chat arguments accept a **numeric id, `@username`, or a title fragment**. If a title
fragment is ambiguous, the tool returns the candidate list instead of guessing.

| Tool | Purpose | Returns |
|---|---|---|
| `whoami()` | The logged-in account (checks the whole chain) | name, @username, id |
| `now()` | Current date and time in the configured time zone; call it before answering anything relative to today | date, weekday, time, time zone, ISO week, this/last week, month and year as dates |
| `list_chats(query?, kind?, limit=30, folder?, min_folders?)` | Find/browse dialogs (incl. archived); `folder` (id or title words) limits to that folder, in its order; `min_folders=N` keeps chats in ≥ N folders and names them | id, kind, title, @username, last message date, `deleted_peer` flag |
| `list_folders()` | The user's chat folders in app order | id, title, kind, chat count, unread chats, rule/explicit summary |
| `get_chat_info(chat)` | Details of one chat | type, title, folders, description, members count, peer identity, topics (forums) |
| `get_messages(chat \| folder, period…, mode?, topic?, around?, cursor?, limit?)` | Read a chat chronologically for a period; `folder` merges all its chats (period required); `mode="headlines"` one line per post, reposts merged `×N`; channel posts with comments end in `💬N`, and the page names one ready `get_comments` call | compact lines + `next_cursor` + `truncated` flag |
| `get_comments(chat, post?, period…, cursor?, limit?)` | A channel post's comment thread, oldest first, author and comment links per line, no membership in the discussion group needed; `post` may be left out when a cursor continues the thread | compact lines + `next_cursor` |
| `search_messages(query, chat? \| folder?, period…, from_me?, cursor?, limit?)` | Server-side text search, in one chat, in a folder's chats, or globally | compact lines with chat name + link |
| `export_chat(chat, period…, format="md")` | Write a conversation to a Markdown file | file path, message count, date range |
| `find_deleted_users()` | 1:1 dialogs whose peer account is deleted (archived included; under `public` only switched-on private chats). Chats holding only the "joined Telegram" notice are skipped and counted; per-chat checks stop after 30 s, later chats are listed as not checked | one line per chat: id, message count, first → last date, archived flag, name guesses from contact cards; totals in the first line |
| `export_deleted_users()` | One Markdown file per deleted-user dialog in `<EXPORT_DIR>/deleted-users/` (join-notice-only chats skipped); idempotent (complete files skipped, unfinished resumed), one 40 s budget per call | one line per chat with path + count; `Next: call export_deleted_users()` while chats remain |
| `download_media(chat, message_id)` | Save one message's media to the download dir | file path, size |

**Compact message line** (builder may refine, must stay token-cheap):
```
#4812 2026-10-25 14:03 Anna (@anna_k): text here… [photo 1.2MB] ↩#4807
```

**Output budget:** each call returns at most `TG_BRIDGE_MAX_CHARS` (default 12 000)
characters. When there's more, it returns `next_cursor` and says so explicitly in the
text, so the model knows to page or narrow the period.

**Long-period summaries:** the model pages through `get_messages`. If that turns out
too weak for a ~30B model, a later feature can add summarisation, but only via MCP
sampling (the client's model does the work); the bridge itself never calls an LLM (roadmap).

**Errors** are returned as short human-readable tool results, never stack traces:
chat not found (with closest matches), ambiguous chat (candidates), FloodWait
(waits automatically if ≤ 30 s, otherwise reports the wait time), not logged in
(points to the login script), any other Telegram refusal (its code plus a plain reason, e.g.
`CHANNEL_PRIVATE`: "the channel … is private, or this account was removed or banned there").

**Scope** (feature 17): `TG_BRIDGE_SCOPE=public` (the default): public channels and supergroups (with a
`@username`) are always visible; every other chat (people, bots, Saved Messages, private groups and channels) is hidden
until the owner switches it on. A person's `@username` never makes a 1:1 chat public. `all` keeps everything visible. A hidden chat does not exist for the model: it is never a resolution
candidate or suggestion, and asking for it gives the not-found text of a missing chat plus one line saying the bridge
runs in public scope. Under `public`, `list_chats` ends with the same line.

## 8. Security & privacy

- **Read-only by construction:** `ReadOnlyTelegram` exposes no write methods; a test
  fails if any module other than `telegram/client` imports Telethon. Reading uses
  methods that do **not** send read receipts (`iter_messages`, never
  `send_read_acknowledge`), and the client never calls `account.UpdateStatus`.
  Builder checks whether connecting shows the account "online", avoids it if cheap
  (e.g. `receive_updates=False`), and documents the finding. Unavoidable "online" is accepted.
- **Secrets:** api_id/api_hash and session live in `~/.config/tg-bridge/` (dir `700`,
  files `600`), outside the repo. `.gitignore` covers `*.session`, `.env*`, exports.
- **Logging:** to **stderr only** (stdout belongs to MCP stdio). Never log message
  text, names or phone numbers. Only counts, chat ids and durations.
- **Exports/downloads** go to configurable dirs outside the repo
  (defaults: `~/tg-bridge-data/exports`, `~/tg-bridge-data/downloads`).
- **Network mode (feature 11)** is the only MCP network listener, and it is strict:
  - **Bearer token** on every MCP request (`<session stem>.mcp-token`, `600`, generated by `tg-bridge-net token --new`);
    exactly one `Authorization: Bearer` header, constant-time compare, 401 otherwise. No token (or one shorter than 32
    characters) means every request is refused and `serve` refuses to start. The token is re-read when the file changes,
    so a rotation applies at once. Logs say only "mcp auth refused".
  - **Explicit bind:** `TG_BRIDGE_MCP_HOST` is required; `0.0.0.0`, `::`, `*` and empty are refused. The `Host` header
    is checked against it (the SDK's DNS-rebinding protection). Plain HTTP on the LAN is accepted; the Proxmox firewall
    allows the port from the agent's IP only.
  - **`scope=all` is refused** unless `TG_BRIDGE_NET_ALLOW_ALL=1`.
  - **Two separate keys:** the MCP token never opens the admin page and the admin cookie never opens MCP. The page binds
    `127.0.0.1` only (reached through `tailscale serve`, so the cookie is `Secure`), checks `Host` and derives its cookie
    and CSRF token from `<session stem>.admin-token` on every request.
  - **Bot-chat guard:** the chat with the Homelab bot holds login links. Its id (the numeric prefix of the bot token)
    is hidden under `public` even if allowlisted, and the page shows it locked with no switch.
  - **Login link:** sent through the Bot API with link previews off. Failures log the exception class or the status
    code only, because `urllib` errors carry the URL that contains the bot token.
  - **Exit codes:** `2` config error, `3` not logged in; neither may restart (`RestartPreventExitStatus=2 3`), so a
    revoked session never loops or spams links.
- **Scope (feature 17):** the rule is enforced in code, never by a prompt. `ScopedSource` wraps `MessageSource`:
  it filters `list_dialogs`, refuses every chat-taking call for a hidden chat id (cursors carry chat ids) and drops
  hidden hits from global search, topping the page up from the next raw pages (bounded). Fail closed: an unknown kind,
  a missing username or an unreadable allowlist file means hidden. The allowlist
  (`<session stem>.allowlist.json` next to the session, dir `700`, file `600`) is written only by the admin page and
  re-read on every call, so a switch takes effect without a restart.
- **Admin page (`tg-bridge-admin`):** a separate stdlib HTTP server on `127.0.0.1` only (`TG_BRIDGE_ADMIN_PORT`).
  It prints a one-time login link with a random token to the terminal; the link sets an HttpOnly, SameSite=Strict
  cookie and every other route needs it. POSTs carry a CSRF token; chat titles (attacker-controlled) are
  HTML-escaped; a CSP forbids anything but the inline style and one nonce'd script; the request log is off because
  it would contain the token. The page has tabs (Groups, Channels, People, Mine); typing in the search box filters the visible rows live in the browser (rows carry their normalized title in a `data-t` attribute, so the script holds no chat data; the Filter button still works server-side); switching on a person, a bot or
  Saved Messages asks for confirmation (generic text in the nonce'd script, no chat data in JS) and switched-on 1:1
  chats are marked "shared with cloud". Small chat photos are cached on disk (`<session stem>.photos/`, dir `700`,
  files `600`, named by chat id and photo id, so only a changed photo is downloaded again); a second Refresh within
  10 s is ignored. It reads Telegram only at start and on Refresh, then releases the session. Phone access goes through `tailscale serve`, not through a wider bind.

## 9. Testing

- TDD; `pytest`. No real account in tests, ever.
- `FakeSource` implementing `MessageSource` for tool/format tests.
- Telethon wrapped and mocked at the `ReadOnlyTelegram` boundary.
- Must-have test areas: period maths (DST transitions, `days=1/2`, inclusive `until`),
  budget/pagination, chat resolution (id / @username / fragment / ambiguous),
  deleted-user detection, export file content, "no Telethon import outside client".
- One manual smoke checklist per feature (run by a human against the real account).

## 10. Future stages (not designed in detail yet)

| Stage | Module | Notes |
|---|---|---|
| 2 | **Recorder + ArchiveSource** on the home server (LXC) | Always-on listener: new messages, edits (versions), deletions, profile snapshots → SQLite (network MCP with auth already exists: feature 11) |
| 2b | **Backfill** | Throttled, resumable history import into the archive |
| 3 | **Semantic search** | Embeddings via a local model (host decided in its brief), `sqlite-vec` |
| 4 | **Phone access** | Covered by the owner's agent Hermes as an MCP client of the networked bridge with `scope=public` (features 11, 17) |
| 5 | **Channel/group discovery** by interests | Searches outside existing chats |

## 11. Clarifications (from builder review; Claude's calls unless marked)

- **Tool output is plain text** (no structured MCP content) for maximum client compatibility.
  If LM Studio truncates results below the 12 000-char default, lower the default in feature 02.
- **Scenario 1 is keyword-driven in stage 1:** the model runs several `search_messages`
  calls with variants/synonyms (in all relevant languages). For a 1:1 chat it may also page
  through `get_messages`. Both `search_messages` and `get_messages` get a `from_me: bool`
  filter. Quality is limited until semantic search (stage 3), and that's accepted.
- **Global search** (`chat` omitted) means Telegram's search across **the user's own dialogs**
  only (`messages.SearchGlobal`). It never returns public chats he isn't in.
- **`topic`** accepts a topic id or title fragment from `get_chat_info`.
- **Deleted users:** only 1:1 dialogs, incl. archived and all folders. Message count via a
  cheap per-dialog total query, and only for dialogs already detected as deleted. Not "few": the owner has ~240,
  almost all holding only the "joined Telegram" notice; those are skipped from the dialog list (see roadmap Log, 05).
  A slow first scan is acceptable. Dialog list is cached per process.
- **Exports:** filenames are sanitised slugs + id + period + timestamp. They **never
  overwrite**. No size cap (it's a file, not model context). Layout is defined in feature 04.
- **`MessageSource`** is designed for stage 1 only. Stage 2 may extend it.
- Login script and credential source are specified in setup.md §1–2.

## 12. Left to the builder

- Exact library versions, package layout details, and whether to use `uv` (recommended).
- Exact compact-line format and default limits, within the budget rule.
- Fuzzy-matching approach for chat titles.
- Pagination cursor encoding.
- Recommended LM Studio model (test 2–3 tool-calling models around 30B and note results in setup.md).
