# 08 — Folder queries

**Design refs:** §7 (period args, compact line, output budget), D1 (bridge = pure data provider, no LLM inside) · **Depends on:** 07, 03

## Goal
Ask about a whole folder in one call, especially news-like channel folders:
- *"Who signed a contract for next year?"* → search across folder **F1**.
- *"Main F1 news this week?"* → a compact, de-duplicated digest of folder **F1** for 7 days that a ~30B model can hold in context.

The bridge stays a **pure data provider**: it shapes and shrinks data, but never calls an LLM (decided 2026-10-28).

## Scope
- **`search_messages(query, folder=…, …)`**: runs the per-chat search over every chat in the folder (07's membership), merges hits by date (newest first), and keeps the existing paging/cursor contract. `chat` and `folder` are mutually exclusive.
- **`get_messages(folder=…, days|since|until, mode=…)`**: reads all chats of the folder for the period, merged chronologically, each line prefixed with a short chat label.
- **`mode="headlines"`** (also available for single-chat `get_messages`): one line per post: date, chat label, first meaningful line of text cut at ~140 chars, link. Media-only posts are skipped unless they have a caption.
- **De-duplication** in headlines mode: near-identical posts across channels (same news reposted) collapse into one line with `×N` and the list of chat labels. Use a cheap normalised-text similarity (builder's choice). Unit-tested on reposts with small wording differences.
- **Volume guard:** if the period still exceeds the budget, return the first page plus a header line with totals (`412 posts in 6 chats, showing 1–90`) and the usual `Next:` hint. Never silently truncate.
- Rate limits: reading N chats is N request streams, so respect FloodWait and keep sequential order. Note the duration for the F1 folder × 7 days in the Log.

## Out of scope
- Server-side summarisation of any kind (no LLM calls). If headlines mode proves too weak, revisit via **MCP sampling** (server asks the client's model). Only an option if LM Studio supports it by then.
- Semantic search (stage 3).

## Done when
- Folder merge, headlines rendering and dedupe tested with FakeSource.
- From LM Studio: both example questions get sensible answers from folder F1.

## Smoke checklist (manual, real account)
- [ ] `get_messages(folder="F1", days=7, mode="headlines")` fits in 1–2 pages, and reposts of the same news show as one `×N` line.
- [ ] The model's "main news this week" answer matches what you remember from the channels.
- [ ] `search_messages("contract", folder="F1")` (plus the model's own variants) finds the signing news.
- [ ] A folder with a muted or archived chat behaves the same as in `list_chats(folder=…)`.
