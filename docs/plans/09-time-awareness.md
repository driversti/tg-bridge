# 09 — Time Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The model always sees today's date and can name calendar periods without computing them: a `Now:` line heads every tool result, a `now()` tool, the date in the server `instructions`, and calendar keywords (`this_week`, `last_month`, …) for every period argument.

**Architecture:** All date logic stays in `tg_bridge/timerange.py` (pure, Telethon-free): the keyword table, `now_line`, `now_details`. `parse_period` gains a `period` argument and keyword support in `since`/`until`. `server.py` stamps the `Now:` line on every result and error in one helper, passes a shared `now` clock to the tools, and hands the tools a `Settings` whose `max_chars` already excludes the line. No source, cursor or Telegram changes.

**Tech Stack:** unchanged (Python ≥3.12, `mcp` v2 `MCPServer` (has `instructions=`), `pytest-asyncio`).

**Spec:** `docs/features/09-time-awareness.md` + `docs/design.md` §6 (`timerange`), §7 (period args, budget), §9 (DST tests). Pattern to copy: existing `parse_period` / `_midnight` (date arithmetic on the local date, then `_midnight`, so DST is right by construction).

## Global Constraints

All constraints from plans 00–06 still apply. Additionally:

- **Time zone** is `settings.timezone` everywhere (default Europe/Warsaw); never the machine's local zone, never `datetime.now()` without `UTC`.
- **Weekday and month names are fixed English tuples**, not `strftime("%a")`/`%B`: those follow the process locale.
- **Budget:** the `Now:` line plus its newline count inside `TG_BRIDGE_MAX_CHARS`. Tools receive `replace(settings, max_chars=settings.max_chars - NOW_RESERVE)`; the stamped text must be `<= settings.max_chars`.
- **One clock per call:** the `Now:` line and that call's period maths use the same `now()` value.
- Ask the maintainer before every commit. Commit steps below are proposals.

## Decisions taken here (Claude's calls)

- **Keywords:** `today`, `yesterday`, `this_week`, `last_week`, `this_month`, `last_month`, `this_year`, `last_year`. `last_year` is beyond the brief: one table row, and "last year" is as common as "this year". Weeks start Monday. Each keyword is a local-date range `[first_day, end_day)`:
  | keyword | first day | end day (exclusive) |
  |---|---|---|
  | `today` | today | tomorrow |
  | `yesterday` | today − 1 | today |
  | `this_week` | Monday of this week | Monday of next week |
  | `last_week` | Monday − 7 | Monday of this week |
  | `this_month` | 1st of this month | 1st of next month |
  | `last_month` | 1st of previous month | 1st of this month |
  | `this_year` | Jan 1 | Jan 1 next year |
  | `last_year` | Jan 1 last year | Jan 1 this year |
- **Where keywords work:** `since=<kw>` → the range's start; `until=<kw>` → the range's end (inclusive of the whole period, consistent with date-only `until`); `period=<kw>` → the whole range, except that an end in the future becomes `None` ("→ now"), so `this_*` and `today` read "… → now" in headers, exactly like `days=1`. Mixed use is fine: `since="last_month", until="yesterday"`.
- **Keyword spelling:** case-insensitive; spaces and hyphens are read as `_` (`"this week"`, `"Last-Month"`). Small models do this.
- **`period` is exclusive:** `period` with `days`, `since` or `until` → `PeriodError("Use one of `period`, `days` or `since`/`until`.")`. Unknown `period` → `PeriodError` listing all keywords.
- **Error text** for bad `since`/`until` (`_bad`) lists all keywords: `Cannot read `since`. Use YYYY-MM-DD, "YYYY-MM-DD HH:MM" or a keyword: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_year.`
- **`period` argument** is added to `get_messages`, `search_messages`, `export_chat` (all tools with period args). A cursor still wins over every period argument; `export_chat(resume=true)` still ignores them. Cursors are unchanged (they store `start`/`end`).
- **`Now:` line format:** `Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)`: `tz.key`, no seconds. Its length is constant for a tz, so `NOW_RESERVE = len(now_line(<any>, tz)) + 1` is computed once in `build_server`.
- **Errors are stamped too:** a `ToolError` from a tool is re-raised as `ToolError(f"{line}\n{e}")`. Period errors are exactly where the model needs the date. SDK-level argument validation errors are not ours and stay unstamped.
- **`now()` tool** (read-only, no arguments). The wrapper adds the `Now:` line; the tool body returns the rest:
  ```
  Today: Wednesday 2026-10-28 · ISO week 2026-W44 · UTC+01:00
  This week: 2026-10-26 Mon → 2026-11-01 Sun · last week: 2026-10-19 → 2026-10-25
  This month: 2026-10 (October) · last month: 2026-09 (September)
  This year: 2026 · last year: 2025 · next year: 2027
  Period keywords: today, yesterday, this_week, last_week, this_month, last_month, this_year, last_year
  ```
  ISO week uses `date.isocalendar()` and prints its own year (`2027-01-01` → `2026-W53`). UTC offset as `±HH:MM` from `now.astimezone(tz).utcoffset()`. Week/month end dates shown inclusive (Sun, last day).
