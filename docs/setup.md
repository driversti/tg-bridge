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
