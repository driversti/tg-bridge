# Roadmap

Status: `[ ]` todo · `[~]` in progress · `[x]` done. **This file is the single source of truth for status.**

## Workflow per feature (token-saving)

1. **Plan session (Opus):** read `CLAUDE.md` → `docs/design.md` §6–9 → the feature brief.
   Write the implementation plan to `docs/plans/NN-<slug>.md`. Set status `[~]`.
2. **Implement session (Sonnet):** read `CLAUDE.md` → the plan. Implement with TDD,
   keep the suite green, update the brief's "Smoke checklist" if needed.
3. **Finish:** the maintainer runs the smoke checklist, then the feature gets `[x]` here, plus a one-line
   note under "Log" if something deviated from the design.

Briefs are deliberately short. The plan session turns them into detail, so don't
pre-plan future features here.

## Stage 1: Live bridge (Mac, stdio)

| # | Feature | Brief | Depends on | Status |
|---|---|---|---|---|
| 00 | Foundation: skeleton, config, login, read-only client, MCP server, `whoami` | [features/00-foundation.md](features/00-foundation.md) · [plan](plans/00-foundation.md) | — | [x] |
| 01 | Chats: `list_chats`, `get_chat_info`, chat resolution | [features/01-chats.md](features/01-chats.md) · [plan](plans/01-chats.md) | 00 | [x] |
| 02 | Reading: `get_messages` + periods, budget, pagination | [features/02-reading.md](features/02-reading.md) · [plan](plans/02-reading.md) | 01 | [x] |
| 03 | Search: `search_messages` | [features/03-search.md](features/03-search.md) · [plan](plans/03-search.md) | 02 | [x] |
| 04 | Export: `export_chat` → Markdown | [features/04-export.md](features/04-export.md) · [plan](plans/04-export.md) | 02 | [x] |
| 05 | Deleted users: `find_deleted_users`, `export_deleted_users` | [features/05-deleted-users.md](features/05-deleted-users.md) · [plan](plans/05-deleted-users.md) | 04 | [~] |
| 06 | Media: `download_media` | [features/06-media.md](features/06-media.md) · [plan](plans/06-media.md) | 02 | [x] |
| 07 | Folders: `list_folders`, `list_chats(folder=)`, folders in `get_chat_info` | [features/07-folders.md](features/07-folders.md) · [plan](plans/07-folders.md) | 01 | [x] |
| 08 | Folder queries: `search_messages(folder=)`, `get_messages(folder=)`, `mode="headlines"` + dedupe | [features/08-folder-queries.md](features/08-folder-queries.md) · [plan](plans/08-folder-queries.md) | 07, 03 | [x] |
| 09 | Time awareness: "Now" line in results, `now()`, `this_week`/`last_month`/… keywords | [features/09-time-awareness.md](features/09-time-awareness.md) · [plan](plans/09-time-awareness.md) | 02 | [x] |
| 16 | Channel post comments: `get_comments(chat, post)` via `GetReplies`, no discussion-group membership needed; `💬N` on post lines | [features/16-channel-comments.md](features/16-channel-comments.md) · [plan](plans/16-channel-comments.md) | 02 | [x] |

Stage 1 is done when scenarios 1–4 from `design.md` §2 work end-to-end from LM Studio.

## Later stages (briefs written when we get there)