- **Server `instructions`** (built once in `build_server` from `now()`; LM Studio restarts the server per message, so it stays fresh):
  `Read-only access to the user's Telegram account. Today is Wednesday 2026-10-28, 14:03 Europe/Warsaw (ISO week 2026-W44). Your own sense of the date is out of date: for anything relative to today ("this week", "last month", "next year", ages, deadlines) use this date or call now(). For periods prefer the `period` keywords or `days` over computing dates yourself.`
- **Minimum budget:** `TG_BRIDGE_MAX_CHARS` must be ≥ 500 (was > 0), else `ConfigError("TG_BRIDGE_MAX_CHARS must be an integer of at least 500")`, so the reserve can never leave a tool a zero or negative budget. Update `docs/setup.md` if it states the range.

## File Structure

| File | Change |
|---|---|
| `tg_bridge/timerange.py` | `KEYWORDS`, keyword ranges, `period` arg, `now_line`, `now_details`, `iso_week`, new `_bad` text |
| `tg_bridge/config.py` | `TG_BRIDGE_MAX_CHARS` ≥ 500 |
| `tg_bridge/tools.py` | `period` kwarg on `get_messages`, `search_messages`, `export_chat`; `now()` body |
| `tg_bridge/server.py` | `now` clock param, `_stamped`, budget-reduced settings, `instructions`, `now` tool, `period` params, docstrings |
| `tests/test_timerange.py`, `tests/test_config.py`, `tests/test_tools*.py`, `tests/test_server.py` | tests below |

## Review Focus

1. **`last_week` across the DST end** (now = Wed 2026-10-28): start `2026-10-18 22:00Z`, end `2026-10-25 23:00Z`. A `timedelta` on an aware datetime would be off by an hour (Task 1).
2. **Monday itself** is in `this_week`, Sunday 23:59 is in `last_week` when today is Monday (Task 1).
3. **January** `last_month` = December of the previous year; **March 1** `last_month` covers Feb 29 in a leap year (Task 1).
4. **Every registered tool's result and error starts with `Now: `** and stays within `max_chars` (Task 3).
5. **Existing exact-text tests in `test_server.py`** now get the `Now:` line first; update them, don't weaken them (Task 3).

---

### Task 1: Calendar keywords in `timerange`

**Files:**
- Modify: `tg_bridge/timerange.py`
- Test: `tests/test_timerange.py`

**Interfaces:**
- Produces:
  ```python
  KEYWORDS: tuple[str, ...]  # in the table order above
  def parse_period(days, since, until, tz, now, period: str | None = None) -> Period  # period last, keeps old calls valid
  ```
  Internals (names are a suggestion): `_keyword(raw) -> str | None` (normalise case, spaces, hyphens; `None` if not a keyword), `_keyword_days(kw, today) -> tuple[date, date]` (the table), reused by `_parse_moment` for `since`/`until` (replacing the `today`/`yesterday` branch).

