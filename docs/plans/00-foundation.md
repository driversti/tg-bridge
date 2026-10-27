# 00 — Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable stdio MCP server that LM Studio launches, authenticated to Telegram read-only, exposing one tool `whoami`.

**Architecture:** `server` (MCP bootstrap) → `tools` (thin, plain-text output) → `MessageSource` protocol → `LiveSource` → `ReadOnlyTelegram` (the only Telethon user). Config is loaded and validated at startup (fail fast); the Telegram connection is opened **lazily on the first tool call**, so listing tools never touches Telegram and the account isn't connected while idle.

**Tech Stack:** Python ≥3.12, `uv`, `telethon>=1.45,<2`, `mcp>=2.3,<3` (SDK **v2**: `MCPServer`, not `FastMCP`), `python-dotenv`, `pytest` + `pytest-asyncio`.

**Spec:** `docs/features/00-foundation.md` + `docs/design.md` §3 (anti-goals), §6, §8, §9 + `docs/setup.md` §2–4.

## Global Constraints

- **Read-only.** No sending, editing, reactions, joins, read receipts, typing, status updates. Never call `account.UpdateStatus`, `send_read_acknowledge`, or `client.start()` in the server.
- Only `tg_bridge/telegram/` may import `telethon`.
- stdout is reserved for MCP. Logs go to stderr only; never log message text, names, usernames or phones (ids, counts, durations are fine).
- Never print, log or commit `TG_API_ID`/`TG_API_HASH` values or `*.session`. Error messages name variables, never values.
- Session dir `~/.config/tg-bridge` is `700`; session file is `600`.
- Tests never touch a real account or the real `~/.config/tg-bridge/` (subprocess tests set `HOME` to a tmp dir).
- Config vars and defaults exactly as setup.md §4:
  `TG_API_ID`, `TG_API_HASH` (required) · `TG_BRIDGE_SESSION=~/.config/tg-bridge/tg-bridge.session` · `TG_BRIDGE_TZ=Europe/Warsaw` · `TG_BRIDGE_MAX_CHARS=12000` · `TG_BRIDGE_EXPORT_DIR=~/tg-bridge-data/exports` · `TG_BRIDGE_DOWNLOAD_DIR=~/tg-bridge-data/downloads`. Env vars override the config file.
- Tool output is plain text (design §11).
- Ask the maintainer before every commit (CLAUDE.md). The commit steps below are proposals.

## Decisions taken here

- **MCP SDK v2.** `from mcp.server.mcpserver import MCPServer`; anticipated tool failures raise `mcp.server.mcpserver.exceptions.ToolError` (model sees the message, `is_error=True`); any other exception reaches the model only as `Error executing tool <name>`.
- **Logging is configured before `MCPServer(...)` is constructed.** MCPServer calls `logging.basicConfig`, which is a no-op once the root logger has a handler. Ours goes to `sys.stderr`.
- **Only `Person` is defined now.** `Chat`/`Message`/`MediaInfo`/`Page` arrive with features 01/02, where their shape gets decided (design §6 says "builder refines"). The protocol has only `whoami()` and `close()`.
- **Lazy connect** in `LiveSource` (guarded by `asyncio.Lock`) + `receive_updates=False` on the Telethon client. Minimises time connected; the "online" question is settled by the manual smoke test.
- **Login logic lives in `tg_bridge/telegram/login.py`**; `tg_bridge/login.py` is the thin `python -m` entry point, so the import rule holds.
- Flat layout (`tg_bridge/` at repo root), build backend `hatchling`, `.python-version` = `3.12`.

## Review Focus

