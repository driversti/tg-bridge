# tg-bridge

Read-only MCP bridge between a local LLM (LM Studio) and the user's personal Telegram account.
Python 3.12+, Telethon, official `mcp` SDK, `uv`, `pytest`.

**Start here:** `docs/README.md` (index) → `docs/roadmap.md` (status + workflow) → your feature's brief or plan.
Read `docs/design.md` only in the sections the brief points to.

## Hard rules
- **Read-only.** No account side effects: no sending, editing, reactions, joins, read receipts, typing or status updates.
- Only `tg_bridge/telegram/` may import Telethon.
- stdout is reserved for MCP. Log to stderr, and never log message text, names or phones.
- Never print, log or commit `TG_API_ID`/`TG_API_HASH`, `*.session`, or exports.
- Tests never touch a real account. TDD, keep the suite green.
- Ask the maintainer before committing.

## Commands
- `uv sync`: install deps
- `uv run pytest -q`: test suite
- `uv run tg-bridge`: stdio MCP server (LM Studio launches it; it waits for MCP on stdin)
- `uv run python -m tg_bridge.login [--method qr|code]`: interactive login (humans only: needs the real account)
