# 11 — Networked MCP with auth (bridge in its own LXC)

**Design refs:** §6 (architecture), §8 (security), feature 17 brief (scope, admin page) · **Depends on:** 17 · **Blocks:** 14 (phone via Hermes)

## Goal
The owner asks Hermes questions about Telegram **from the phone, away from home**. Example: "find post 'Недільний AI Чекін' #104 in @imatrofAI and list what people shared, with a short description of each". They can also ask for channel and folder summaries and search across visible chats. Hermes (Nous Research Hermes Agent, LXC 110, cloud model, Telegram front-end) reads Telegram through this bridge as an MCP client over the home LAN.

The phone talks only to Hermes via Telegram. The bridge is never exposed to the internet, and MCP needs no Tailscale.

Decided with the owner (2026-11-07):
- The bridge runs in **its own Proxmox LXC** on the same LAN as Hermes (`192.168.10.0/24`; Hermes is `192.168.10.20`). It uses its **own Telegram login**: a separate session file, never copied from the Mac.
- `scope=public` (feature 17) for this process: public chats always, private ones only when switched on in the page.
- The admin page is reachable from the phone through **Tailscale** (the owner has it on the phone). The login link is delivered by the owner's existing **Homelab bot** on **every start** of the bridge, so a lost link is easy to find again.
- A short **commands cheat sheet** lives next to the bridge in the LXC, so the owner can restart it, rotate the link, resend it or check logs without asking Claude.
- The Telegram session gets a **configurable device name**, so it is easy to tell apart in Telegram → Settings → Devices.
- The Mac stays as it is: LM Studio keeps stdio, `scope=all` and its own session.

Container (created 2026-11-07): **CT 120 `tg-bridge`**, clone of the golden image `debian13-template`, `192.168.10.22`, tailnet `tg-bridge.tail4be1a2.ts.net`, user `yurii`, repo in `/home/yurii/tg-bridge`. The owner moved the project to **Python 3.14** (same day).

Facts checked on LXC 110 (2026-11-07): Hermes **v0.23.0**, runs as `hermes-gateway.service` (systemd user unit, user `yurii`). MCP servers go under `mcp_servers:` in `~/.hermes/config.yaml`. Remote servers use `url` + `headers`, the transport is streamable HTTP by default (`transport: sse` is optional), and custom headers like `Authorization: Bearer …` are supported. No MCP servers are configured yet. Proxmox firewall is on for that container (`firewall=1`).

## Scope

