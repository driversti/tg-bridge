# 11 Networked MCP — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** a long-running `tg-bridge-net serve` for the homelab LXC. It serves MCP over streamable HTTP behind a bearer token. The admin page runs inside it on loopback. The login link goes out via the Homelab bot.

**Architecture:** one asyncio process owns one `LiveSource`, i.e. one Telegram client:
- **MCP:** `ScopedSource(live)` behind `MCPServer.streamable_http_app()`, wrapped in a bearer-auth ASGI middleware and served by `uvicorn.Server` on the LAN address.
- **Admin page:** the existing stdlib page runs in a thread on `127.0.0.1` and reads the **unscoped** `live`. It reaches Telegram through `asyncio.run_coroutine_threadsafe` onto the main loop.
- **Secrets:** both tokens live in `600` files and are re-read by mtime, so a rotation done by another command applies to the running process at once.

**Tech Stack:** Python **3.14**, `mcp` 2.3 (`MCPServer`, `TransportSecuritySettings`), uvicorn + starlette (already transitive deps of `mcp`), Telethon, stdlib `urllib` for the Bot API, pytest.

**Spec:** `docs/features/11-network-mcp.md` (read it first). Also read `CLAUDE.md`, `docs/design.md` §8, and `docs/features/17-cloud-scope.md` (page section).

## Global Constraints

- Read-only towards the account: no Telethon write calls. The Bot API call is a bot speaking and does not go through Telethon. Only `tg_bridge/telegram/` imports Telethon.
- Logs go to stderr only. Never log the MCP token, the admin token or link, the bot token, message text, names or phones. On failures log exception **class names** only, because `urllib` errors carry the URL, and the URL contains the bot token.
- `scope=public` stays the default. Network mode refuses `scope=all` unless `TG_BRIDGE_NET_ALLOW_ALL=1`.
- MCP bind: an explicit address is required, and `0.0.0.0`, `::` and an empty value are refused.
- The admin page binds `127.0.0.1` only. The MCP token never opens the page, and the admin cookie never opens MCP.
- Secret files go next to the session: dir `700`, files `600`.
- stdio mode (`tg-bridge`, LM Studio) and standalone `tg-bridge-admin` on the Mac behave exactly as today.
- Exit codes: `2` = config error, `3` = not logged in. Neither may restart under systemd (`RestartPreventExitStatus=2 3`).
- TDD, suite green after every task. Commit only after the maintainer approves (CLAUDE.md), so the "Commit" steps below mean "stage, show the diff summary, ask".

## Review Focus

1. **`Host` header behind `tailscale serve`:** the page must accept the tailnet name from `TG_BRIDGE_ADMIN_URL`. If Tailscale forwards a different `Host`, the page breaks in the smoke. Task 6 has a test for the configured host, and the smoke checks it live.
2. **A token file that was edited by hand** (trailing newline, spaces, empty file): whitespace is stripped. An empty file or one shorter than `MIN_TOKEN_LEN` counts as "no token", so MCP refuses everything and `serve` refuses to start. Tests are in Task 2.
3. **Bot token leaking through exceptions:** `HTTPError` and `URLError` text includes the request URL. A test in Task 5 asserts that captured logs never contain the token on HTTP 401 or a timeout.
4. **Client header variants:** `bearer` in lowercase, extra spaces, a second `Authorization` header, an empty token. The scheme is case-insensitive and only exactly one `Bearer <token>` passes. Tests are in Task 3.
5. **The page and MCP hitting Telegram at the same time:** an admin Refresh while a tool call is running must neither deadlock the loop nor run on a second client. A test in Task 6 runs a fetch through `run_coroutine_threadsafe` while the loop is serving another coroutine.

---

### Task 0: Move to Python 3.14

**Files:** Modify `.python-version`, `pyproject.toml` (`requires-python = ">=3.14"`), `uv.lock`, `CLAUDE.md` ("Python 3.12+" → "Python 3.14+"), `docs/setup.md` (any "3.12" mention).

- [ ] **Step 1:** set `.python-version` to `3.14` and `requires-python = ">=3.14"`, then run `uv lock`.
- [ ] **Step 2:** `uv sync && uv run pytest -q` → expect all tests pass (1073 today). Fix only real 3.14 incompatibilities. If any appear, list them in the commit message.
- [ ] **Step 3:** add a note in `setup.md`: the first `uv run` after this change rebuilds `.venv` on 3.14, so the first LM Studio call is slower.
- [ ] **Step 4: Commit** `chore: move to Python 3.14`

