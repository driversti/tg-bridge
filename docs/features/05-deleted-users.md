# 05 — Deleted users

**Design refs:** §2 scenario 3, §7, D9 · **Depends on:** 04

## Goal
Find every 1:1 dialog whose peer account is now deleted and save each conversation to its own file.

## Scope
- `find_deleted_users()`: scan all dialogs (incl. archived) for users with `deleted=True`; return chat id, user id, any identity still available, message count, first/last message date.
- Best-effort identity recovery for files: names seen in forwards/replies/mentions within the chat, if any. Mark it clearly as a "guess".
- `export_deleted_users(dir?)`: reuse 04's exporter, one file per user, into `<EXPORT_DIR>/deleted-users/`; returns list of paths + counts.
- Note in the tool description: dialogs the user already deleted on their side cannot be recovered.

## Done when
- Detection and per-user export tested with FakeSource.
- From LM Studio: "find my chats with deleted accounts and export them" produces the files.

## Smoke checklist (manual, real account)
- [ ] The count matches the "Deleted Account" dialogs visible in the Telegram app.
- [ ] Each file contains the full history.
