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
| 03 | Search: `search_messages` | [features/03-search.md](features/03-search.md) | 02 | [ ] |
| 04 | Export: `export_chat` → Markdown | [features/04-export.md](features/04-export.md) | 02 | [ ] |
| 05 | Deleted users: `find_deleted_users`, `export_deleted_users` | [features/05-deleted-users.md](features/05-deleted-users.md) | 04 | [ ] |
| 06 | Media: `download_media` | [features/06-media.md](features/06-media.md) | 02 | [ ] |

Stage 1 is done when scenarios 1–4 from `design.md` §2 work end-to-end from LM Studio.

## Later stages (briefs written when we get there)

- [ ] 10 Recorder + SQLite archive on Jetson (edits, deletions, profile snapshots)
- [ ] 11 Network MCP with auth (Mac → Jetson)
- [ ] 12 Backfill into archive
- [ ] 13 Semantic search (Ollama embeddings, sqlite-vec)
- [ ] 14 Telegram bot interface
- [ ] 15 Channel/group discovery
- [ ] ? Map-reduce `summarize` tool (only if paging proves too weak)

## Log

_(deviations from design, one line each)_
- 01: dialog cache is on disk (5 min TTL), not per process: LM Studio restarts the server for every message (design §11 said "cached per process").
- 02: chat link base shown once in the page header ("Links:") instead of a per-message Message.link; days shown under day headers.
- 02: cursor is a short readable token (`f6782037_s1792969200_c795`) instead of base64 JSON: the smoke test caught a ~30B model splicing an earlier chat's id into a cursor. The footer now says "…remain… Next: call get_messages(…)" because the model stopped after page 1.
