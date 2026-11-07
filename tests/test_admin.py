"""The allowlist page, over real HTTP on 127.0.0.1 with an injected snapshot (no Telegram)."""

import http.client
import stat
import threading
from dataclasses import replace
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
MINE_PRIV = make_chat(id=-1001000000011, kind="supergroup", title="My Own Club", username=None, is_creator=True, photo_id=3)
MINE_PUB = make_chat(id=-1001000000012, kind="channel", title="My Public Blog", username="myblog", is_creator=True)
SAVED = make_user_chat(make_person(id=99, first_name="Owner", username="owner_u"), title="Saved Messages", is_self=True)
EVIL = make_chat(id=-1001000000007, kind="supergroup", title="<script>alert(1)</script> Club", username=None)
CHANNEL_PRIV = make_chat(id=-1001000000009, kind="channel", title="Quiet Channel", username=None, is_archived=True)
FORUM = make_chat(id=-1001000000010, kind="supergroup", title="Forum Zone", username=None, is_forum=True)
PUBLIC = make_chat(id=-1001000000008, kind="channel", title="Public News", username="pubnews")
DM = make_user_chat(make_person(id=42, first_name="Anna", last_name="K", username="anna_k"), photo_id=8)
BOT = make_user_chat(make_person(id=77, first_name="Helper", last_name="Bot", username=None, is_bot=True))
FOLDER = make_folder(id=5, title="Work <b>", include_ids=(GROUP.id, PUBLIC.id))


def snapshot(**kw):
    fields = dict(
        chats=[GROUP, EVIL, CHANNEL_PRIV, FORUM, PUBLIC, DM, BOT, MINE_PRIV, MINE_PUB, SAVED],
        folders=[FOLDER],
        fetched_at=datetime(2026, 11, 7, 12, 0, tzinfo=UTC),
    )
    fields.update(kw)
    return Snapshot(**fields)


class Harness:
    def __init__(self, tmp_path, fetch):
        self.allowlist = Allowlist(tmp_path / "a.json")
        self.photo_dir = tmp_path / "photos"
        self.photo_dir.mkdir()
        self.fetches = 0
        self.now = 1000.0
        self._fetch = fetch
        self.app = AdminApp(
            self._counted, self.allowlist, LOGIN, COOKIE, CSRF, photo_dir=self.photo_dir, clock=lambda: self.now
        )
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


def test_groups_tab_is_the_default_and_lists_private_groups_only(harness):
    resp, body = harness.get("/")
    assert resp.status == 200
    for title in ("Family", "Forum Zone", "<script>alert(1)</script>".replace("<", "&lt;").replace(">", "&gt;"), "My Own Club"):
        assert title in body
    for title in ("Quiet Channel", "Public News", "Anna", "Helper", "Saved Messages", "My Public Blog"):
        assert title not in body
    assert "4 chats here" in body and "0 on" in body


def test_tab_counts_and_links(harness):
    _, body = harness.get("/")
    assert "Groups 4" in body and "Channels 1" in body and "People 2" in body and "Mine 3" in body
    assert 'href="/?tab=people"' in body
    _, body = harness.get("/?tab=channels&q=quiet&folder=5")
    assert 'href="/?tab=people&amp;q=quiet&amp;folder=5"' in body  # filters survive tab links


def test_channels_tab(harness):
    _, body = harness.get("/?tab=channels")
    assert "Quiet Channel" in body and "Family" not in body and "Public News" not in body


def test_people_tab_has_people_and_bots_not_saved_messages(harness):
    _, body = harness.get("/?tab=people")
    assert "Anna K" in body and "Helper Bot" in body
    assert "Saved Messages" not in body and "Family" not in body


def test_mine_tab_has_created_chats_and_saved_messages_and_overlaps(harness):
    _, body = harness.get("/?tab=mine")
    assert "My Own Club" in body and "My Public Blog" in body and "Saved Messages" in body
    assert "Family" not in body and "Anna K" not in body
    _, groups = harness.get("/")
    assert "My Own Club" in groups  # a chat can sit in two tabs


def test_own_public_chat_is_always_on_without_a_form(harness):
    _, body = harness.get("/?tab=mine")
    row = next(r for r in body.split('<div class="row') if "My Public Blog" in r)
    assert "always on" in row and "<form" not in row
    assert harness.post("/toggle", form(MINE_PUB.id, "1"))[0].status == 400


def test_unknown_tab_falls_back_to_groups(harness):
    _, body = harness.get("/?tab=nope")
    assert "Family" in body and "Anna K" not in body


def test_titles_are_escaped(harness):
    _, body = harness.get("/")
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Club" in body
    assert "Work &lt;b&gt;" in body and "Work <b>" not in body


def test_links_and_kind_labels(harness):
    _, body = harness.get("/")
    assert 'data-link="https://t.me/c/1000000007"' in body  # the id is -100N: the link carries N only
    assert "t.me/c/555" not in body
    assert "no link" in body  # the basic group
    assert "forum" in body and "supergroup" in body
    _, body = harness.get("/?tab=channels")
    assert 'data-link="https://t.me/c/1000000009"' in body and "archived" in body and "channel" in body


