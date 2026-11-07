"""The allowlist page, over real HTTP on 127.0.0.1 with an injected snapshot (no Telegram)."""

import http.client
import threading
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest

from tests.fakes import make_chat, make_folder, make_person, make_user_chat
from tests.test_config import CREDS
from tg_bridge import admin
from tg_bridge.admin import AdminApp, Snapshot, make_server
from tg_bridge.config import load_settings
from tg_bridge.errors import SessionBusyError
from tg_bridge.source.allowlist import Allowlist

LOGIN, COOKIE, CSRF = "login-token", "session-cookie", "csrf-token"

GROUP = make_chat(id=-555, kind="group", title="Family", username=None)
EVIL = make_chat(id=-1001000000007, kind="supergroup", title="<script>alert(1)</script> Club", username=None)
CHANNEL_PRIV = make_chat(id=-1001000000009, kind="channel", title="Quiet Channel", username=None, is_archived=True)
FORUM = make_chat(id=-1001000000010, kind="supergroup", title="Forum Zone", username=None, is_forum=True)
PUBLIC = make_chat(id=-1001000000008, kind="channel", title="Public News", username="pubnews")
DM = make_user_chat(make_person(id=42, first_name="Anna", last_name="K"))
FOLDER = make_folder(id=5, title="Work <b>", include_ids=(GROUP.id, PUBLIC.id))


def snapshot(**kw):
    fields = dict(
        chats=[GROUP, EVIL, CHANNEL_PRIV, FORUM, PUBLIC, DM],
        folders=[FOLDER],
        photos={EVIL.id: b"\xff\xd8photo", PUBLIC.id: b"public-photo", DM.id: b"dm-photo"},
        fetched_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )
    fields.update(kw)
    return Snapshot(**fields)


