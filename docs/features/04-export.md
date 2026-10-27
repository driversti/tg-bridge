# 04 — Export

**Design refs:** §6 (`export`), §7 (`export_chat`), §8 (export dir) · **Depends on:** 02

## Goal
Write a conversation (or period of it) to a readable Markdown file, so the user or another model gets the full text without it passing through the model's context. Supports scenario 4 and is reused by 05.

## Scope
- `export_chat(chat, days|since|until, format="md")` → writes `<EXPORT_DIR>/<chat-slug>_<from>_<to>.md`, returns path + message count + date range (not contents).
- File header: chat title, kind, all identity data of the peer (D9), export time, period.
- Body: messages grouped by day, full text (no budget), media as `[photo · name · size · link]`, replies/forwards marked.
- Streams messages page by page (no full-history load into memory). Respects FloodWait.
- Files `600`, export dir created if missing.

## Done when
- Markdown output snapshot-tested with FakeSource.
- From LM Studio: "export my chat with A for October" creates the file.

## Smoke checklist (manual, real account)
- [ ] The exported file opens nicely in Obsidian or an editor.
- [ ] A large chat exports without the model's context growing.
