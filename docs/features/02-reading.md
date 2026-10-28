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
- [x] `days=2` starts exactly at yesterday 00:00.
- [x] The unread counter in the Telegram app doesn't change after reading.
- [x] A busy group returns `next_cursor` and the model continues paging.
- [x] Forum: topic by title and by id; topic #1 (General) works or gives a readable error.
- [x] Forum: `get_chat_info` lists #1 General; `topic=1` (and `topic="General"`) returns General's messages, none from other topics, and `next_cursor` pages on in General.
- [x] since="yesterday" until="yesterday" returns only yesterday.
- [x] LM Studio shows the full ~12 000-char result (design §11; if it truncates, lower TG_BRIDGE_MAX_CHARS default).
- [ ] A FloodWait (if one happens) is reported as "retry in N s".
