# 02 — Reading

**Design refs:** §7 (period args, compact line, output budget, errors), §8 (no read receipts) · **Depends on:** 01

## Goal
Read any chat chronologically for a period, in a form a ~30B local model can consume without context overflow. Core of scenarios 2 and 4.

## Scope
- `timerange` module: `days`, `since`/`until` → `[start, end)` per design §7, Europe/Warsaw. Tests incl. DST change days (last Sunday of March/October).
- `format` module: compact line rendering (sender name + @username, media summary, reply/forward markers, link), char budget, opaque `next_cursor`.
- `get_messages(chat, days|since|until, topic?, from_me?, cursor?, limit?)`: chronological order (oldest → newest within the page).
- FloodWait handling (auto-wait ≤ 30 s, else report).
- Must not mark messages as read.

## Done when
- Period maths, budget truncation and cursor round-trip unit-tested.
- From LM Studio: "summarise chat C for the last two days" pages correctly through a busy group.

## Smoke checklist (manual, real account)
- [ ] `days=2` starts exactly at yesterday 00:00.
- [ ] The unread counter in the Telegram app doesn't change after reading.
- [ ] A busy group returns `next_cursor` and the model continues paging.