- [ ] **Step 1: Failing tests** (now = `datetime(2026, 10, 28, 14, 3, tzinfo=W)` unless stated; extend the `parse` helper with `period=`):
  - `test_period_this_week_starts_monday` → start `utc(2026, 10, 25, 23)`, end `None`.
  - `test_period_last_week_across_dst_end` → `(utc(2026, 10, 18, 22), utc(2026, 10, 25, 23))`.
  - `test_monday_is_this_week` (now Mon 2026-10-26 00:30 → this_week start = that midnight) and `test_sunday_night_is_last_week_on_monday` (now Mon 2026-11-02 10:00 → last_week end = `utc(2026, 11, 1, 23)`).
  - `test_this_month_last_month` (`this_month` start `utc(2026, 9, 30, 22)`; `last_month` = `(utc(2026, 8, 31, 22), utc(2026, 9, 30, 22))`).
  - `test_last_month_in_january` (now 2027-01-15 → `(utc(2026, 11, 30, 23), utc(2026, 12, 31, 23))`).
  - `test_last_month_leap_february` (now 2028-03-01 12:00 → end `utc(2028, 2, 29, 23)`, start `utc(2028, 1, 31, 23)`).
  - `test_this_year_last_year` (`this_year` start `utc(2025, 12, 31, 23)`, end `None`; `last_year` = `(utc(2024, 12, 31, 23), utc(2025, 12, 31, 23))`).
  - `test_last_week_across_dst_start` (now Wed 2026-04-01 → `(utc(2026, 3, 22, 23), utc(2026, 3, 29, 22))`).
  - `test_period_yesterday_is_closed`, `test_period_today_is_open`.
  - `test_keywords_in_since_until` (`since="last_month", until="last_month"` == `period="last_month"`; `until="this_week"` → `utc(2026, 11, 1, 23)`).
  - `test_keyword_spelling_variants` (`"This Week"`, `"this-week"`, `" this_week "` all equal `period="this_week"`).
  - `test_period_with_days_or_since_rejected`, `test_unknown_period_lists_keywords` (`match="last_year"`), `test_bad_since_lists_keywords`.
  - Existing tests stay green (`test_keywords_today_yesterday`, `test_garbage_since_rejected`).
- [ ] **Step 2: Run** `uv run pytest -q tests/test_timerange.py` → FAIL.
- [ ] **Step 3: Implement.** Date arithmetic on local `date`s only, then `_midnight`. Month starts: `today.replace(day=1)`; previous month start: `(first - timedelta(days=1)).replace(day=1)`; next month start: `(first + timedelta(days=32)).replace(day=1)`. For `period=`, an end day after today → `end=None`.
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(time): calendar period keywords (this_week, last_month, …)`.

### Task 2: `now_line`, `now_details`, minimum budget

**Files:**
- Modify: `tg_bridge/timerange.py`, `tg_bridge/config.py`, `docs/setup.md` (only if it states the range)
- Test: `tests/test_timerange.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  ```python
  def now_line(now: datetime, tz: ZoneInfo) -> str      # "Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)"
  def now_details(now: datetime, tz: ZoneInfo) -> str   # the 5 lines shown in "Decisions"
  def iso_week(day: date) -> str                        # "2026-W44"
  ```
- [ ] **Step 1: Failing tests:** `test_now_line_format` (exact string above, from `utc(2026, 10, 28, 13, 3)`); `test_now_line_uses_local_date_after_utc_midnight` (`utc(2026, 10, 28, 23, 30)` → `2026-10-29 Thu 00:30`); `test_now_line_length_constant` (same length for a Monday in January and a Wednesday in October); `test_now_details_exact` (exact 5 lines above); `test_now_details_summer_offset` (July → `UTC+02:00`); `test_iso_week_year_boundary` (`date(2027, 1, 1)` → `"2026-W53"`); `test_now_line_ignores_locale` (monkeypatch `locale.setlocale(LC_TIME, …)` is fragile, so instead assert the weekday comes from the module tuple: `now_line` on all 7 days of a week gives `Mon…Sun`); `test_max_chars_below_500_rejected` (`"499"` → ConfigError naming the variable), `"500"` accepted.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_timerange.py tests/test_config.py` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** `feat(time): now line and calendar summary`.

### Task 3: Server: stamp, `now` tool, `period` params, instructions

**Files:**
- Modify: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py`, `tests/test_tools_search.py`, `tests/test_tools_export.py`, `tests/test_server.py`

