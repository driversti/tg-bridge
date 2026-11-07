import logging
import socket
import threading
from pathlib import Path
import time
from dataclasses import replace

import httpx2
import pytest
import uvicorn
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tg_bridge.config import load_settings
from tg_bridge.http_app import BearerAuth, build_http_app
from tg_bridge.server import build_server
from tests.fakes import FakeSource, make_person
from tg_bridge.token_file import TokenFile

TOKEN = "k" * 40
CREDS = {"TG_API_ID": "1", "TG_API_HASH": "x"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
POST_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, token):
        self.port = free_port()
        settings = replace(
            load_settings(env=CREDS, config_file=Path("/nonexistent.env")),
            mcp_host="127.0.0.1",
            mcp_port=self.port,
        )
        source = FakeSource(me=make_person(first_name="Anna", last_name="K", username="anna_k"))
        app = build_http_app(build_server(source, settings), settings, token)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def running():
    made = []

    def make(token=lambda: TOKEN):
        r = Running(token)
        r.start()
        made.append(r)
        return r

    yield make
    for r in made:
        r.stop()


def post(r: Running, headers: list[tuple[str, str]], host: str | None = None) -> int:
    h = httpx2.Headers(list(POST_HEADERS.items()) + headers)
    if host:
        h["Host"] = host
    return httpx2.post(r.url, headers=h, json=INIT, timeout=10).status_code


def test_no_header_401(running):
    r = running()
    resp = httpx2.post(r.url, headers=POST_HEADERS, json=INIT)
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert resp.json() == {"error": "unauthorized"}


def test_wrong_token_401(running):
    assert post(running(), [("Authorization", "Bearer nope")]) == 401


def test_two_auth_headers_401(running):
    assert post(running(), [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")]) == 401


@pytest.mark.parametrize("value", ["Bearer", f"Basic {TOKEN}", f"Bearer a {TOKEN}"])
def test_empty_or_odd_bearer_401(running, value):
    assert post(running(), [("Authorization", value)]) == 401


def test_lowercase_scheme_and_extra_spaces_ok(running):
    r = running()
    assert post(r, [("Authorization", f"bearer   {TOKEN}")]) == 200


def test_token_none_refuses_everything(running):
    assert post(running(lambda: None), [("Authorization", f"Bearer {TOKEN}")]) == 401


def test_bad_host_refused(running):
    status = post(running(), [("Authorization", f"Bearer {TOKEN}")], host="evil.example")
    assert status >= 400 and status != 200


async def test_tool_call_end_to_end(running):
    r = running()
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"})
    async with streamable_http_client(r.url, http_client=client) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert "list_chats" in {t.name for t in tools.tools}
            result = await session.call_tool("whoami", {})
            assert "Anna" in result.content[0].text
    await client.aclose()


def test_rotation_applies_without_restart(running, tmp_path):
    a, b = TokenFile(tmp_path / "m.token"), TokenFile(tmp_path / "m.token")
    old = a.rotate()
    r = running(a.read)
    assert post(r, [("Authorization", f"Bearer {old}")]) == 200
    new = b.rotate()
    assert post(r, [("Authorization", f"Bearer {old}")]) == 401
    assert post(r, [("Authorization", f"Bearer {new}")]) == 200


def test_auth_failure_logs_no_token(running, caplog):
    r = running()
    with caplog.at_level(logging.DEBUG):
        post(r, [("Authorization", "Bearer secret-wrong-token")])
    assert "mcp auth refused" in caplog.text
    assert "secret-wrong-token" not in caplog.text


async def test_lifespan_scope_passes_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await BearerAuth(inner, lambda: None)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


async def test_empty_bearer_value_refused_at_asgi_level():
    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("must not reach the app")

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer ")]}
    await BearerAuth(inner, lambda: TOKEN)(scope, None, send)
    assert sent[0]["status"] == 401