1. **Missing/garbage credentials** (`TG_API_ID=abc`, empty hash, unreadable config file) → one clear stderr line naming the variable and file, exit code 2, **nothing on stdout**, no value echoed. → Task 2 tests + Task 6 subprocess test.
2. **Session file absent / not authorized** (login hasn't been run yet) → `whoami` returns a ToolError telling the user to run `uv run python -m tg_bridge.login`; the server must **never** fall into Telethon's interactive prompt (that would read MCP's stdin). → Task 4 test (`start` never called) + Task 5 test.
3. **Concurrent first tool calls** → exactly one `connect()`. → Task 5 test.
4. **Config file readable by group/others** → warning on stderr (no values), server still starts. → Task 2 test.
5. **FloodWait from Telegram** → ToolError saying "rate limited, retry in N s", not a crash. → Task 4 + Task 6 tests.

---

### Task 1: Project skeleton + import boundary

**Files:**
- Create: `pyproject.toml`, `.python-version`, `tg_bridge/__init__.py`, `tg_bridge/telegram/__init__.py`, `tests/__init__.py`, `tests/test_import_boundary.py`
- `.gitignore` already covers the brief's list (`*.session`, `.env*`, `exports/`, `downloads/`, `.venv/`); no change.

**Interfaces:**
- Produces: package `tg_bridge`; console script `tg-bridge = "tg_bridge.server:main"` (module created in Task 6 — the entry point may dangle until then).

- [ ] **Step 1: `pyproject.toml`** — `requires-python = ">=3.12"`; deps `telethon>=1.45,<2`, `mcp>=2.3,<3`, `python-dotenv>=1.0`; `[dependency-groups] dev = ["pytest>=8", "pytest-asyncio>=0.24"]`; `[tool.pytest.ini_options] asyncio_mode = "auto"`, `testpaths = ["tests"]`; hatchling with `packages = ["tg_bridge"]`. Run `uv sync`. Commit `uv.lock`.
- [ ] **Step 2: Write `tests/test_import_boundary.py::test_telethon_only_imported_in_telegram_package`** — walk `tg_bridge/**/*.py`, parse with `ast`, collect `Import`/`ImportFrom` whose top-level module is `telethon`; assert the offending-file list is empty for files outside `tg_bridge/telegram/`. Add `test_boundary_detects_violation(tmp_path)` that runs the same scanner function on a tmp file containing `from telethon import TelegramClient` and asserts it is reported (so the guard can't silently pass on an empty walk). Scanner: `find_telethon_imports(root: Path) -> list[Path]` defined in the test module.
- [ ] **Step 3: Run** `uv run pytest -q` → 2 passed.
- [ ] **Step 4: Commit** `chore: uv project skeleton and telethon import boundary test`.

### Task 2: Errors, config, logging

**Files:**
- Create: `tg_bridge/errors.py`, `tg_bridge/config.py`, `tg_bridge/logging_setup.py`
- Test: `tests/test_config.py`, `tests/test_logging_setup.py`

**Interfaces:**
- Produces:
  - `errors.py`: `class TgBridgeError(Exception)`; `ConfigError(TgBridgeError)`; `NotLoggedInError(TgBridgeError)`; `RateLimitedError(TgBridgeError)` with attr `seconds: int`.
  - `config.py`: `DEFAULT_CONFIG_FILE: Path` computed at call time as `Path.home()/".config/tg-bridge/config.env"` (use a function `default_config_file() -> Path`); frozen dataclass `Settings(api_id: int, api_hash: str, session_path: Path, timezone: ZoneInfo, max_chars: int, export_dir: Path, download_dir: Path)` with `__repr__` that masks `api_hash`; `load_settings(env: Mapping[str, str] | None = None, config_file: Path | None = None) -> Settings` (defaults: `os.environ`, `default_config_file()`).
  - `logging_setup.py`: `configure_logging(level: int = logging.INFO) -> None` — root handler `StreamHandler(sys.stderr)`, format `%(asctime)s %(levelname)s %(name)s: %(message)s`; `telethon` logger at `WARNING`.

- [ ] **Step 1: Write failing tests in `tests/test_config.py`** (all use `tmp_path` files and explicit `env` dicts):
  - `test_defaults_applied` — env `{TG_API_ID: "123", TG_API_HASH: "h"}`, no file → `session_path == Path.home()/".config/tg-bridge/tg-bridge.session"`, `timezone.key == "Europe/Warsaw"`, `max_chars == 12000`, export/download dirs as in Global Constraints, paths `expanduser`-ed.
  - `test_reads_config_file` and `test_env_overrides_file`.
  - `test_missing_api_id_raises` — `ConfigError` whose message contains `"TG_API_ID"` and the config file path.
  - `test_non_integer_api_id_raises` — `TG_API_ID="abc"` → `ConfigError`; message does **not** contain `"abc"`.
  - `test_empty_api_hash_raises`.
  - `test_invalid_timezone_raises`, `test_non_positive_max_chars_raises`.
  - `test_repr_masks_hash` — `"h-secret" not in repr(settings)`.
  - `test_group_readable_config_file_warns` — file mode `0o644` → `caplog` has a WARNING mentioning `chmod 600`; settings still load. Mode `0o600` → no warning.
- [ ] **Step 2: Run** `uv run pytest tests/test_config.py -q` → fails (module missing).
- [ ] **Step 3: Implement** `errors.py`, `config.py` (`dotenv_values(config_file)` if the file exists, then overlay `env`), `logging_setup.py`.
- [ ] **Step 4: Write `tests/test_logging_setup.py::test_logs_go_to_stderr`** — call `configure_logging()`, log a line, `capsys` shows it in `err` and `out == ""`. Reset root handlers in a fixture.
- [ ] **Step 5: Run** `uv run pytest -q` → all pass.
- [ ] **Step 6: Commit** `feat: settings loading with fail-fast validation and stderr logging`.

### Task 3: Domain model + MessageSource protocol + FakeSource

**Files:**
- Create: `tg_bridge/source/__init__.py`, `tg_bridge/source/models.py`, `tg_bridge/source/protocol.py`, `tests/fakes.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `models.py`: frozen dataclass `Person(id: int, first_name: str | None, last_name: str | None, username: str | None, phone: str | None, is_deleted: bool, is_bot: bool)` with property `display_name -> str` (first + last joined by a space, stripped; `"Deleted Account"` if `is_deleted`; `"(no name)"` if both empty).
  - `protocol.py`: `class MessageSource(Protocol)`: `async def whoami(self) -> Person`; `async def close(self) -> None`.
  - `source/__init__.py` re-exports `Person`, `MessageSource`.
  - `tests/fakes.py`: `FakeSource(me: Person | None = None, error: Exception | None = None)` — `whoami` raises `error` if set, else returns `me`; `close()` sets `self.closed = True`.

- [ ] **Step 1: Write failing tests** `test_display_name_full`, `test_display_name_first_only`, `test_display_name_deleted`, `test_display_name_empty`, `test_fake_source_satisfies_protocol` (assign `FakeSource()` to a `MessageSource`-annotated variable and call both methods).
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** `feat: Person model and MessageSource protocol`.

### Task 4: Read-only Telegram wrapper

**Files:**
- Create: `tg_bridge/telegram/client.py`, `tg_bridge/telegram/mapping.py`
- Test: `tests/telegram/__init__.py`, `tests/telegram/test_mapping.py`, `tests/telegram/test_client.py`

**Interfaces:**
- Consumes: `Settings`, `Person`, `NotLoggedInError`, `RateLimitedError`.
- Produces:
  - `mapping.py`: `person_from_user(user) -> Person` — reads `id, first_name, last_name, username, phone, deleted, bot` (Telethon `User` attribute names); missing/None booleans → `False`.
  - `client.py`: `class ReadOnlyTelegram`:
    - `__init__(self, settings: Settings, client_factory: Callable[..., Any] = TelegramClient)` — builds the client as `client_factory(str(settings.session_path), settings.api_id, settings.api_hash, receive_updates=False)`.
    - `async connect(self) -> None` — `await client.connect()`; if `not await client.is_user_authorized()`: disconnect, raise `NotLoggedInError("Not logged in to Telegram. Run: uv run python -m tg_bridge.login")`.
    - `async get_me(self) -> Person`.
    - `async disconnect(self) -> None`.
    - Every Telegram call goes through a private helper that converts `telethon.errors.FloodWaitError` → `RateLimitedError(seconds=e.seconds)`.
    - **No other public methods.** Never `client.start()`.

- [ ] **Step 1: Write failing `test_mapping.py`** — `SimpleNamespace` users: full user → all fields; user with `deleted=True`, names `None` → `is_deleted is True`; `bot=None` → `False`.
- [ ] **Step 2: Write failing `test_client.py`** with a `FakeTelethonClient` (records constructor args/kwargs and calls; configurable `authorized`, `me`, `raise_on_get_me`):
  - `test_builds_client_without_updates` — `kwargs["receive_updates"] is False`, session path passed as `str`.
  - `test_connect_unauthorized_raises_not_logged_in` — and `"start"` not in recorded calls, `"disconnect"` is.
  - `test_get_me_maps_person`.
  - `test_flood_wait_becomes_rate_limited` — fake raises `FloodWaitError(request=None, capture=42)` → `RateLimitedError` with `seconds == 42`. (Check the Telethon 1.45 constructor; if it differs, build it the way Telethon does.)
  - `test_public_surface_is_read_only` — `{n for n in dir(ReadOnlyTelegram) if not n.startswith("_")} == {"connect", "get_me", "disconnect"}`. Adding a method must be a deliberate edit of this test.
- [ ] **Step 3: Run** → fail. **Step 4: Implement.** **Step 5: Run** `uv run pytest -q` → pass (boundary test still green).
- [ ] **Step 6: Commit** `feat: ReadOnlyTelegram wrapper with get_me`.

### Task 5: LiveSource

**Files:**
- Create: `tg_bridge/source/live.py`
- Test: `tests/test_live_source.py`

**Interfaces:**
- Consumes: `ReadOnlyTelegram` (Task 4), `MessageSource`.
- Produces: `class LiveSource(telegram: ReadOnlyTelegram)` implementing `MessageSource`. `whoami()` connects once on first use (`asyncio.Lock`, `_connected` flag), then `get_me()`. A failed connect leaves `_connected` False so the next call retries. `close()` disconnects only if connected. Logs `"telegram connected"` / duration at INFO — no names.

- [ ] **Step 1: Write failing tests** using a stub with the `ReadOnlyTelegram` surface (counts calls):
  - `test_whoami_connects_lazily` — no `connect` after construction; one after `whoami()`.
  - `test_concurrent_first_calls_connect_once` — `asyncio.gather(src.whoami(), src.whoami(), src.whoami())` with a stub `connect` that `await asyncio.sleep(0)` → `connect_calls == 1`.
  - `test_failed_connect_retries_next_call` — first `connect` raises `NotLoggedInError`, second succeeds.
  - `test_close_without_connect_is_noop`, `test_close_disconnects`.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → pass.
- [ ] **Step 5: Commit** `feat: LiveSource with lazy connect`.

### Task 6: `whoami` tool + MCP server

**Files:**
- Create: `tg_bridge/tools.py`, `tg_bridge/server.py`
- Test: `tests/test_tools.py`, `tests/test_server.py`, `tests/test_server_process.py`

**Interfaces:**
- Consumes: `MessageSource`, `Person`, `TgBridgeError` family, `load_settings`, `configure_logging`, `LiveSource`, `ReadOnlyTelegram`.
- Produces:
  - `tools.py`: `async def whoami(source: MessageSource) -> str` → `"{display_name} (@{username}), id {id}"`, or `"{display_name} (no username), id {id}"`. Catches `TgBridgeError` and re-raises `ToolError(str(e))`; `RateLimitedError` message: `"Telegram rate limit: retry in {seconds} s"`.
  - `server.py`: `build_server(source: MessageSource) -> MCPServer` — name `"tg-bridge"`, registers tool `whoami` (no arguments; docstring = model-facing description: "Return the Telegram account this bridge is logged in as: name, @username and numeric id."), lifespan that `await source.close()` on shutdown. `main() -> None` — `configure_logging()`; `load_settings()`; on `ConfigError`: print `f"tg-bridge: {e}"` to stderr, `sys.exit(2)`; build `LiveSource(ReadOnlyTelegram(settings))`; `build_server(...).run()` (stdio).

- [ ] **Step 1: Write failing `test_tools.py`** with `FakeSource`: `test_whoami_with_username` → exact string `"Anna K (@anna_k), id 42"`; `test_whoami_without_username`; `test_not_logged_in_becomes_tool_error` (message contains `tg_bridge.login`); `test_rate_limited_becomes_tool_error` (contains `"retry in 42 s"`).
- [ ] **Step 2: Write failing `test_server.py`** (in-process, `FakeSource`): `test_lists_whoami_tool` — `await build_server(src).list_tools()` names == `["whoami"]`; `test_call_whoami` — `await server.call_tool("whoami", {})` result text equals the tools-layer string.
- [ ] **Step 3: Write failing `test_server_process.py`** (subprocess `uv run tg-bridge`, `env` = minimal copy of `os.environ` with `HOME=tmp_path` and no `TG_*` vars):
  - `test_missing_credentials_exits_cleanly` — exit code 2, `stdout == ""`, `"TG_API_ID"` in stderr, timeout 30 s.
  - `test_stdio_lists_tools_without_telegram` — env adds `TG_API_ID=1`, `TG_API_HASH=x`, `TG_BRIDGE_SESSION=<tmp>/none.session`; connect with `mcp.client.stdio.stdio_client(StdioServerParameters(...))` + `mcp.client.ClientSession`, `initialize()`, `list_tools()` → contains `whoami`. Proves stdout carries only MCP and that startup doesn't touch Telegram. Follow the v2 client docs for exact session usage.
- [ ] **Step 4: Run** → fail. **Step 5: Implement.** **Step 6: Run** `uv run pytest -q` → all pass.
- [ ] **Step 7: Commit** `feat: stdio MCP server with whoami tool`.

### Task 7: Interactive login

**Files:**
- Create: `tg_bridge/telegram/login.py`, `tg_bridge/login.py`
- Test: `tests/test_login.py`

**Interfaces:**
- Consumes: `Settings`, `load_settings`, `configure_logging`, `person_from_user`.
- Produces:
  - `tg_bridge/login.py`: `prepare_session_dir(session_path: Path) -> None` (create parent with `0o700`, `chmod 0o700` if it exists looser); `secure_session_file(session_path: Path) -> None` (`chmod 0o600` the file and `*.session-journal` if present); `main() -> None` — `os.umask(0o077)` first, load settings (ConfigError → stderr, exit 2), `prepare_session_dir`, `asyncio.run(interactive_login(settings))`, `secure_session_file`, print `"Logged in as {display_name} (id {id}). Session: {path}"` to the terminal, and where to review the session (Telegram → Settings → Devices). `if __name__ == "__main__": main()`.
  - `tg_bridge/telegram/login.py`: `async def interactive_login(settings: Settings) -> Person` — `TelegramClient(..., receive_updates=False)`, `await client.start()` (Telethon prompts for phone, code, 2FA — allowed **only here**), `get_me`, disconnect.

- [ ] **Step 1: Write failing tests** (`tmp_path`): `test_prepare_creates_dir_700`, `test_prepare_tightens_existing_dir`, `test_secure_session_file_sets_600` (file created `0o644` first), `test_secure_handles_missing_journal`. `interactive_login` is not unit-tested (it is Telethon's prompt flow); the manual smoke test covers it.
- [ ] **Step 2: Run** → fail. **Step 3: Implement.** **Step 4: Run** `uv run pytest -q` → pass, boundary test green.
- [ ] **Step 5: Commit** `feat: interactive login script with 600 session file`.

### Task 8: Docs

**Files:**
- Modify: `CLAUDE.md` (Commands), `docs/setup.md` (§3 block, new §6), `docs/features/00-foundation.md` (smoke checklist if needed), `docs/roadmap.md`

- [ ] **Step 1: `CLAUDE.md` → Commands:** `uv sync` · `uv run pytest -q` · `uv run tg-bridge` (stdio server; LM Studio runs it) · `uv run python -m tg_bridge.login`.
- [ ] **Step 2: setup.md §3:** `uv` is at `/opt/homebrew/bin/uv` on this Mac (not `~/.local/bin/uv`); confirm with `which uv` and fix the `command` path. Remove the "(Feature 00 confirms …)" note.
- [ ] **Step 3: setup.md new "§6 Online status"** with what the code does (lazy connect, `receive_updates=False`, never `UpdateStatus`) and a `TBD after smoke test` line for the smoke-test observation.
- [ ] **Step 4: Run** `uv run pytest -q` → green. Commit `docs: commands, LM Studio config, online-status notes`.
- [ ] Roadmap stays `[~]` until the smoke checklist is run.
