# 06 — Media download

**Design refs:** §7 (`download_media`), D8 · **Depends on:** 02

## Goal
Download one message's media on explicit request. Nothing is downloaded automatically.

## Scope
- `download_media(chat, message_id)` → saves into `<DOWNLOAD_DIR>/<chat-slug>_<id>/`, returns path, size, mime type, never the content.
- Configurable size limit (`TG_BRIDGE_MAX_DOWNLOAD_MB`, default 200). Above it, refuse and report the size.
- Big files continue across calls (~40 s per call, same arguments again), like `export_chat`.
- A message in an album downloads the whole album into `album_<grouped_id>/`.
- Never overwrites; asking again returns the existing file.
- Never downloads as a side effect of other tools.

## Done when
- Tool logic tested with a mocked client.
- From LM Studio: "download the photo from message #4812 in chat C" saves the file.

## Smoke checklist (manual, real account, from LM Studio)
- [x] A photo and a document download correctly and open.
- [x] Asking again returns "Already downloaded", no second file.
- [x] A file > ~100 MB (under the limit) finishes over several calls into one file; no `.part` left.
- [x] An album message saves all its files into `album_<id>/`.
- [x] A file over `TG_BRIDGE_MAX_DOWNLOAD_MB` is refused with its size.
- [x] A voice message saves as `.ogg`.
