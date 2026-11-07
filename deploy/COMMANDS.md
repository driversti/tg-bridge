# tg-bridge: commands cheat sheet (CT 120)

Run on CT 120 (`192.168.10.22`) as user `yurii`. Everything is copy-paste.

## Service

Status:

```bash
systemctl status tg-bridge
```

Restart (a new login link arrives in the Homelab bot chat):

```bash
sudo systemctl restart tg-bridge
```

Stop:

```bash
sudo systemctl stop tg-bridge
```

Start:

```bash
sudo systemctl start tg-bridge
```

Follow the logs:

```bash
journalctl -u tg-bridge -f
```

## Tokens and links

Rotate the MCP token (printed once; the old one stops working at once, no restart needed):

```bash
cd ~/tg-bridge && uv run tg-bridge-net token --new
```

Then on CT 110 paste it into `~/.hermes/config.yaml` under `mcp_servers.tg-bridge.headers.Authorization` as `"Bearer <token>"` and restart Hermes:

```bash
systemctl --user restart hermes-gateway
```

Print the current MCP token:

```bash
cd ~/tg-bridge && uv run tg-bridge-net token --show
```

Print the admin page link:

```bash
cd ~/tg-bridge && uv run tg-bridge-net link
```

Send the link again through the Homelab bot:

```bash
cd ~/tg-bridge && uv run tg-bridge-net link --send
```

Rotate the admin link (old links and cookies die at once) and send the new one:

```bash
cd ~/tg-bridge && uv run tg-bridge-net link --new --send
```

## Telegram login

Re-login (the service exits with code 3 when the session is gone or revoked and stays down until you do this). Scan the QR with Telegram on the phone (Settings, Devices, Link Desktop Device):

```bash
sudo systemctl stop tg-bridge
```

```bash
cd ~/tg-bridge && uv run python -m tg_bridge.login --method qr
```

```bash
sudo systemctl start tg-bridge
```

## Update

```bash
cd ~/tg-bridge && git pull && uv sync --frozen && sudo systemctl restart tg-bridge
```

## Tailscale (admin page from the phone)

```bash
sudo tailscale serve --bg 8765
```