**Interfaces:**
- Consumes: `KEYWORDS`, `now_line`, `now_details`, `iso_week`, `parse_period(..., period=)` (Tasks 1–2).
- Produces:
  ```python
  # tools.py
  def now(settings: Settings, moment: datetime) -> str              # == now_details(moment, settings.timezone)
  # get_messages / search_messages / export_chat gain `period: str | None = None`, passed to parse_period
  # server.py
  def build_server(source, settings, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> MCPServer
  ```
  In `build_server`: `tz = settings.timezone`; `budget = replace(settings, max_chars=settings.max_chars - len(now_line(now(), tz)) - 1)`; `MCPServer("tg-bridge", instructions=_instructions(now(), tz), lifespan=lifespan)`. A local helper used by every tool:
  ```python
  async def stamped(call: Callable[[datetime], Awaitable[str]]) -> str:
      moment = now()
      line = now_line(moment, tz)
      try:
          return f"{line}\n{await call(moment)}"
      except ToolError as e:
          raise ToolError(f"{line}\n{e}") from e
  ```
  Each tool: `return await stamped(lambda m: tools.get_messages(source, budget, ..., period=period, now=lambda: m))`; tools without a `now` param ignore `m`. Don't wrap tool functions with a decorator: the SDK builds the schema from the signature.
- **Docstrings:** `now`: `Current date, weekday, time, time zone and ISO week, plus this/last week, month and year as dates. Call it first when the question depends on today ("today", "this week", "last month", "next year", how long ago): never guess the date. Every other tool's result also starts with a "Now:" line.` In `get_messages` replace the period sentence with: `Period: `period` = today, yesterday, this_week, last_week, this_month, last_month, this_year or last_year; OR `days` (1 = today, 2 = since yesterday 00:00, …); OR `since`/`until` (YYYY-MM-DD, "YYYY-MM-DD HH:MM" or a keyword; `until` dates include that whole day). Prefer `period` or `days` over computing dates yourself. No period = the latest messages.` `search_messages`/`export_chat`: `Period: `period` OR `days` OR `since`/`until`, as in get_messages.`

- [ ] **Step 1: Failing tests**
  - tools layer: `test_get_messages_period_keyword` (FakeSource records the `MessageQuery`; `period="last_week"` with a fixed `now` → expected start/end), same for `search_messages` (`SearchQuery`) and `export_chat` (file header period), `test_period_and_days_is_tool_error`, `test_now_tool_body`.
  - server: `MINIMAL_ARGS: dict[str, dict]` with one entry per tool (`{"whoami": {}, "now": {}, "list_chats": {}, "get_chat_info": {"chat": "family"}, "get_messages": {"chat": "family"}, "search_messages": {"query": "hi"}, "export_chat": {"chat": "family"}, "download_media": {"chat": "family", "message_id": 7}}`) on a FakeSource with one chat and one message, settings with `export_dir`/`download_dir` under `tmp_path` and a fixed `now`:
    - `test_minimal_args_cover_every_tool` (keys == registered names, so a new tool must join the table),
    - `test_every_tool_result_starts_with_now_line` (parametrised over `MINIMAL_ARGS`; success or error, `content[0].text` starts with `"Now: 2026-10-28 Wed 14:03 (Europe/Warsaw)\n"`),
    - `test_tool_error_is_stamped` (`get_chat_info(chat="nope")` → `is_error` and stamped),
    - `test_stamped_output_within_budget` (`TG_BRIDGE_MAX_CHARS=500`, 300 long messages → `len(text) <= 500`),
    - `test_instructions_contain_today` (`server` instructions contain `2026-10-28`, `Wednesday`, `now()`; read them however the v2 SDK exposes them, e.g. `server.instructions`),
    - `test_now_tool_registered_read_only`, `period` in the property sets of `get_messages`, `search_messages`, `export_chat`, and `"this_week" in description` for those three.
  - Update `test_lists_tools` (8 tools incl. `now`; `now` read-only) and the exact-text assertions (`whoami`, `list_chats`) to expect the `Now:` line first, using the fixed `now`.
- [ ] **Step 2: Run** `uv run pytest -q tests/test_server.py tests/test_tools.py tests/test_tools_search.py tests/test_tools_export.py` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` → green; `uv run tg-bridge` still starts (the process test lists 8 tools).
- [ ] **Step 5: Commit** `feat(time): now() tool, Now line in every result, period keywords in tools`.

### Task 4: Smoke and docs

- [ ] Maintainer runs the brief's smoke checklist. Builder also checks, in LM Studio, whether the model sees the server `instructions` (ask "what's today's date?" **without** letting it call a tool, e.g. with tools disabled for that turn, or look at LM Studio's developer log of the prompt). Record the result as one line under roadmap "Log" (`09: LM Studio does/doesn't pass MCP instructions to the model`), plus a line for `last_year` and the `TG_BRIDGE_MAX_CHARS ≥ 500` floor.
- [ ] Roadmap: 09 → `[x]` after the smoke checklist passes.