### Task 1: Settings and device name

**Files:** Modify `tg_bridge/config.py`, `tg_bridge/telegram/identity.py`, `tg_bridge/telegram/client.py:91`, `tg_bridge/telegram/login.py:165`; Test `tests/test_config.py`, `tests/telegram/` (identity test next to the existing client tests).

**Interfaces — Produces:** new `Settings` fields, all with safe defaults so stdio is unaffected:

| Env | Field | Default | Validation in `load_settings` |
|---|---|---|---|
| `TG_BRIDGE_DEVICE_NAME` | `device_name: str` | `tg-bridge` | stripped, 1–64 chars |
| `TG_BRIDGE_MCP_HOST` | `mcp_host: str \| None` | unset | none here (checked by `check_network_settings`) |
| `TG_BRIDGE_MCP_PORT` | `mcp_port: int` | `8766` | 1–65535 |
| `TG_BRIDGE_ADMIN_URL` | `admin_url: str \| None` | unset | if set: `http(s)://host[:port]`, no path, no query |
| `TG_BRIDGE_NOTIFY_BOT_TOKEN` | `notify_bot_token: str \| None` (`repr=False`) | unset | if set: `<digits>:<rest>` |
| `TG_BRIDGE_NOTIFY_CHAT_ID` | `notify_chat_id: int \| None` | unset | if set: integer (may be negative) |
| `TG_BRIDGE_NET_ALLOW_ALL` | `net_allow_all: bool` | `False` | `1`/`0` |

- `check_network_settings(settings: Settings) -> None` in `config.py` raises `ConfigError` naming the variable when:
  - `mcp_host` is unset or a wildcard (`0.0.0.0`, `::`, `*`, empty);
  - `scope == "all"` and not `net_allow_all`;
  - `mcp_port == admin_port`.
- `notify_bot_id(settings) -> int | None`: the token's numeric prefix.
- `client_identity(device_name: str) -> dict[str, str]`: `device_model=device_name`, other keys as today.

- [ ] **Step 1: Write failing tests:**
  - `test_network_defaults`: `load_settings(CREDS)` gives `device_name == "tg-bridge"`, `mcp_host is None`, `mcp_port == 8766`, `notify_bot_token is None`.
  - `test_bot_token_not_in_repr`.
  - `test_bad_bot_token_names_variable_not_value` (`"abc"` → `ConfigError` with `TG_BRIDGE_NOTIFY_BOT_TOKEN` in the message and `"abc"` not in it).
  - `test_admin_url_with_path_refused`.
  - `test_check_network_settings_refuses` parametrized over `None`, `"0.0.0.0"`, `"::"`, `"*"`, the same port as admin, and `scope=all` without allow.
  - `test_check_network_settings_accepts_lan_ip` (`192.168.10.22`).
  - `test_identity_uses_device_name`: `client_identity("tg-bridge (homelab)")["device_model"] == "tg-bridge (homelab)"`.
- [ ] **Step 2:** `uv run pytest tests/test_config.py -q` → FAIL.
- [ ] **Step 3:** implement. `ReadOnlyTelegram.connect` and `interactive_login` pass `settings.device_name`.
- [ ] **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5:** in `docs/features/11-network-mcp.md` §Session name, record whether Telegram updates the Devices entry on reconnect. Telethon sends `initConnection` with `device_model` on every connect. If unsure, leave it for the smoke.
- [ ] **Step 6: Commit** `feat(config): network settings and device name`

### Task 2: Token files (shared by the page and MCP)

**Files:** Create `tg_bridge/token_file.py`; Modify `tg_bridge/admin.py` (drop `token_path_for`, `_write_token`, `write_new_token`, `load_or_create_token`, `BadTokenFile`, and import from the new module); Test `tests/test_token_file.py`, `tests/test_admin.py` (only update imports).

**Interfaces — Produces:**
```python
MIN_TOKEN_LEN = 32
class BadTokenFile(TgBridgeError): ...
class TokenFile:
    def __init__(self, path: Path): ...
    def read(self) -> str | None          # stripped; None if missing, empty or < MIN_TOKEN_LEN; re-reads only when mtime/size changed
    def rotate(self) -> str               # secrets.token_urlsafe(32), atomic write, file 600, parent dir 700
    def load_or_create(self) -> str       # read() or rotate(); BadTokenFile if the file exists but is unreadable or too short
def admin_token_file(session_path: Path) -> TokenFile   # "<stem>.admin-token" (today's name, unchanged)
def mcp_token_file(session_path: Path) -> TokenFile     # "<stem>.mcp-token"
```

