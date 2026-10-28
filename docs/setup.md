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
      "args": ["--directory", "/absolute/path/to/tg-bridge", "run", "tg-bridge"]
    }
  }
}
```

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
