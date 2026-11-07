# Setup & deployment (stage 1: Mac)

## 1. Credentials

1. Create the private config dir and an empty config file, then lock both down
   (the file must exist before `chmod` can set its mode):

   ```bash
   mkdir -p ~/.config/tg-bridge
   chmod 700 ~/.config/tg-bridge
   touch ~/.config/tg-bridge/config.env
   chmod 600 ~/.config/tg-bridge/config.env
   ```

2. Get your own `api_id` / `api_hash` from Telegram, following the official guide
   ([Obtaining api_id](https://core.telegram.org/api/obtaining_api_id)):
   log in at <https://my.telegram.org> with your phone number → **API development tools** →
   fill in the app form (any title/short name) → copy `App api_id` and `App api_hash`.
   They identify *your* app, so treat them as secrets.

3. Put them into `~/.config/tg-bridge/config.env`:

   ```
   TG_API_ID=<your api_id>
   TG_API_HASH=<your api_hash>
   ```

Agents: never echo, log or commit these values.

> The project requires Python 3.14. The first `uv run` after moving to it rebuilds `.venv`
> on 3.14, so the first LM Studio call is slower.

## 2. First login (interactive)

`uv run python -m tg_bridge.login` asks how to log in and creates
`~/.config/tg-bridge/tg-bridge.session` (`600`):

- **QR code** (default, `--method qr`): scan the QR shown in the terminal with Telegram on your
  phone (Settings → Devices → Link Desktop Device). No login code is needed. Recommended:
  Telegram sometimes doesn't deliver login codes to third-party apps at all.
- **Login code** (`--method code`): enter your phone number; the script says where Telegram sent
  the code (Telegram app, SMS, call, email) and lets you resend it by pressing Enter.

Either way, a two-step verification password is asked if you have one. The session shows up as
a new device in Telegram → Settings → Devices, where you can revoke it at any time.

## 3. LM Studio

Add to LM Studio's `mcp.json`. The file is `~/.lmstudio/mcp.json`; edit it directly if you
can't find the menu entry (it moves between LM Studio versions), then restart LM Studio:

```json
{
  "mcpServers": {
    "tg-bridge": {
      "command": "/opt/homebrew/bin/uv",
      "args": ["--directory", "/absolute/path/to/tg-bridge", "run", "tg-bridge"],
      "env": { "TG_BRIDGE_SCOPE": "all" }
    }
  }
}
```

`TG_BRIDGE_SCOPE` defaults to `public`, which shows public channels and supergroups, plus the private chats
(people, bots, groups, channels) you switch on in the allowlist page. Without `"TG_BRIDGE_SCOPE": "all"` in `env`,
LM Studio sees no private chats unless you switched them on.

Use absolute paths: `which uv` for `command` (`/opt/homebrew/bin/uv` with Homebrew,
`~/.local/bin/uv` with the official installer) and your clone's location in `args`.
LM Studio is a GUI app and doesn't get your shell `PATH`.
The server connects to Telegram only on the first tool call, so the tools list appears
even before login. `whoami` then tells you to run step 2.

- Model: a tool-calling model around 30B with a context of at least 32k. Candidates to
  evaluate: Qwen3-30B-A3B, Qwen3-32B, Gemma 3 27B. Record results here.

## 4. Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `TG_API_ID`, `TG_API_HASH` | — | Telegram app credentials |
| `TG_BRIDGE_SESSION` | `~/.config/tg-bridge/tg-bridge.session` | Session file |
| `TG_BRIDGE_TZ` | `Europe/Warsaw` | Time zone for periods/output |
| `TG_BRIDGE_MAX_CHARS` | `12000` | Per-tool-call output budget, including the `Now:` line (minimum 500) |
| `TG_BRIDGE_EXPORT_DIR` | `~/tg-bridge-data/exports` | Markdown exports |
| `TG_BRIDGE_DOWNLOAD_DIR` | `~/tg-bridge-data/downloads` | Downloaded media |
| `TG_BRIDGE_MAX_DOWNLOAD_MB` | `200` | Largest file download_media fetches (MB) |
| `TG_BRIDGE_SCOPE` | `public` | `all`: every chat. `public`: public channels/supergroups always, plus any private chat (people and bots included) switched on in the allowlist page |
| `TG_BRIDGE_ADMIN_PORT` | `8765` | Port of the allowlist page (`tg-bridge-admin`), bound to `127.0.0.1` only |
| `TG_BRIDGE_DEVICE_NAME` | `tg-bridge` | Name of the session in Telegram, Settings, Devices (1 to 64 characters). Set it before the first login |
| `TG_BRIDGE_MCP_HOST` | unset | Network mode: LAN address to bind. Required; wildcards are refused |
| `TG_BRIDGE_MCP_PORT` | `8766` | Network mode: MCP port (must differ from the admin port) |
| `TG_BRIDGE_ADMIN_URL` | unset | Network mode: the page's public URL (`https://<host>.<tailnet>.ts.net`, no path); used in the login link and the `Host` check |
| `TG_BRIDGE_NOTIFY_BOT_TOKEN` | unset | Network mode: token of the Homelab bot that receives the login link (`<digits>:<rest>`, a secret) |
| `TG_BRIDGE_NOTIFY_CHAT_ID` | unset | Network mode: the owner's chat id with that bot |
| `TG_BRIDGE_NET_ALLOW_ALL` | `0` | Network mode: `1` allows `TG_BRIDGE_SCOPE=all` (never needed for Hermes) |

### Allowlist page

For `public` scope, the owner switches private chats (groups, channels, people, bots, Saved Messages) on or off in a small web page:

```
uv run tg-bridge-admin
```

Open the `http://127.0.0.1:<port>/login?token=...` link it prints (the link sets a cookie; without it every
request is refused). The link is stable: the token lives in `<session stem>.admin-token` next to the session (mode `600`), so bookmark it; the cookie lasts a year. `uv run tg-bridge-admin --link` prints it again without starting anything; `--new-link` replaces the token (old links and cookies stop working). Rows have Open and Copy for the link: public chats `t.me/<username>`, private ones `t.me/c/<id>/<latest message>`, people and bots `t.me/<username>` or `tg://user?id=`. The page has tabs: Groups, Channels, People and Mine (what you created, plus Saved Messages). Each row shows
icon, title, kind and a link; new chats start off, and a switch
applies on the next tool call. Switching on a person, a bot or Saved Messages asks for confirmation. Small chat icons
are cached in `<session stem>.photos/` next to the session (dir `700`), so a second Refresh only fetches changed ones. The page opens at once; icons arrive in the background (25 per short session, with a pause between batches; `Icons: n/m` shows progress, reload to see more). It reads Telegram only at start, on
Refresh and in those short batches, so it does not keep the session locked while LM Studio works. If the session is busy, press Refresh
again in a moment.

From the phone: run `tailscale serve --bg <port>` on the Mac, then open the same login path on the Mac's tailnet name.
The page stays bound to `127.0.0.1`; only your tailnet devices can reach it, and they still need the login link.

## 5. Troubleshooting

- MCP server shows no tools: something printed to stdout. All logs must go to stderr.
- "Not logged in": rerun step 2.
- FloodWait: Telegram rate limit, so wait the reported time.

## 6. Online status

What the code does to stay invisible: connects lazily (first tool call, not at startup),
`receive_updates=False`, never calls `account.UpdateStatus`, never sends read receipts.
Telegram may still show "online"/"last seen recently" while a connection is open; that is
accepted (design §3).

Observed during the feature 00 smoke test (LM Studio 0.4.26): while the server ran, contacts
saw "last seen <time>", not "online". A tool call may bump "last seen"; not investigated further.

## 7. Performance

LM Studio starts a fresh server process for every chat message, so in-memory caches don't help.
The dialog list (888 dialogs: ~2.5–3 s to fetch) is therefore cached on disk for 5 minutes in
`~/.config/tg-bridge/tg-bridge.dialogs.json` (file `600`, next to the session). It contains chat
titles, usernames and visible phones: treat it like the session file. Deleting it is always safe.
After logging in as a different account, delete it or wait 5 minutes.

Most of the waiting is the model, not the bridge. On this Mac a dense 27B model (`qwen3.6-27b`)
read a 4k-token tool result at ~94 tok/s (~46 s). Prefer MoE models (now `google/gemma-4-26b-a4b-qat`),
and keep tool output short: `list_chats` prints totals per kind (plus public/private groups and channels, so the model never subtracts) on line 2, so "how many" questions need `limit=1`.

## 8. Homelab LXC (network mode, feature 11)

`tg-bridge-net` runs in its own LXC and serves the bridge to Hermes over the LAN. Facts: **CT 120 `tg-bridge`**,
`192.168.10.22`, tailnet name `tg-bridge.tail4be1a2.ts.net`, user `yurii`, repo in `/home/yurii/tg-bridge`, Hermes on
CT 110 (`192.168.10.20`). It has its **own Telegram login**: never copy the Mac's session file.

1. Install `uv`, clone the repo to `~/tg-bridge`, run `uv sync --frozen` (Python 3.14).
2. `~/.config/tg-bridge/config.env` (dir `700`, file `600`):

   ```
   TG_API_ID=<your api_id>
   TG_API_HASH=<your api_hash>
   TG_BRIDGE_DEVICE_NAME=tg-bridge (homelab)
   TG_BRIDGE_MCP_HOST=192.168.10.22
   TG_BRIDGE_MCP_PORT=8766
   TG_BRIDGE_ADMIN_URL=https://tg-bridge.tail4be1a2.ts.net
   TG_BRIDGE_NOTIFY_BOT_TOKEN=<token>
   TG_BRIDGE_NOTIFY_CHAT_ID=<your chat id with the bot>
   ```

   The scope stays `public` (the default). Telegram may only apply a changed device name at login, so set it first.
3. Log in: `uv run python -m tg_bridge.login --method qr`.
4. Create the MCP token: `uv run tg-bridge-net token --new` (printed once).
5. Install the unit: `sudo cp deploy/tg-bridge.service /etc/systemd/system/ && sudo systemctl daemon-reload &&
   sudo systemctl enable --now tg-bridge`. Copy `deploy/COMMANDS.md` to `~/tg-bridge-COMMANDS.md` for the owner.
6. Page over HTTPS from the phone: `sudo tailscale serve --bg 8765`. The login link arrives in the Homelab bot chat on every start.
7. Firewall (ask the owner before adding it): in `/etc/pve/firewall/120.fw` allow Hermes only:
   `IN ACCEPT -source 192.168.10.20 -p tcp -dport 8766`.
8. Hermes `~/.hermes/config.yaml` on CT 110, then `systemctl --user restart hermes-gateway`:

   ```yaml
   mcp_servers:
     tg-bridge:
       url: http://192.168.10.22:8766/mcp
       headers:
         Authorization: "Bearer <token>"
   ```

   Compatibility with Hermes v0.23.0 (`mcp` 2.0.0) was checked with `scripts/hermes_compat.py` against the SDK's
   default mode (stateful sessions, SSE responses): it works, so no `stateless_http` or JSON mode is needed.

Exit codes `2` (config) and `3` (not logged in) are not restarted by systemd: fix the cause (see `deploy/COMMANDS.md`),
then `sudo systemctl start tg-bridge`.