- [ ] 10 Recorder + SQLite archive on the home server LXC (edits, deletions, profile snapshots)
- [x] 11 Network MCP with auth: the bridge in its own LXC, own session, streamable HTTP + bearer token for Hermes (LXC 110); admin page inside on loopback via `tailscale serve`, link via the Homelab bot · [features/11-network-mcp.md](features/11-network-mcp.md) · [plan](plans/11-network-mcp.md)
- [x] 17 Cloud scope: `TG_BRIDGE_SCOPE=public` (public chats always; any private chat, people included, only when switched on) and an owner-only allowlist page · [features/17-cloud-scope.md](features/17-cloud-scope.md) · [plan](plans/17-cloud-scope.md)
- [ ] 12 Backfill into archive
- [ ] 13 Semantic search (local embeddings, sqlite-vec)
- [x] 14 Phone access via Hermes (MCP client of 11 + 17). Mostly setup once 11 and 17 are done
- [ ] 15 Channel/group discovery
- [ ] ? Summarisation of large volumes, only via **MCP sampling** (the client's model does the work); the bridge itself never calls an LLM (decided 2026-10-28). Only if 08's headlines mode proves too weak.

## Log

_(deviations from design, one line each)_
- 01: dialog cache is on disk (5 min TTL), not per process: LM Studio restarts the server for every message (design §11 said "cached per process").
- 02: chat link base shown once in the page header ("Links:") instead of a per-message Message.link; days shown under day headers.
- 02: cursor is a short readable token (`f6782037_s1792969200_c795`) instead of base64 JSON: the smoke test caught a ~30B model splicing an earlier chat's id into a cursor. The footer now says "…remain… Next: call get_messages(…)" because the model stopped after page 1.
- 03: `get_messages` gained `around=<id>` so the model can read the context of a search hit; global search resumes from the boundary hit's date as `offset_rate` (smoke: 90 hits in one call = 3 pages of 30).
- 03: a short `SearchGlobal` page is not the end (Telegram drops hidden hits); only an empty page or a non-slice answer is.
- 03: global `from_me` uses `messages.Search` on `inputPeerEmpty` with `from_id=self` (server-side) instead of the plan's client-side filter with a 500-hit scan cap, which covered only ~10 days for channel-heavy words. In the smoke test it missed 1 of 21 own hits (a 2021 supergroup message, cause unknown).
- 04: LM Studio ends a tool call after ~60 s, so one `export_chat` call pauses after 40 s and the model continues with `resume=true`; the state sits in a hidden note at the end of the file (a ~55-char cursor got garbled on the 5th call). Smoke: 48 835 messages in 12 calls, ~14 min, one file.
- 04: message `#id`/`↩#id` are t.me links where the chat has them; `export_chat` is annotated not read-only (it writes a local file).
- 06: `DownloadResult` also carries `file_count` (the paused text needs "n/total files"); an album with a single downloadable file is treated as a plain single download.
- 07: `list_chats` gained `min_folders` (smoke: asked for chats in 2+ folders, the model started calling `get_chat_info` on all 888 chats instead of intersecting per-folder lists).
- 08: channels rewrite news instead of forwarding it, so whole-text overlap missed every real repost (smoke: 0 of 104 merged). Repost grouping also matches headlines (overlap ≥ 0.85, ≥ 4 tokens, ≤ 6 h apart); F1 × 7 days: 2 merges, no false ones.
- 08: F1 (4 channels) × 7 days = 179 posts → 102 headlines, 2 pages, 1.2–4.5 s per call; folder search 1.5 s. Telethon `wait_time=0` for reads/searches ≤ 3000 messages (it slept 1 s per history page).
- 08: the model did not fill `Links: …/<id>` into post links, so headline lines end with full post links (`#id` only for chats without links); recheck: 8 of 8 news items in the answer linked to real posts.
- 09: LM Studio does not pass the MCP server `instructions` to the model (smoke 2026-10-28: asked without tools, it answered "[current date]" and could not quote them), so the date reaches the model only via the `Now:` line and `now()`. Also beyond the brief: `last_year` keyword, `TG_BRIDGE_MAX_CHARS` ≥ 500.
- 09 (after 08's smoke): for "last 7 days"/"останній місяць" the model picked calendar `last_week`/`last_month`. Keywords are documented as `previous_week/month/year` (`last_*` kept as hidden aliases) and docstrings map rolling periods to `days`; recheck: days=7, days=30 (once after a first previous_month try), "минулого тижня" → previous_week.
- 16: first smoke failed on every tool with `database is locked`: tg-bridge was also a local MCP server in Claude Code, and its long-lived processes held the Telethon session's write lock. Removed it from Claude Code (never run two bridges on one session file); the error now reads as `SessionBusyError`.
- 16: a ~26B model (gemma-4-26b) never found `get_messages(comments_of=…)` at the end of a long docstring and said it cannot read comments. Comments became their own tool `get_comments`, and channel post lines show `💬N` plus one ready `get_comments` call per page; recheck: the model called `get_comments` on its own.
- 16: Telegram refusals (`RPCError`) carry a plain reason instead of a generic tool error; for comments the text says to skip the post and go on, so one refused thread does not end a series.
- 17: brief said DMs are never visible under `public`; after the first smoke the owner changed it to "public always, any private chat (people, bots, Saved Messages included) only when switched on"; switching on a 1:1 chat asks for confirmation.
- 17: the allowlist page is its own command `tg-bridge-admin`, not part of the MCP process (LM Studio starts a fresh stdio process per message). It touches Telegram only at start, on Refresh and in short icon batches (25 per session, 2 s pause), so it doesn't lock LM Studio out. Login: a stable token link (`--link`, `--new-link`) and a year-long cookie instead of Tailscale identity headers.
- 17: `t.me/c/<N>` without a message id opens Telegram's home page; private chat links use the dialog's last message id. People without a username get `tg://user?id=`.
- 17: global search drops hits from hidden chats and tops the page up with at most 5 extra raw pages; a word that lives mostly in DMs can end paging early (incomplete, never a leak).
- 17: smoke 2026-11-07 on the Mac: default `public` hid DMs, a switched-on group appeared, `all` restored everything; phone via `tailscale serve` and a `public` client reading one switched-on DM are rechecked with Hermes after 11.
- 11: implemented on `claude/feat-11-network-mcp-impl-1af11f`, deployed on CT 120 and smoke-checked by the owner (2026-11-07). Hermes compatibility (`mcp` 2.0.0 client against our 2.3 server) works in the SDK's default stateful/SSE mode. Deviation: the startup-order test makes the fake `serve` wait for the off-loop `send`, since the plan sends the link in an executor and the two are concurrent by design.
- 11: after the smoke the admin page got a live chat filter: typing in the search box hides non-matching rows in the browser (rows carry their normalized title in `data-t`; the Filter button still filters on the server).
- 14: no brief or plan needed: it was setup only, done and checked as part of 11's smoke with Hermes (2026-11-07).
- 08, 16: smoke checklists ticked only where the log records a result; the other items are marked "Not rechecked" (2026-11-07).
- 05: `export_deleted_users` has no `dir` argument (the brief had `dir?`): a model-supplied path could write anywhere on the host, and on CT 120 that host is a server. Files always go to `<EXPORT_DIR>/deleted-users/`.
- 05: identity guesses come only from contact cards whose `user_id` is the deleted peer (brief said forwards/replies/mentions): a deleted account's own name is gone everywhere, forward names can't be tied to the peer, and cards are the one place the name survives. Phones from cards are not taken.
- 05: first smoke (2026-11-08): `find_deleted_users` timed out in LM Studio. The account has ~240 deleted-account chats (design §11 assumed "few"), and Telegram throttles the per-chat reads with a FloodWait of ~25 s every ~60 chats, which Telethon sleeps through silently: 122 s for one call. Almost all of them held only the "joined Telegram" notice, so chats whose newest dialog message is `MessageActionContactSignUp` are now skipped for free from the dialog list (owner's choice), and the per-chat check stops after 30 s, listing the rest as "not checked". Dialog cache version 3.
