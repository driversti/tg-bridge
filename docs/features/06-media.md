# 06 — Media download

**Design refs:** §7 (`download_media`), D8 · **Depends on:** 02

## Goal
Download one message's media on explicit request. Nothing is downloaded automatically.

## Scope
- `download_media(chat, message_id)` → saves into `<DOWNLOAD_DIR>/<chat-slug>/`, returns path, size, mime type.
- Configurable size limit (default 200 MB). Above it, refuse and report the size.
- Never downloads as a side effect of other tools.

## Done when
- Tool logic tested with a mocked client.
- From LM Studio: "download the photo from message #4812 in chat C" saves the file.

## Smoke checklist (manual, real account)
- [ ] A photo and a document download correctly.