### Network MCP process
- A long-running mode of the bridge serving MCP over **streamable HTTP**. stdio stays the default for LM Studio; the command or flag name is the plan's call.
- **Bearer token required.** Every MCP request needs `Authorization: Bearer <token>`. A wrong or missing token gets 401 and is not logged with its value; compare in constant time. **No token configured → refuse to start.** The token is long and random, kept in a `600` file next to the session (file only, no env var: Claude's call, so it can rotate without editing the unit). A command creates and rotates it and prints it once for pasting into Hermes' config.
- **Bind:** an explicit address must be configured, and a wildcard (`0.0.0.0`, `::`) is refused. In practice this is the LXC's LAN IP. Check the `Host` header (DNS-rebinding protection; the MCP SDK has transport-security settings for this).
- Plain HTTP between the two LXCs on the home LAN is accepted (Claude's call): the traffic never leaves the LAN, and TLS for MCP is out of scope.
- One process holds one Telegram client for its whole life. The session file is used by this process only, so the Mac's `database is locked` (Log 16) can't happen. The per-process caches can stay in memory or on disk (plan's call), but the 5-minute TTL behaviour of the dialog cache must not get worse.
- Default scope stays `public`. Running the network mode with `scope=all` must be refused unless explicitly forced (Claude's call: a cloud client must never get `all` by mistake).
- Logs go to stderr → journald. They never contain the MCP token, the admin login link or token, the bot token, message text, names or phones.
- **Hermes compatibility first (Claude's call):** Hermes v0.23.0 uses the `mcp` SDK 2.0.0 streamable-HTTP client, and we run `mcp` 2.3. The plan's first step is a check that this client works against our server: the SDK's default stateful sessions, auth header, and a tool call. Do it with the same client version in a test or a throwaway script before building the rest. Fall back to stateless/JSON responses only if that check fails.
- **Rotation without restart (Claude's call):** the MCP token and the admin login token are read from their files on every request (cache by mtime is fine). Rotating takes effect at once and the old value dies immediately. Rotate/resend commands are separate processes that only write the file and call the Bot API; they need no IPC with the server.
- **Startup order and failures (Claude's call):** connect to Telegram and check authorization **before** sending the link.
  - Session missing, revoked or unauthorized → one clear log line, exit with a dedicated exit code. The systemd unit lists that code in `RestartPreventExitStatus=`, so there is no crash loop and no link spam. The cheat sheet's "re-login" command fixes it.
  - Network errors at start → normal exit, systemd restarts with a delay (`RestartSec=`). The link is sent only after a successful connect.

### Admin page inside the process
- The feature-17 page moves into the long-running process on its **own listener, `127.0.0.1` only** (`TG_BRIDGE_ADMIN_PORT`). The owner reaches it via `tailscale serve` (HTTPS) on the LXC. Why loopback and not the LAN (explained to and accepted by the owner): Hermes sits on the same LAN, and loopback means it can't reach the page at all, even if page auth had a bug. Tailscale adds HTTPS, so the login cookie never crosses the LAN in plain text.
- The MCP token is **never** accepted by the page, and the admin cookie never by MCP.
- Same auth as 17: login link with a token → HttpOnly, SameSite=Strict cookie (a year), CSRF on POSTs, CSP, escaped titles, request log off.
- Refresh uses the process's own client instead of connecting and releasing a session. Running page requests and MCP calls together must not corrupt the snapshot or the allowlist.
- Claude's call: keep the stdlib page server in its own thread and hand Telegram work to the MCP event loop (`asyncio.run_coroutine_threadsafe`). No rewrite of the page; the 10 s Refresh cooldown and the photo worker's locking stay as they are.
- In network mode the cookie gets `Secure` (the page is reached over HTTPS via Tailscale). The page also checks `Host` against loopback and the configured Tailscale host. On the Mac (plain `http://127.0.0.1`) both stay as today.
- `tg-bridge-admin` as a standalone command keeps working on the Mac (stdio setup), unchanged.

### Login link via the Homelab bot
- On **every start** of the network process, and on rotate (`--new-link`) or resend (`--send-link`, names are the plan's call), the bridge sends the login link to the owner through the **Homelab bot** using the Telegram **Bot API** (HTTPS to `api.telegram.org`, stdlib is enough). This is a bot speaking, not the owner's account, so the read-only rule holds; it does not go through Telethon.
- Settings: bot token and the owner's chat id, in an env file with mode `600` (names are the plan's call). The link points at the page's Tailscale URL, configured as a setting (e.g. `https://<lxc>.<tailnet>.ts.net`).
- If sending fails, log a short error without secrets and keep running: the link can still be printed with a command in the LXC.
- If the bot settings are missing, the process still starts and logs that the link wasn't sent (Claude's call: the bot is a convenience, not a security gate).
- **Guard (Claude's call):** the private chat between the owner and the Homelab bot holds login links. The bot's user id is the numeric prefix of its token. Under `public` that chat is **always hidden**, even if switched on in the allowlist, and the page shows it as locked with no switch. Otherwise switching it on would hand Hermes the page link. If no bot token is configured, the guard has nothing to hide (this setup never sent a link) and the process logs a warning.

### Session name
- A setting for the Telegram `device_model`, default `tg-bridge` (today's value in `telegram/identity.py`), set e.g. to `tg-bridge (homelab)`. It lives in the shared `telegram/identity.py`, so every entry point uses it (login, network mode, and the Mac's stdio and admin, which keep the default). The plan checks whether Telegram's Devices list picks up a changed name on reconnect or only at login, and documents it.

### Deployment (repo files + owner-facing docs)
- `deploy/`: a systemd unit (runs as a non-root user, `Restart=always`, env file), and the cheat sheet `deploy/COMMANDS.md`, installed next to the bridge in the LXC. The cheat sheet has copy-paste commands for: status, restart, follow logs, rotate MCP token, rotate/resend login link, print link, re-login to Telegram (QR in the terminal over ssh), update to the latest `main`, and the `tailscale serve` line for the page.
- `setup.md`: a section "Homelab LXC". It covers creating the container (small Debian/Ubuntu, `uv`, Python 3.12+), `tg_bridge.login` with the device name, the env file, the systemd unit, `tailscale serve`, a Proxmox firewall rule allowing the MCP port from `192.168.10.20` only, and the Hermes snippet:
  ```yaml
  mcp_servers:
    tg-bridge:
      url: http://<bridge LAN IP>:<port>/mcp
      headers:
        Authorization: "Bearer <token>"
  ```
- Creating the LXC itself is done at smoke time (with the owner, via the proxmox skill), not by the implement session.

## Out of scope
- TLS for MCP on the LAN, OAuth, multiple tokens or per-client scopes.
- Exposing anything to the internet; MCP over Tailscale.
- Recorder/archive (feature 10). The long-running client should not grow update handlers here.
- Changes to the Mac/LM Studio setup (stays stdio, `scope=all`).
- A Telegram admin bot with commands. The Homelab bot only receives the link.

## Done when
- Tests (no real account, no real network to Telegram):
  - no token → refuses to start; wildcard bind → refuses; `scope=all` in network mode without force → refuses;
  - MCP request with no/wrong token → 401, correct → tool works; bad `Host` → refused;
  - the page rejects the MCP token; MCP rejects the admin cookie;
  - the page listens on loopback only; a toggle made in the page affects the next MCP call in the same process;
  - the bot sender is called on start and on resend with the right URL (fake HTTP); its failure doesn't stop the process; secrets never appear in logs (assert on captured logs);
  - the Homelab bot's chat is hidden under `public` even when allowlisted, and shown locked on the page;
  - the device name setting reaches the client kwargs.
- Existing suite green; stdio mode unchanged.
- Docs: `design.md` §6 (network mode), §8 (replace "No MCP network listener" with the network rules, the two keys, the bot guard), `setup.md` (Homelab LXC), `deploy/COMMANDS.md`, roadmap row.

## Smoke checklist (manual, real account, with the owner)
- [ ] New LXC, fresh login with the custom device name; Telegram's Devices list shows it.
- [ ] Bridge starts under systemd; the Homelab bot delivers the link; restart → a new message arrives.
- [ ] From the phone over Tailscale (away from home or with Wi-Fi off): the link opens the page; without it → refused.
- [ ] From Hermes' LXC: no token → 401; Hermes configured → `hermes mcp list` shows tg-bridge and its tools.
- [ ] From the phone in Telegram: the "Недільний AI Чекін #104 in @imatrofAI" question returns the list.
- [ ] A folder summary and a search work; DMs are out of reach.
- [ ] Switch on one DM in the page → Hermes reads only that DM; off → gone (left over from 17).
- [ ] Global search for a word in a DM and a public channel → only the channel hit (left over from 17).
- [ ] Comments of a public channel with a private discussion group are readable (left over from 17).
- [ ] The Homelab bot chat can't be switched on, and Hermes can't find it.
- [ ] Another LAN device (not Hermes' IP) can't reach the MCP port (firewall).
- [ ] The cheat sheet's commands all work as written.
