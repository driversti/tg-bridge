# 00 — Foundation

**Design refs:** §4, §6 (modules), §8 (security), §9 (testing) · **Depends on:** —

## Goal
A runnable, empty-but-real MCP server that LM Studio can launch, authenticated to Telegram read-only, with one tool proving the chain works.

## Scope
- `uv` project, package `tg_bridge`, entry point `tg-bridge` (stdio MCP server), `pytest` set up.
- `.gitignore`: `*.session`, `.env*`, `exports/`, `downloads/`, `.venv/`.
- `config`: env + `~/.config/tg-bridge/config.env` (vars in setup.md §4). Fail fast with a clear message if credentials are missing.
- `tg_bridge.login`: interactive login script that creates the session with `600` perms (dir `700`).
- `telegram/client.py`: `ReadOnlyTelegram` wrapper (connect, `get_me`). No write methods.
- `source`: `MessageSource` protocol + domain models (design §6) with only `whoami` wired for now.
- Tool `whoami()` → own name, @username, id (smoke test for the chain).
- Logging to stderr, no PII.
- Test: no module outside `tg_bridge/telegram/` imports `telethon`.
- Fill in `CLAUDE.md` → Commands, and confirm the LM Studio `mcp.json` block in setup.md.
- Verify and document in setup.md: does connecting change the account's "online" status? Avoid if cheap; acceptable if not (design §3).

## Done when
- `uv run pytest` is green.
- LM Studio lists the `tg-bridge` tools, and asking "who am I on Telegram?" returns the right account.

## Smoke checklist (manual, real account)
- [ ] Login script works and the session file is `600`.
- [ ] `whoami` from LM Studio returns the right account.
- [ ] Note whether contacts see you "online" while the server runs (acceptable if unavoidable).