def test_people_links_use_the_username_only_when_there_is_one(harness):
    _, body = harness.get("/?tab=people")
    assert 'data-link="https://t.me/anna_k"' in body
    row = next(r for r in body.split('<div class="row') if "Helper Bot" in r)
    assert "no link" in row and 'data-link="' not in row
    _, body = harness.get("/?tab=mine")
    assert "t.me/owner_u" not in body


def test_hostile_username_gets_no_link(tmp_path):
    evil = make_user_chat(make_person(id=5, first_name="X", username='a"><script>'))
    h = Harness(tmp_path, lambda: snapshot(chats=[evil]))
    try:
        _, body = h.get("/?tab=people")
        assert 'data-link="' not in body and "<script>alert" not in body
    finally:
        h.close()


def test_photo_or_initial_circle(harness):
    harness.photo_dir.joinpath(f"{MINE_PRIV.id}_3.jpg").write_bytes(b"jpg")
    _, body = harness.get("/")
    assert f'src="/photo/{MINE_PRIV.id}"' in body
    assert f'src="/photo/{GROUP.id}"' not in body
    assert 'class="avatar">F<' in body  # no photo: first letter of "Family"


def test_search_filter(harness):
    _, body = harness.get("/?q=FAMILY")
    assert "Family" in body and "Quiet Channel" not in body
    _, body = harness.get("/?q=zzz")
    assert "Family" not in body


def test_folder_filter(harness):
    _, body = harness.get(f"/?folder={FOLDER.id}")
    assert "Family" in body and "Forum Zone" not in body
    _, body = harness.get("/?folder=999")  # unknown folder: ignored
    assert "Forum Zone" in body
    _, body = harness.get(f"/?tab=channels&folder={FOLDER.id}")  # the folder filter applies inside the tab
    assert "Quiet Channel" not in body


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
    assert harness.post("/toggle", form(GROUP.id, "1", tab="people", q="x"))[0].getheader("Location") == "/?tab=people&q=x"
    harness.post("/toggle", form(GROUP.id, "0"))
    assert harness.allowlist.load() == frozenset()


def test_toggle_redirect_without_filters(harness):
    assert harness.post("/toggle", form(GROUP.id, "1"))[0].getheader("Location") == "/"


def test_toggle_refuses_public_chats(harness):
    assert harness.post("/toggle", form(PUBLIC.id, "1"))[0].status == 400
    assert harness.allowlist.load() == frozenset()


@pytest.mark.parametrize("chat", [DM, BOT, SAVED])
def test_dm_toggle_on_and_off_persists(harness, chat):
    assert harness.post("/toggle", form(chat.id, "1", tab="people"))[0].status == 303
    assert harness.allowlist.load() == frozenset({chat.id})
    assert harness.post("/toggle", form(chat.id, "0"))[0].status == 303
    assert harness.allowlist.load() == frozenset()


def test_confirmation_only_on_the_on_button_of_people_bots_and_saved(harness):
    _, people = harness.get("/?tab=people")
    assert people.count('data-confirm="1"') == 2
    _, mine = harness.get("/?tab=mine")
    assert mine.count('data-confirm="1"') == 1  # Saved Messages, not the created group
    _, groups = harness.get("/")
    assert "data-confirm" not in groups.split("<script", 1)[0]


def test_switched_on_dm_is_marked_and_confirmation_is_not_asked_to_switch_off(harness):
    harness.allowlist.set(DM.id, True)
    _, body = harness.get("/?tab=people")
    assert body.count("shared with cloud") == 1
    assert 'class="row shared"' in body
    assert body.count('data-confirm="1"') == 1  # only Helper's On button is left


def test_script_has_no_chat_data_and_carries_the_nonce(harness):
    resp, body = harness.get("/?tab=people")
    script = body[body.index("<script"):]
    assert "confirm(" in script and "Anna" not in script and "anna_k" not in script
    assert f"nonce-" in resp.getheader("Content-Security-Policy")


@pytest.mark.parametrize("body", [form(123456, "1"), form("abc", "1"), form(-555, "maybe")])
def test_toggle_rejects_unknown_ids_and_values(harness, body):
    assert harness.post("/toggle", body)[0].status == 400
    assert harness.allowlist.load() == frozenset()


def test_toggle_takes_effect_in_page(harness):
    harness.post("/toggle", form(GROUP.id, "1"))
    _, body = harness.get("/")
    assert "1 on" in body


def test_photo_served_from_disk_for_listed_chats_only(harness):
    (harness.photo_dir / f"{MINE_PRIV.id}_3.jpg").write_bytes(b"\xff\xd8photo")
    (harness.photo_dir / f"{PUBLIC.id}_1.jpg").write_bytes(b"public-photo")
    resp, data = harness.request("GET", f"/photo/{MINE_PRIV.id}")
    assert resp.status == 200 and resp.getheader("Content-Type") == "image/jpeg"
    assert data == b"\xff\xd8photo"
    assert harness.request("GET", f"/photo/{GROUP.id}")[0].status == 404  # no photo
    assert harness.request("GET", "/photo/999")[0].status == 404
    assert harness.request("GET", "/photo/abc")[0].status == 404
    assert harness.request("GET", f"/photo/{PUBLIC.id}")[0].status == 404  # not listed
    assert harness.request("GET", "/photo/../a.json")[0].status == 404


