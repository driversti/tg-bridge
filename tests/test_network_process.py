"""`tg-bridge-net` as a real process: exit codes and token commands, no Telegram."""

import os
import stat
import subprocess
import sys
from pathlib import Path

from tg_bridge.token_file import mcp_token_file

REPO = Path(__file__).resolve().parent.parent
NET = [str(Path(sys.executable).parent / "tg-bridge-net")]


def clean_env(home: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TG_API_", "TG_BRIDGE_"))}
    env["HOME"] = str(home)
    env.update(extra)
    return env


def run(home, *args, **extra):
    env = clean_env(home, TG_API_ID="1", TG_API_HASH="x", TG_BRIDGE_SESSION=str(home / "s.session"), **extra)
    return subprocess.run(NET + list(args), env=env, cwd=REPO, capture_output=True, text=True, timeout=60)


def test_serve_no_token_exit_2(tmp_path):
    proc = run(tmp_path, "serve", TG_BRIDGE_MCP_HOST="127.0.0.1")
    assert proc.returncode == 2
    assert "token --new" in proc.stderr and "Traceback" not in proc.stderr


def test_serve_without_host_exit_2(tmp_path):
    proc = run(tmp_path, "serve")
    assert proc.returncode == 2 and "TG_BRIDGE_MCP_HOST" in proc.stderr


def test_serve_wildcard_host_exit_2(tmp_path):
    proc = run(tmp_path, "serve", TG_BRIDGE_MCP_HOST="0.0.0.0")
    assert proc.returncode == 2 and "TG_BRIDGE_MCP_HOST" in proc.stderr


def test_serve_scope_all_exit_2(tmp_path):
    proc = run(tmp_path, "serve", TG_BRIDGE_MCP_HOST="127.0.0.1", TG_BRIDGE_SCOPE="all")
    assert proc.returncode == 2 and "TG_BRIDGE_NET_ALLOW_ALL" in proc.stderr


def test_serve_not_logged_in_exit_3(tmp_path):
    mcp_token_file(tmp_path / "s.session").rotate()
    proc = run(tmp_path, "serve", TG_BRIDGE_MCP_HOST="127.0.0.1")
    assert proc.returncode == 3
    assert "tg_bridge.login" in proc.stderr and proc.stdout == ""


def test_token_new_prints_once_and_writes_600(tmp_path):
    proc = run(tmp_path, "token", "--new")
    assert proc.returncode == 0 and proc.stdout == ""
    path = tmp_path / "s.mcp-token"
    token = path.read_text().strip()
    assert proc.stderr.count(token) == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    shown = run(tmp_path, "token", "--show")
    assert shown.stdout.strip() == token


def test_token_show_without_token_exit_2(tmp_path):
    assert run(tmp_path, "token", "--show").returncode == 2


def test_link_prints_and_new_replaces(tmp_path):
    first = run(tmp_path, "link", TG_BRIDGE_ADMIN_URL="https://h.ts.net").stdout.strip()
    assert first.startswith("https://h.ts.net/login?token=")
    assert run(tmp_path, "link", TG_BRIDGE_ADMIN_URL="https://h.ts.net").stdout.strip() == first
    assert run(tmp_path, "link", "--new", TG_BRIDGE_ADMIN_URL="https://h.ts.net").stdout.strip() != first


def test_link_send_without_bot_exit_1(tmp_path):
    proc = run(tmp_path, "link", "--send")
    assert proc.returncode == 1 and "not sent" in proc.stderr
