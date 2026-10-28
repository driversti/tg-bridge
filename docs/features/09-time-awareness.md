# 09 — Time awareness

**Design refs:** §7 (period args), §11 · **Depends on:** 02

## Goal
The local model often doesn't know today's date (it guesses from its training cutoff). Questions like *"news this week"* or *"who signed for **next year**"* then go wrong: the model may send `since="2024-…"` or misread "next year". The bridge already does period maths. This feature makes sure the model **always sees the current date** and can name calendar periods without computing them.

## Scope
- **"Now" line in every tool result:** each result starts with one short line, e.g. `Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)`. ~40 chars, counted inside the budget. The model sees it after its first tool call, whatever that call is.
- **`now()` tool:** date, weekday, time, time zone, ISO week. Its description tells the model to call it first when a question depends on "today", "this week", "next year", etc.
- **Server `instructions`** (MCP initialize) include the current date. LM Studio restarts the server per message (see Log, 01), so it stays fresh. Builder checks whether LM Studio actually shows instructions to the model and notes the result in the Log.
- **More period keywords** in `timerange` (all Europe/Warsaw, weeks start Monday):
  `this_week`, `last_week`, `this_month`, `last_month`, `this_year`. Each works as `since` and also as a whole-period shorthand (`period="this_week"`) for every tool that takes period args. The error message lists all keywords.
- Tool descriptions say: "prefer `days`/keywords over computing dates yourself".

## Out of scope
- Changing LM Studio's system prompt or chat template.

## Done when
- Keyword ranges unit-tested (week boundary on Monday, month and year boundaries, DST days).
- The "Now" line is present in every tool's output (one test over all registered tools).

## Smoke checklist (manual, real account)
- [x] In a fresh LM Studio chat, "what's the date today?" is answered correctly after one tool call.
- [x] "News in folder F1 this week" (checked on a single chat: folders come in 07/08) uses `this_week` (or `now()` + correct dates), not a stale year.
- [x] "Who signed for next year?" treats next year as 2027 (as of 2026).