class Harness:
    def __init__(self, tmp_path, fetch):
        self.allowlist = Allowlist(tmp_path / "a.json")
        self.fetches = 0
        self._fetch = fetch
        self.app = AdminApp(self._counted, self.allowlist, LOGIN, COOKIE, CSRF)
        self.app.refresh()  # what main() does before it starts serving
        self.server = make_server(self.app, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def _counted(self):
        self.fetches += 1
        return self._fetch()

    def request(self, method, path, body=None, *, cookie=COOKIE, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        hdrs = dict(headers or {})
        if cookie is not None:
            hdrs["Cookie"] = f"tgb_admin={cookie}"
        if body is not None:
            body = urlencode(body)
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def get(self, path, **kw):
        resp, data = self.request("GET", path, **kw)
        return resp, data.decode("utf-8", "replace")

    def post(self, path, body, **kw):
        return self.request("POST", path, body, **kw)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path, lambda: snapshot())
    yield h
    h.close()


def test_binds_loopback_only(harness):
    assert harness.server.server_address[0] == "127.0.0.1"


def test_no_cookie_is_forbidden_everywhere(harness):
    for path in ("/", "/photo/-555", "/nope"):
        resp, body = harness.get(path, cookie=None)
        assert resp.status == 403
        assert "login link" in body
    resp, _ = harness.post("/toggle", {"chat_id": "-555", "on": "1", "csrf": CSRF}, cookie=None)
    assert resp.status == 403
    assert harness.allowlist.load() == frozenset()


def test_wrong_cookie_forbidden(harness):
    assert harness.get("/", cookie="guess")[0].status == 403


def test_login_wrong_token_forbidden(harness):
    resp, _ = harness.get("/login?token=nope", cookie=None)
    assert resp.status == 403
    assert resp.getheader("Set-Cookie") is None
    assert harness.get("/login", cookie=None)[0].status == 403


def test_login_right_token_sets_strict_httponly_cookie(harness):
    resp, _ = harness.get(f"/login?token={LOGIN}", cookie=None)
    assert resp.status == 303 and resp.getheader("Location") == "/"
    cookie = resp.getheader("Set-Cookie")
    assert f"tgb_admin={COOKIE}" in cookie
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie


def test_security_headers(harness):
    resp, _ = harness.get("/")
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert "default-src 'none'" in resp.getheader("Content-Security-Policy")
    assert resp.getheader("Cache-Control") == "no-store"


def test_lists_only_chats_that_need_the_allowlist(harness):
    resp, body = harness.get("/")
    assert resp.status == 200
    assert "Family" in body and "Quiet Channel" in body and "Forum Zone" in body
    assert "Public News" not in body and "Anna" not in body
    assert "3 private chats" not in body and "4 private chats" in body and "0 on" in body


def test_titles_are_escaped(harness):
    _, body = harness.get("/")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Club" in body
    assert "Work &lt;b&gt;" in body and "Work <b>" not in body


def test_links_and_kind_labels(harness):
    _, body = harness.get("/")
    assert 'data-link="https://t.me/c/1000000007"' in body  # the id is -100N: the link carries N only
    assert 'data-link="https://t.me/c/1000000009"' in body
    assert "t.me/c/555" not in body
    assert "no link" in body  # the basic group
    assert "forum" in body and "archived" in body and "supergroup" in body and "channel" in body


def test_photo_or_initial_circle(harness):
    _, body = harness.get("/")
    assert f'src="/photo/{EVIL.id}"' in body
    assert f'src="/photo/{GROUP.id}"' not in body
    assert 'class="avatar">F<' in body  # no photo: first letter of "Family"


def test_search_filter(harness):
    _, body = harness.get("/?q=FAMILY")
    assert "Family" in body and "Quiet Channel" not in body
    _, body = harness.get("/?q=zzz")
    assert "Family" not in body


def test_folder_filter(harness):
    _, body = harness.get(f"/?folder={FOLDER.id}")
    assert "Family" in body and "Quiet Channel" not in body
    _, body = harness.get("/?folder=999")  # unknown folder: ignored
    assert "Quiet Channel" in body


def test_on_chats_come_first_and_counted(harness):
    harness.allowlist.set(FORUM.id, True)
    _, body = harness.get("/")
    assert "1 on" in body
    assert body.index("Forum Zone") < body.index("Family")


def form(chat_id, on, csrf=CSRF, **extra):
    return {"chat_id": str(chat_id), "on": on, "csrf": csrf, **extra}


def test_toggle_requires_csrf(harness):
    assert harness.post("/toggle", {"chat_id": "-555", "on": "1"})[0].status == 403
    assert harness.post("/toggle", form(-555, "1", csrf="wrong"))[0].status == 403
    assert harness.allowlist.load() == frozenset()


def test_toggle_persists_and_redirects_keeping_filters(harness):
    resp, _ = harness.post("/toggle", form(GROUP.id, "1", q="fam", folder="5"))
    assert resp.status == 303
    assert resp.getheader("Location") == "/?q=fam&folder=5"
    assert harness.allowlist.load() == frozenset({GROUP.id})
    harness.post("/toggle", form(GROUP.id, "0"))
    assert harness.allowlist.load() == frozenset()


def test_toggle_redirect_without_filters(harness):
    assert harness.post("/toggle", form(GROUP.id, "1"))[0].getheader("Location") == "/"


@pytest.mark.parametrize("chat", [DM, PUBLIC])
def test_toggle_refuses_chats_that_never_need_it(harness, chat):
    assert harness.post("/toggle", form(chat.id, "1"))[0].status == 400
    assert harness.allowlist.load() == frozenset()


@pytest.mark.parametrize("body", [form(123456, "1"), form("abc", "1"), form(-555, "maybe")])
def test_toggle_rejects_unknown_ids_and_values(harness, body):
    assert harness.post("/toggle", body)[0].status == 400
    assert harness.allowlist.load() == frozenset()


def test_toggle_takes_effect_in_page(harness):
    harness.post("/toggle", form(GROUP.id, "1"))
    _, body = harness.get("/")
    assert "1 on" in body


def test_photo_served_for_listed_chats_only(harness):
    resp, data = harness.request("GET", f"/photo/{EVIL.id}")
    assert resp.status == 200 and resp.getheader("Content-Type") == "image/jpeg"
    assert data == b"\xff\xd8photo"
    assert harness.request("GET", f"/photo/{GROUP.id}")[0].status == 404  # no photo
    assert harness.request("GET", "/photo/999")[0].status == 404
    assert harness.request("GET", "/photo/abc")[0].status == 404
    # Present in the snapshot's dict, but not a chat the page lists:
    assert harness.request("GET", f"/photo/{PUBLIC.id}")[0].status == 404
    assert harness.request("GET", f"/photo/{DM.id}")[0].status == 404


def test_unknown_route_404_and_wrong_method(harness):
    assert harness.get("/nope")[0].status == 404
    assert harness.post("/", {})[0].status in (404, 405)


def test_refresh_requires_csrf_and_refetches(harness):
    before = harness.fetches
    assert harness.post("/refresh", {"csrf": "bad"})[0].status == 403
    assert harness.fetches == before
    resp, _ = harness.post("/refresh", {"csrf": CSRF, "q": "x"})
    assert resp.status == 303 and resp.getheader("Location") == "/?q=x"
    assert harness.fetches == before + 1


def test_refresh_busy_keeps_old_snapshot_and_says_so(tmp_path):
    state = {"busy": False}

    def fetch():
        if state["busy"]:
            raise SessionBusyError()
        return snapshot()

    h = Harness(tmp_path, fetch)
    try:
        state["busy"] = True
        assert h.post("/refresh", {"csrf": CSRF})[0].status == 303
        _, body = h.get("/")
        assert "Telegram session is busy (LM Studio is using it); try Refresh in a moment." in body
        assert "Family" in body  # the previous snapshot is still there
        state["busy"] = False
        h.post("/refresh", {"csrf": CSRF})
        assert "session is busy" not in h.get("/")[1]
    finally:
        h.close()


def test_no_snapshot_yet_shows_notice(tmp_path):
    def fetch():
        raise SessionBusyError()

    h = Harness(tmp_path, fetch)
    try:
        h.app.refresh()
        resp, body = h.get("/")
        assert resp.status == 200 and "busy" in body and "Refresh" in body
    finally:
        h.close()


def test_logs_have_ids_only(harness, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        harness.get(f"/login?token={LOGIN}", cookie=None)
        harness.post("/toggle", form(EVIL.id, "1"))
        harness.get("/")
    text = caplog.text
    assert LOGIN not in text and COOKIE not in text and CSRF not in text
    assert "alert(1)" not in text and "Family" not in text


# --- the Telegram fetch --------------------------------------------------------


class FakeTelegram:
    instances: list = []

    def __init__(self, settings):
        FakeTelegram.instances.append(self)
        self.photo_requests = []
        self.disconnected = False

    async def connect(self):
        pass

    async def list_dialogs(self):
        return [GROUP, EVIL, PUBLIC, DM]

    async def list_folders(self):
        return [FOLDER]

    async def download_chat_photo(self, chat_id):
        self.photo_requests.append(chat_id)
        return None if chat_id == GROUP.id else b"img"

    async def disconnect(self):
        self.disconnected = True


async def test_fetch_snapshot_fetches_photos_only_for_listed_chats_then_disconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "ReadOnlyTelegram", FakeTelegram)
    settings = load_settings(env={**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "s.session")}, config_file=tmp_path / "c")

    snap = await admin.fetch_snapshot(settings)

    tg = FakeTelegram.instances[-1]
    assert tg.photo_requests == [GROUP.id, EVIL.id]
    assert snap.photos == {EVIL.id: b"img"}
    assert [c.id for c in snap.chats] == [GROUP.id, EVIL.id, PUBLIC.id, DM.id]
    assert snap.folders == [FOLDER]
    assert tg.disconnected


async def test_fetch_snapshot_disconnects_on_error(tmp_path, monkeypatch):
    class Failing(FakeTelegram):
        async def list_dialogs(self):
            raise SessionBusyError()

    monkeypatch.setattr(admin, "ReadOnlyTelegram", Failing)
    settings = load_settings(env={**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "s.session")}, config_file=tmp_path / "c")

    with pytest.raises(SessionBusyError):
        await admin.fetch_snapshot(settings)

    assert FakeTelegram.instances[-1].disconnected
