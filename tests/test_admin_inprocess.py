"""The admin page inside a running event loop (network mode): Telegram work goes to the MCP loop."""

import asyncio
import http.client
import socket
import threading
import time
from dataclasses import replace

import pytest

from tests.fakes import make_chat
from tg_bridge import admin
from tg_bridge.admin import snapshot_from, start_admin_in_thread
from tg_bridge.config import load_settings
from tg_bridge.token_file import admin_token_file

TITLE = "In Process Club"


class FakeLive:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []  # (what, id of the loop it ran on)
        self.photo_calls = 0

    async def list_dialogs(self, *, refresh=False):
        self.calls.append(("dialogs", id(asyncio.get_running_loop())))
        await asyncio.sleep(0.2)
        return [make_chat(id=-1001000000050, kind="supergroup", title=TITLE, username=None, photo_id=1)]

    async def list_folders(self, *, refresh=False):
        self.calls.append(("folders", id(asyncio.get_running_loop())))
        return []

    async def download_chat_photo(self, chat_id):
        self.photo_calls += 1
        return b"\xff\xd8jpeg"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def loop_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(5)
    loop.close()


def get(port, path, cookie=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Cookie": f"tgb_admin={cookie}"} if cookie else {}
    if host:
        headers["Host"] = host
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


async def test_snapshot_from_uses_the_given_source_and_closes_nothing():
    live = FakeLive()
    snap = await snapshot_from(live)
    assert [c.title for c in snap.chats] == [TITLE]
    assert [name for name, _ in live.calls] == ["dialogs", "folders"]


async def test_download_photos_with_stores_into_photo_dir(tmp_path):
    live = FakeLive()
    chat = make_chat(id=-1001000000050, kind="supergroup", title=TITLE, username=None, photo_id=1)
    await admin.download_photos_with(live, tmp_path, [chat])
    assert live.photo_calls == 1 and any(tmp_path.iterdir())


def test_inprocess_fetch_while_loop_busy(loop_thread, tmp_path):
    port = free_port()
    settings = replace(
        load_settings(
            env={"TG_API_ID": "1", "TG_API_HASH": "x", "TG_BRIDGE_SESSION": str(tmp_path / "s.session")},
            config_file=tmp_path / "none.env",
        ),
        admin_port=port,
    )
    token = admin_token_file(settings.session_path).load_or_create()
    live = FakeLive()
    busy_done = threading.Event()

    async def busy():  # an MCP tool call in flight on the same loop
        await asyncio.sleep(0.4)
        busy_done.set()

    asyncio.run_coroutine_threadsafe(busy(), loop_thread)
    server = start_admin_in_thread(settings, live, loop_thread, frozenset())
    try:
        cookie = admin.derive_secrets(token)[0]
        deadline = time.monotonic() + 10
        body = ""
        while time.monotonic() < deadline and TITLE not in body:
            _, body = get(port, "/", cookie)
            time.sleep(0.05)
        assert TITLE in body
        assert busy_done.wait(5)
        # Both ran on one loop and one source instance.
        assert {loop_id for _, loop_id in live.calls} == {id(loop_thread)}
    finally:
        server.shutdown()
        server.server_close()


def test_start_admin_rejects_foreign_host_and_sets_secure_cookie(loop_thread, tmp_path):
    port = free_port()
    settings = replace(
        load_settings(
            env={
                "TG_API_ID": "1",
                "TG_API_HASH": "x",
                "TG_BRIDGE_SESSION": str(tmp_path / "s.session"),
                "TG_BRIDGE_ADMIN_URL": "https://tg-bridge.tail4be1a2.ts.net",
            },
            config_file=tmp_path / "none.env",
        ),
        admin_port=port,
    )
    token = admin_token_file(settings.session_path).load_or_create()
    server = start_admin_in_thread(settings, FakeLive(), loop_thread, frozenset())
    try:
        assert get(port, f"/login?token={token}", host="192.168.10.22:8765")[0] == 400
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", f"/login?token={token}", headers={"Host": "tg-bridge.tail4be1a2.ts.net"})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 303 and "Secure" in resp.getheader("Set-Cookie")
        conn.close()
    finally:
        server.shutdown()
        server.server_close()
