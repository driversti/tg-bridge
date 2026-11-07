import json
import logging
import urllib.error

from tg_bridge.config import load_settings
from tg_bridge.notify import admin_link, send_login_link

BOT_TOKEN = "555:bot-secret-part"
ADMIN_TOKEN = "admin-secret-token-" + "a" * 20
CREDS = {"TG_API_ID": "1", "TG_API_HASH": "x"}


def settings(tmp_path, **extra):
    return load_settings(env={**CREDS, **extra}, config_file=tmp_path / "none.env")


def full(tmp_path, **extra):
    return settings(
        tmp_path,
        TG_BRIDGE_NOTIFY_BOT_TOKEN=BOT_TOKEN,
        TG_BRIDGE_NOTIFY_CHAT_ID="-42",
        TG_BRIDGE_DEVICE_NAME="tg-bridge (homelab)",
        **extra,
    )


def test_admin_link_uses_admin_url(tmp_path):
    s = settings(tmp_path, TG_BRIDGE_ADMIN_URL="https://tg-bridge.tail4be1a2.ts.net")
    assert admin_link(s, "T") == "https://tg-bridge.tail4be1a2.ts.net/login?token=T"


def test_admin_link_defaults_to_loopback(tmp_path):
    assert admin_link(settings(tmp_path), "T") == "http://127.0.0.1:8765/login?token=T"


def test_no_bot_settings_no_request(tmp_path):
    def post(url, body):
        raise AssertionError("must not be called")

    assert send_login_link(settings(tmp_path), "http://x/login?token=T", post) is False
    only_token = settings(tmp_path, TG_BRIDGE_NOTIFY_BOT_TOKEN=BOT_TOKEN)
    assert send_login_link(only_token, "http://x/login?token=T", post) is False


def test_sends_expected_payload(tmp_path):
    calls = []

    def post(url, body):
        calls.append((url, json.loads(body)))
        return 200

    link = "https://h.ts.net/login?token=T"
    assert send_login_link(full(tmp_path), link, post) is True
    (url, payload), = calls
    assert url == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    assert payload == {
        "chat_id": -42,
        "text": f"tg-bridge started on tg-bridge (homelab).\nAdmin page (Tailscale): {link}",
        "link_preview_options": {"is_disabled": True},
    }


def test_http_error_logs_no_secret(tmp_path, caplog):
    link = f"https://h.ts.net/login?token={ADMIN_TOKEN}"

    def post(url, body):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    with caplog.at_level(logging.DEBUG):
        assert send_login_link(full(tmp_path), link, post) is False
    assert "login link not sent" in caplog.text
    assert BOT_TOKEN not in caplog.text and "bot-secret-part" not in caplog.text
    assert ADMIN_TOKEN not in caplog.text


def test_non_200_status_returns_false_and_logs_status(tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        assert send_login_link(full(tmp_path), "http://x/login?token=T", lambda u, b: 403) is False
    assert "403" in caplog.text


def test_timeout_returns_false(tmp_path, caplog):
    def post(url, body):
        raise TimeoutError(f"timed out talking to {url}")

    with caplog.at_level(logging.DEBUG):
        assert send_login_link(full(tmp_path), "http://x/login?token=T", post) is False
    assert "TimeoutError" in caplog.text
    assert "bot-secret-part" not in caplog.text
