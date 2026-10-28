# 03 — Search

**Design refs:** §7 (`search_messages`) · **Depends on:** 02

## Goal
Find messages by text in one chat or across all chats. Scenario 1.

## Scope
- `search_messages(query, chat?, days|since|until, from_me?, cursor?, limit?)`: Telegram server-side search (per-chat, or global when `chat` is omitted).
- Results reuse the compact line format, prefixed with chat title, and always include a message link/id so the model can call `get_messages` around a hit.
- Optional `from_me: bool` filter (useful for "when did I write X").
- Global mode = the user's own dialogs only (`SearchGlobal`), never public discovery (design §11).
- `get_messages(chat, around=<message id>)`: the messages just before and after a hit (default 20), the hit marked `▶`; `Older:`/`Newer:` cursors continue from there.
- Document the limits of server-side search (word-based, no semantics) in the tool description so the model tries synonyms/variants itself.

## Done when
- Tool logic tested with FakeSource.
- From LM Studio: "find in my chat with B when I mentioned my address" returns hits with dates.

## Smoke checklist (manual, real account)
- [x] Per-chat and global search both return results.
- [x] Ukrainian/Russian/Polish text is found.
- [x] Paging: page 2 of a global search has no duplicates or gaps against page 1 (validates the `offset_rate` = date choice).
- [x] A hit inside an **archived** chat shows up in global search (if not: note it; `folder_id=1` would need a second request).
- [x] `get_messages(around=<hit id>)` from a search result shows the context with `▶` on the hit.
- [x] Global `from_me` finds an old message of yours.