- [ ] **Step 1: Write failing tests:**
  - `test_rotate_writes_600_and_dir_700`.
  - `test_read_strips_whitespace` (file `"<token>\n  "` → token).
  - `test_empty_or_short_file_reads_none`.
  - `test_read_sees_rotation_by_another_instance`: two `TokenFile` on one path; B rotates; A's `read()` returns the new value.
  - `test_read_is_cached_until_mtime_changes`: monkeypatch `Path.read_text` call counter; two reads → one call.
  - `test_admin_token_file_name_unchanged` (`.admin-token`, so the Mac's existing link keeps working).
- [ ] **Step 2:** run → FAIL. **Step 3:** implement and move the admin helpers. **Step 4:** `uv run pytest -q` → PASS.
- [ ] **Step 5: Commit** `refactor: token files shared by admin and MCP`

### Task 3: Bearer auth + HTTP app + Hermes compatibility check

**Files:** Create `tg_bridge/http_app.py`; Test `tests/test_http_app.py`; Create `scripts/hermes_compat.py` (a throwaway check, kept for re-runs).

**Interfaces:**
- Consumes: `TokenFile.read()` (Task 2), `Settings.mcp_host/mcp_port` (Task 1), `build_server(source, settings) -> MCPServer` (existing, `server.py`).
- Produces:
```python
class BearerAuth:                                   # pure ASGI middleware
    def __init__(self, app, token: Callable[[], str | None]): ...
def build_http_app(server: MCPServer, settings: Settings, token: Callable[[], str | None]) -> ASGIApp
```
- `BearerAuth` passes `lifespan` scopes through. For an `http` scope it requires exactly one `Authorization` header matching `^[Bb][Ee][Aa][Rr][Ee][Rr] +(\S+) *$`, compared with `hmac.compare_digest` against `token()`.
- On failure, and also whenever `token()` is `None`, it returns **401** with `WWW-Authenticate: Bearer` and body `{"error":"unauthorized"}`. Log a counter-style line without the header value (`"mcp auth refused"`).
- `build_http_app` returns `BearerAuth(server.streamable_http_app(streamable_http_path="/mcp", transport_security=TransportSecuritySettings(allowed_hosts=[f"{host}:{port}", host]), host=host), token)`. Keep the SDK defaults for sessions (stateful, SSE responses).

- [ ] **Step 1: Write failing tests.** Run the app with `uvicorn.Server` on `127.0.0.1:0` in a thread, wrapped in a small fixture. Use a `FakeSource` server from `tests/fakes.py` and the SDK's streamable-HTTP client from `mcp.client` (find the exact module in the installed SDK).
  - `test_no_header_401`, `test_wrong_token_401`, `test_two_auth_headers_401`, `test_empty_bearer_401`.
  - `test_lowercase_scheme_ok`: `bearer <tok>` + `tools/list` works.
  - `test_token_none_refuses_everything`: `token=lambda: None`, correct header → 401.
  - `test_bad_host_refused`: correct token, `Host: evil.example` → 421 or 403 (whatever the SDK returns; assert `>= 400` and not 200).
  - `test_tool_call_end_to_end`: the SDK client with a `headers={"Authorization": "Bearer …"}` → `list_tools` contains `list_chats`; `call_tool("whoami")` returns the fake's name.
  - `test_rotation_applies_without_restart`: `token` backed by a `TokenFile`; rotate via a second instance; the old token → 401, the new one → 200.
  - `test_auth_failure_logs_no_token` (caplog: the wrong token string is not in any record).
- [ ] **Step 2:** run → FAIL. **Step 3:** implement. **Step 4:** `uv run pytest tests/test_http_app.py -q` → PASS.
- [ ] **Step 5: Hermes compatibility.** Hermes v0.23.0 ships `mcp` **2.0.0**. Write `scripts/hermes_compat.py URL TOKEN`: it connects with the SDK's streamable-HTTP client and a bearer header, lists tools, calls `now`, prints `OK <n tools>`. Start `build_http_app` over a `FakeSource` locally (a `--fake` flag in the script that serves on `127.0.0.1:8799`). Run the client side as `uv run --isolated --no-project --python 3.11 --with 'mcp==2.0.0' python scripts/hermes_compat.py http://127.0.0.1:8799/mcp <token>`. Expected: `OK`.
  If it fails, switch to `stateless_http=True, json_response=True`, re-run, and record which mode works in the brief's Log section. Do **not** proceed to Task 7 until this prints `OK`.
- [ ] **Step 6: Commit** `feat(net): bearer-auth streamable HTTP app`

### Task 4: Bot-chat guard

**Files:** Modify `tg_bridge/scope.py`, `tg_bridge/source/scoped.py`, `tg_bridge/admin.py` (`switchable()`, row rendering), `tg_bridge/server.py:build_source`; Test `tests/test_scope.py`, `tests/test_scoped_source.py`, `tests/test_admin.py`.

**Interfaces — Produces:**
- `chat_visible(chat, allowlist, hidden: frozenset[int] = frozenset()) -> bool`: `hidden` wins over everything.
- `ScopedSource(inner, allowlist, hidden: frozenset[int] = frozenset())`.
- `build_source(settings)` passes `hidden=frozenset({notify_bot_id(settings)})` when that is not `None`, and logs a warning `"no notify bot configured; bot chat guard inactive"` otherwise. The warning appears only in network mode: `build_source` gets a `warn_no_bot: bool = False` param.
- `AdminApp(..., hidden: frozenset[int] = frozenset())`: hidden chats render as a row with the label `locked (receives login links)` and no switch. `switchable()` excludes them, so a forged POST → 400.

- [ ] **Step 1: Write failing tests:**
  - `test_hidden_beats_allowlist` (bot chat id in the allowlist and in hidden → not visible).
  - `test_scoped_hides_bot_chat_from_list_and_resolution` (`list_dialogs` lacks it; `get_messages` on its id → `ChatNotFoundError`).
  - `test_global_search_drops_bot_chat_hits`.
  - `test_admin_bot_chat_locked_no_switch` (the page HTML has the label and no form with that `chat_id`).
  - `test_admin_post_toggle_bot_chat_400`.
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(scope): bot chat that receives login links is never visible`

### Task 5: Login-link notifier

**Files:** Create `tg_bridge/notify.py`; Test `tests/test_notify.py`.

**Interfaces — Produces:**
```python
def admin_link(settings: Settings, token: str) -> str   # f"{admin_url or f'http://127.0.0.1:{admin_port}'}/login?token={token}"
def send_login_link(settings: Settings, link: str, post: Callable[[str, bytes], int] = _post) -> bool
```
- `_post(url, body) -> int` does a `urllib.request` POST, JSON body, timeout 10 s, and returns the HTTP status.
- `send_login_link` returns `False` without a request when the bot token or chat id is missing. It POSTs to `https://api.telegram.org/bot<token>/sendMessage` with `{"chat_id", "text", "link_preview_options": {"is_disabled": true}}`. Link previews stay off so Telegram's servers never fetch a URL that contains the token.
- Text (exact): `tg-bridge started on {device_name}.\nAdmin page (Tailscale): {link}`.
- Any exception or non-200 → log `"login link not sent: %s"` with the class name or the status code, and return `False`.

- [ ] **Step 1: Write failing tests:**
  - `test_admin_link_uses_admin_url`.
  - `test_admin_link_defaults_to_loopback`.
  - `test_no_bot_settings_no_request` (fake `post` asserts it is not called).
  - `test_sends_expected_payload` (fake captures URL and JSON: the right chat id and text, previews disabled).
  - `test_http_error_logs_no_secret`: fake raises `urllib.error.HTTPError(url_with_token, 401, …)`. Returns `False`, and caplog contains neither the bot token nor the admin token.
  - `test_timeout_returns_false`.
- [ ] **Step 2–4:** FAIL → implement → PASS.
- [ ] **Step 5: Commit** `feat(notify): send the admin link via the Homelab bot`

### Task 6: Admin page inside a running loop

**Files:** Modify `tg_bridge/admin.py`; Test `tests/test_admin.py`, `tests/test_admin_inprocess.py` (new).

**Interfaces:**
- Consumes: `TokenFile` (Task 2), the `hidden` param (Task 4).
- Produces:
  - `async def snapshot_from(live: LiveSource) -> Snapshot`: today's body of `fetch_snapshot` without creating or closing anything. `fetch_snapshot` now calls it.
  - `async def download_photos_with(live: LiveSource, photo_dir: Path, chats: list[Chat]) -> None`: like `download_photos`, but on a connected shared source. Add `LiveSource.download_chat_photo(chat_id) -> bytes | None` as a passthrough after `_ensure_connected()`.
  - `AdminApp(..., token_source: Callable[[], str | None] | None = None)`: when given, `login_token`, `session_cookie` and `csrf` are derived from `token_source()` (via `derive_secrets`) **on each request**. A `None` token refuses every route, `/login` included. Without `token_source`, today's fixed values apply (the Mac path and existing tests).
  - `make_server(app, port, *, secure_cookie: bool = False, allowed_hosts: frozenset[str] | None = None) -> HTTPServer`. `secure_cookie` adds `; Secure` to `Set-Cookie`. `allowed_hosts`, when given, makes any other `Host` get 400.
  - `start_admin_in_thread(settings, live, loop, hidden) -> HTTPServer`. It builds `AdminApp` with fetch=`lambda: run_coroutine_threadsafe(snapshot_from(live), loop).result(timeout=120)`, and a `PhotoWorker` with download=`… download_photos_with … .result(timeout=120)`. It uses `token_source=admin_token_file(...).read` and `secure_cookie = admin_url starts with "https://"`. `allowed_hosts` = `{f"127.0.0.1:{port}", f"localhost:{port}"}` plus the `admin_url` netloc. It runs the initial `refresh()` inside the server thread before `serve_forever()`, so `serve` startup never waits for it, and returns the server so `serve` can `shutdown()` it.

- [ ] **Step 1: Write failing tests:**
  - `test_token_source_rotation_logs_out_old_cookie`: login with token A → cookie works. Rotate to B → the old cookie gets 403, `/login?token=B` works.
  - `test_token_source_none_refuses_login`.
  - `test_secure_cookie_flag` (`Set-Cookie` contains `Secure` when on, and does not when off).
  - `test_host_check`: `Host: tg-bridge.tail4be1a2.ts.net` (configured) → 200 after login; `Host: 192.168.10.22:8765` → 400; no `allowed_hosts` → as today.
  - `test_inprocess_fetch_while_loop_busy` (Review Focus 5): a real event loop in a thread runs a slow coroutine on a fake `LiveSource`; `start_admin_in_thread`'s fetch completes, both finish, and the fake recorded that both calls ran on the same instance.
  - `test_mac_admin_unchanged` (the existing suite covers it; no new test needed beyond green).
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(admin): run inside the network process with rotating token and host check`

### Task 7: `tg-bridge-net` command

**Files:** Create `tg_bridge/network.py`; Modify `pyproject.toml` (`tg-bridge-net = "tg_bridge.network:main"`); Test `tests/test_network.py`, `tests/test_network_process.py` (subprocess, like `tests/test_server_process.py`).

**Interfaces:**
- Consumes: everything above.
- Produces the CLI:
  - `tg-bridge-net serve`: run the service.
  - `tg-bridge-net token --new`: rotate the MCP token and print it once to stderr with a one-line hint about Hermes' `config.yaml`.
  - `tg-bridge-net token --show`: print the current MCP token.
  - `tg-bridge-net link [--new] [--send]`: print the admin link (rotate first with `--new`), and with `--send` also send it via the bot. Exit 1 if sending fails.

`serve` order (exact):
1. `configure_logging()`, `load_settings()`, `check_network_settings()`. A `ConfigError` → print it, exit **2**.
2. `mcp_token_file(...).read() is None` → print `"no MCP token: run tg-bridge-net token --new"`, exit **2**.
3. Build `live` and `source = build_source(settings, live=live, warn_no_bot=True)` (`build_source` gains an optional `live` param). Run `await live.whoami()`. `NotLoggedInError` → print the login command, exit **3**. Any other connect error → log the class name, exit **1** (systemd retries after `RestartSec`).
4. `admin_server = start_admin_in_thread(settings, live, loop, hidden)`.
5. `admin_token_file.load_or_create()`, then `send_login_link(settings, admin_link(...))` in `loop.run_in_executor`, so a slow bot never delays MCP.
6. `await uvicorn.Server(Config(build_http_app(...), host=mcp_host, port=mcp_port, log_config=None, access_log=False, lifespan="on")).serve()`.
7. `finally`: `admin_server.shutdown()`, `await source.close()` (`MCPServer`'s lifespan also closes it, so closing must be idempotent: check `LiveSource.close`).
- Log line on start: `"starting tg-bridge-net (http %s:%d, admin 127.0.0.1:%d, scope=%s)"`, with no secrets.

- [ ] **Step 1: Write failing tests:**
  - `test_serve_no_token_exit_2` (subprocess, clean HOME, creds + `TG_BRIDGE_MCP_HOST=127.0.0.1`).
  - `test_serve_wildcard_host_exit_2`.
  - `test_serve_scope_all_exit_2`.
  - `test_serve_not_logged_in_exit_3` (token present, no session file).
  - `test_token_new_prints_once_and_writes_600`.
  - `test_link_send_without_bot_exit_1`.
  - In-process `test_serve_startup_order`: monkeypatch `live.whoami`, `start_admin_in_thread`, `send_login_link`, `uvicorn.Server.serve` with recorders. Assert the call order `whoami → admin → send → serve`, and that `send` never ran when `whoami` raised `NotLoggedInError`.
  - `test_serve_logs_no_secrets` (caplog over the in-process start: no MCP token, admin token or bot token).
- [ ] **Step 2–4:** FAIL → implement → `uv run pytest -q` PASS.
- [ ] **Step 5: Commit** `feat(net): tg-bridge-net serve/token/link`

### Task 8: Deploy files and docs

**Files:**
- Create `deploy/tg-bridge.service` and `deploy/COMMANDS.md`.
- Modify `docs/setup.md`, `docs/design.md` §6 and §8, `docs/roadmap.md` (row 11 → `[~]` with the plan link), and `docs/features/11-network-mcp.md` (smoke list unchanged unless Task 3 or 1 changed something).

- `deploy/tg-bridge.service` (system unit; exact keys):
  - `User=yurii`, `WorkingDirectory=/home/yurii/tg-bridge`
  - `ExecStart=/home/yurii/.local/bin/uv run --frozen tg-bridge-net serve`
  - `Restart=always`, `RestartSec=10`, `RestartPreventExitStatus=2 3`
  - `NoNewPrivileges=yes`, `PrivateTmp=yes`
  - `After=network-online.target tailscaled.service`, `Wants=network-online.target`
  - No `EnvironmentFile`: settings come from `~/.config/tg-bridge/config.env`, which `load_settings` already reads and checks for `600`.
- `deploy/COMMANDS.md` (installed by copying to `/home/yurii/tg-bridge-COMMANDS.md`, or read in place) has copy-paste blocks, one per action:
  - status, restart, stop/start, `journalctl -u tg-bridge -f`;
  - `tg-bridge-net token --new` (+ where to paste it in Hermes, + `systemctl --user restart hermes-gateway` on CT 110);
  - `tg-bridge-net link --send`, `tg-bridge-net link --new --send`;
  - re-login (`systemctl stop tg-bridge`, then `uv run python -m tg_bridge.login --method qr`, then start);
  - update (`git pull && uv sync --frozen && sudo systemctl restart tg-bridge`);
  - `tailscale serve --bg 8765`.
  Every command is prefixed with `cd ~/tg-bridge &&` where needed.
- `setup.md` "Homelab LXC" covers:
  - the CT 120 facts (`192.168.10.22`, `tg-bridge.tail4be1a2.ts.net`, user `yurii`);
  - the `config.env` keys from Task 1 with example values (the bot token as `<token>`);
  - install the unit, `tailscale serve`;
  - the Proxmox rule `IN ACCEPT -source 192.168.10.20 -p tcp -dport 8766` in `/etc/pve/firewall/120.fw` (ask the owner before adding it);
  - the Hermes `mcp_servers` snippet from the brief with port `8766`, plus the mode recorded in Task 3.
- `design.md` §8: replace the "No MCP network listener" bullet with the network rules (bearer token, explicit bind, `scope=all` refused, the two separate keys, page on loopback via Tailscale, bot-chat guard, exit codes). §6: one paragraph on the network process.

- [ ] **Step 1:** write the files. **Step 2:** `uv run pytest -q` → PASS (docs only, sanity check).
- [ ] **Step 3: Commit** `docs: homelab deployment for feature 11`

---

## After implementation (with the owner, not the implement session)

Run on CT 120: `git pull`, `uv sync`, `config.env`, `tg_bridge.login --method qr` (with `TG_BRIDGE_DEVICE_NAME`), `tg-bridge-net token --new`, install the unit, `tailscale serve`, the firewall rule (ask first), and the Hermes config. Then the brief's smoke checklist.
