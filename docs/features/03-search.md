# 03 — Search

**Design refs:** §7 (`search_messages`) · **Depends on:** 02

## Goal
Find messages by text in one chat or across all chats. Scenario 1.

## Scope
- `search_messages(query, chat?, days|since|until, cursor?, limit?)`: Telegram server-side search (per-chat, or global when `chat` is omitted).
- Results reuse the compact line format, prefixed with chat title, and always include a message link/id so the model can call `get_messages` around a hit.
- Optional `from_me: bool` filter (useful for "when did I write X").
- Global mode = the user's own dialogs only (`SearchGlobal`), never public discovery (design §11).
- Document the limits of server-side search (word-based, no semantics) in the tool description so the model tries synonyms/variants itself.

## Done when
- Tool logic tested with FakeSource.
- From LM Studio: "find in my chat with B when I mentioned my address" returns hits with dates.

## Smoke checklist (manual, real account)
- [ ] Per-chat and global search both return results.
- [ ] Ukrainian/Russian/Polish text is found.
