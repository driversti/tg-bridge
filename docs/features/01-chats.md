# 01 — Chats

**Design refs:** §6 (models), §7 (`list_chats`, `get_chat_info`, chat arguments) · **Depends on:** 00

## Goal
The model can find any dialog (personal, group, channel, archived) and identify it unambiguously.

## Scope
- `list_chats(query?, kind?, limit=30)` incl. archived dialogs; flags `deleted_peer`.
- `get_chat_info(chat)`: kind, title, description, members count, peer identity (all available identity fields per D9), forum topics list.
- **Chat resolver** (shared by all later tools): numeric id → `@username` → title fragment (fuzzy). Ambiguous → return candidates; not found → closest matches.
- Dialog list caching within a server process (dialogs are slow to fetch); simple TTL is fine.

## Done when
- Resolver unit-tested for id / @username / fragment / ambiguous / not found (FakeSource).
- From LM Studio: "show my chats with 'family' in the name" works.

## Smoke checklist (manual, real account)
- [x] A private group and a private channel appear in the list.
- [x] An ambiguous name returns candidates instead of a guess.
