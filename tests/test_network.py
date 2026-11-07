"""`tg-bridge-net serve` wiring, in process: order of startup steps, and that nothing secret is logged."""

import asyncio
import logging
import socket
import threading
from dataclasses import replace

import pytest
import uvicorn

from tests.fakes import make_person
from tg_bridge import network
from tg_bridge.config import load_settings
from tg_bridge.errors import NotLoggedInError
from tg_bridge.source.live import LiveSource
from tg_bridge.token_file import admin_token_file, mcp_token_file

BOT_TOKEN = "555:bot-secret-part"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def settings(tmp_path):
    env = {
        "TG_API_ID": "1",
        "TG_API_HASH": "x",
        "TG_BRIDGE_SESSION": str(tmp_path / "s.session"),
        "TG_BRIDGE_MCP_HOST": "127.0.0.1",
        "TG_BRIDGE_NOTIFY_BOT_TOKEN": BOT_TOKEN,
        "TG_BRIDGE_NOTIFY_CHAT_ID": "7",
    }
    base = load_settings(env=env, config_file=tmp_path / "none.env")
    return replace(base, mcp_port=free_port(), admin_port=free_port())


class FakeAdminServer:
    def __init__(self, calls):
        self.calls = calls

    def shutdown(self):
        self.calls.append("admin-shutdown")

    def server_close(self):
        pass


@pytest.fixture
def recorder(monkeypatch):
    calls: list[str] = []
    sent = threading.Event()

    async def whoami(self):
        calls.append("whoami")
        return make_person()

    def start_admin(settings, live, loop, hidden):
        calls.append("admin")
        return FakeAdminServer(calls)

    def send(settings, link):
        calls.append("send")
        sent.set()
        return True

    async def serve(self):
        # The send runs off-loop and concurrently: wait for it so the order is deterministic.
        await asyncio.get_running_loop().run_in_executor(None, sent.wait, 5)
        calls.append("serve")

    monkeypatch.setattr(LiveSource, "whoami", whoami)
    monkeypatch.setattr(network, "start_admin_in_thread", start_admin)
    monkeypatch.setattr(network, "send_login_link", send)
    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    return calls


async def test_serve_startup_order(settings, recorder):
    mcp_token_file(settings.session_path).rotate()
    assert await network.serve(settings) == 0
    assert recorder[:4] == ["whoami", "admin", "send", "serve"]
    assert recorder[-1] == "admin-shutdown"


async def test_not_logged_in_never_sends_or_serves(settings, recorder, monkeypatch):
    mcp_token_file(settings.session_path).rotate()

    async def refused(self):
        recorder.append("whoami")
        raise NotLoggedInError("nope")

    monkeypatch.setattr(LiveSource, "whoami", refused)
    assert await network.serve(settings) == 3
    assert recorder == ["whoami"]


async def test_other_connect_error_exits_1_with_class_name_only(settings, recorder, monkeypatch, caplog):
    mcp_token_file(settings.session_path).rotate()

    async def broken(self):
        raise ConnectionError(f"cannot reach {BOT_TOKEN}")

    monkeypatch.setattr(LiveSource, "whoami", broken)
    with caplog.at_level(logging.DEBUG):
        assert await network.serve(settings) == 1
    assert "ConnectionError" in caplog.text and "bot-secret-part" not in caplog.text
    assert recorder == []


async def test_no_mcp_token_exits_2_before_connecting(settings, recorder):
    assert await network.serve(settings) == 2
    assert recorder == []


async def test_serve_logs_no_secrets(settings, recorder, caplog):
    mcp = mcp_token_file(settings.session_path).rotate()
    with caplog.at_level(logging.DEBUG):
        await network.serve(settings)
    admin = admin_token_file(settings.session_path).read()
    assert "starting tg-bridge-net" in caplog.text
    for secret in (mcp, admin, BOT_TOKEN, "bot-secret-part"):
        assert secret and secret not in caplog.text