def test_photo_of_an_outdated_photo_id_is_not_served(harness):
    (harness.photo_dir / f"{MINE_PRIV.id}_2.jpg").write_bytes(b"old")
    assert harness.request("GET", f"/photo/{MINE_PRIV.id}")[0].status == 404


def test_unknown_route_404_and_wrong_method(harness):
    assert harness.get("/nope")[0].status == 404
    assert harness.post("/", {})[0].status in (404, 405)


def test_refresh_requires_csrf_and_refetches(harness):
    before = harness.fetches
    harness.now += 60
    assert harness.post("/refresh", {"csrf": "bad"})[0].status == 403
    assert harness.fetches == before
    resp, _ = harness.post("/refresh", {"csrf": CSRF, "q": "x", "tab": "people"})
    assert resp.status == 303 and resp.getheader("Location") == "/?tab=people&q=x"
    assert harness.fetches == before + 1


def test_refresh_within_ten_seconds_does_not_fetch_again(harness):
    before = harness.fetches
    harness.now += 9
    resp, _ = harness.post("/refresh", {"csrf": CSRF, "tab": "mine"})
    assert resp.status == 303 and resp.getheader("Location") == "/?tab=mine"
    assert harness.fetches == before
    harness.now += 2
    harness.post("/refresh", {"csrf": CSRF})
    assert harness.fetches == before + 1


def test_refresh_button_locks_itself_in_the_script(harness):
    _, body = harness.get("/")
    assert 'data-refresh="1"' in body and "disabled=true" in body


def test_refresh_busy_keeps_old_snapshot_and_says_so(tmp_path):
    state = {"busy": False}

    def fetch():
        if state["busy"]:
            raise SessionBusyError()
        return snapshot()

    h = Harness(tmp_path, fetch)
    try:
        state["busy"] = True
        h.now += 60
        assert h.post("/refresh", {"csrf": CSRF})[0].status == 303
        _, body = h.get("/")
        assert "Telegram session is busy (LM Studio is using it); try Refresh in a moment." in body
        assert "Family" in body  # the previous snapshot is still there
        state["busy"] = False
        h.now += 60
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
        return [GROUP, EVIL, PUBLIC, DM, MINE_PRIV, MINE_PUB]

    async def list_folders(self):
        return [FOLDER]

    async def download_chat_photo(self, chat_id):
        self.photo_requests.append(chat_id)
        return b"img"

    async def disconnect(self):
        self.disconnected = True


def fetch_settings(tmp_path):
    return load_settings(env={**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "s.session")}, config_file=tmp_path / "c")


async def test_fetch_snapshot_downloads_photos_only_for_listed_chats_with_a_photo_then_disconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "ReadOnlyTelegram", FakeTelegram)
    photos = tmp_path / "photos"

    snap = await admin.fetch_snapshot(fetch_settings(tmp_path), photos)

    tg = FakeTelegram.instances[-1]
    # DM (listed, photo 8) and MINE_PRIV (photo 3); GROUP/EVIL have no photo_id, PUBLIC is not listed.
    assert sorted(tg.photo_requests) == sorted([DM.id, MINE_PRIV.id])
    assert (photos / f"{DM.id}_8.jpg").read_bytes() == b"img"
    assert stat.S_IMODE(photos.stat().st_mode) == 0o700
    assert stat.S_IMODE((photos / f"{DM.id}_8.jpg").stat().st_mode) == 0o600
    assert [c.id for c in snap.chats] == [GROUP.id, EVIL.id, PUBLIC.id, DM.id, MINE_PRIV.id, MINE_PUB.id]
    assert snap.folders == [FOLDER]
    assert tg.disconnected


async def test_second_fetch_downloads_nothing_until_the_photo_id_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "ReadOnlyTelegram", FakeTelegram)
    photos = tmp_path / "photos"
    settings = fetch_settings(tmp_path)
    await admin.fetch_snapshot(settings, photos)

    await admin.fetch_snapshot(settings, photos)
    assert FakeTelegram.instances[-1].photo_requests == []

    changed = replace(DM, photo_id=9)
    monkeypatch.setattr(FakeTelegram, "list_dialogs", lambda self: _async([changed]))
    await admin.fetch_snapshot(settings, photos)
    assert FakeTelegram.instances[-1].photo_requests == [DM.id]
    assert sorted(p.name for p in photos.iterdir() if p.name.startswith(f"{DM.id}_")) == [f"{DM.id}_9.jpg"]


async def _async(value):
    return value


async def test_fetch_snapshot_disconnects_on_error(tmp_path, monkeypatch):
    class Failing(FakeTelegram):
        async def list_dialogs(self):
            raise SessionBusyError()

    monkeypatch.setattr(admin, "ReadOnlyTelegram", Failing)

    with pytest.raises(SessionBusyError):
        await admin.fetch_snapshot(fetch_settings(tmp_path), tmp_path / "photos")

    assert FakeTelegram.instances[-1].disconnected
