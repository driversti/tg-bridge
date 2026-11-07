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
from tg_bridge.token_file import BadTokenFile, admin_token_file

LOGIN, COOKIE, CSRF = "login-token", "session-cookie", "csrf-token"

GROUP = make_chat(id=-555, kind="group", title="Family", username=None)
MINE_PRIV = make_chat(id=-1001000000011, kind="supergroup", title="My Own Club", username=None, is_creator=True, photo_id=3)
MINE_PUB = make_chat(id=-1001000000012, kind="channel", title="My Public Blog", username="myblog", is_creator=True)
SAVED = make_user_chat(make_person(id=99, first_name="Owner", username="owner_u"), title="Saved Messages", is_self=True)
EVIL = make_chat(id=-1001000000007, kind="supergroup", title="<script>alert(1)</script> Club", username=None, top_message_id=15)
CHANNEL_PRIV = make_chat(id=-1001000000009, kind="channel", title="Quiet Channel", username=None, is_archived=True, top_message_id=9)
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
    def __init__(self, tmp_path, fetch, hidden=frozenset(), token_source=None, server_kw=None):
        self.allowlist = Allowlist(tmp_path / "a.json")
        self.photo_dir = tmp_path / "photos"
        self.photo_dir.mkdir()
        self.fetches = 0
        self.now = 1000.0
        self._fetch = fetch
        self.app = AdminApp(
            self._counted, self.allowlist, LOGIN, COOKIE, CSRF, photo_dir=self.photo_dir, clock=lambda: self.now,
            hidden=hidden, token_source=token_source,
        )
        self.app.refresh()  # what main() does before it starts serving
        self.server = make_server(self.app, 0, **(server_kw or {}))
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
    assert 'data-link="https://t.me/c/1000000007/15"' in body  # the id is -100N: the link carries N and a message id
    assert "t.me/c/555" not in body
    assert "no link" in body  # the basic group
    assert 'href="https://t.me/c/1000000007/15" target="_blank" rel="noopener noreferrer"' in body
    assert ">Copy<" in body
    assert "forum" in body and "supergroup" in body
    _, body = harness.get("/?tab=channels")
    assert 'data-link="https://t.me/c/1000000009/9"' in body and "archived" in body and "channel" in body


def test_people_links_use_the_username_only_when_there_is_one(harness):
    _, body = harness.get("/?tab=people")
    assert 'data-link="https://t.me/anna_k"' in body
    row = next(r for r in body.split('<div class="row') if "Helper Bot" in r)
    assert 'data-link="tg://user?id=77"' in row and "t.me/" not in row
    _, body = harness.get("/?tab=mine")
    assert "t.me/owner_u" not in body


def test_hostile_username_gets_no_link(tmp_path):
    evil = make_user_chat(make_person(id=5, first_name="X", username='a"><script>'))
    h = Harness(tmp_path, lambda: snapshot(chats=[evil]))
    try:
        _, body = h.get("/?tab=people")
        assert 'data-link="tg://user?id=5"' in body and "t.me/" not in body and "<script>alert" not in body
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


async def test_fetch_snapshot_downloads_no_photos_and_disconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "ReadOnlyTelegram", FakeTelegram)

    snap = await admin.fetch_snapshot(fetch_settings(tmp_path))

    tg = FakeTelegram.instances[-1]
    assert tg.photo_requests == []
    assert [c.id for c in snap.chats] == [GROUP.id, EVIL.id, PUBLIC.id, DM.id, MINE_PRIV.id, MINE_PUB.id]
    assert snap.folders == [FOLDER]
    assert tg.disconnected


async def _async(value):
    return value


async def test_fetch_snapshot_disconnects_on_error(tmp_path, monkeypatch):
    class Failing(FakeTelegram):
        async def list_dialogs(self):
            raise SessionBusyError()

    monkeypatch.setattr(admin, "ReadOnlyTelegram", Failing)

    with pytest.raises(SessionBusyError):
        await admin.fetch_snapshot(fetch_settings(tmp_path))

    assert FakeTelegram.instances[-1].disconnected


# --- the background photo worker -----------------------------------------------


class FakeDownloader:
    """Stands in for one Telegram session per batch: records batches and whether sessions overlap."""

    def __init__(self, photo_dir, events=None, fail_first_with=None):
        self.photo_dir = photo_dir
        self.batches: list[list[int]] = []
        self.events = events if events is not None else []
        self.fail_first_with = fail_first_with
        self.inside = 0
        self.overlaps = 0
        self.hook = None

    def __call__(self, chats):
        self.events.append("connect")
        self.inside += 1
        if self.inside > 1:
            self.overlaps += 1
        try:
            if self.fail_first_with is not None:
                err, self.fail_first_with = self.fail_first_with, None
                raise err
            if self.hook:
                self.hook()
            self.batches.append([c.id for c in chats])
            for c in chats:
                admin._store_photo(self.photo_dir, c, b"img")
        finally:
            self.inside -= 1
            self.events.append("disconnect")


def photo_chats(n, start=1):
    return [
        make_chat(id=-1001000100000 - i, kind="supergroup", title=f"Club {i}", username=None, is_creator=True, photo_id=i)
        for i in range(start, start + n)
    ]


def make_worker(tmp_path, download, **kw):
    sleeps = []
    lock = kw.pop("lock", threading.Lock())
    worker = admin.PhotoWorker(download, tmp_path / "photos", lock, sleep=sleeps.append, **kw)
    return worker, sleeps


def test_worker_downloads_in_batches_of_25_releasing_the_session_before_each_sleep(tmp_path):
    events = []
    dl = FakeDownloader(tmp_path / "photos", events)
    worker, sleeps = make_worker(tmp_path, dl)
    chats = photo_chats(60)
    worker.set_chats(chats, autostart=False)

    worker.run_until_done()

    assert [len(b) for b in dl.batches] == [25, 25, 10]
    assert [i for b in dl.batches for i in b] == [c.id for c in chats]  # snapshot order
    assert sleeps == [2.0, 2.0]  # between batches, none after the last
    assert events == ["connect", "disconnect"] * 3
    assert worker.progress() is None


def test_worker_skips_chats_whose_photo_is_already_on_disk_and_non_listed_ones(tmp_path):
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl)
    have, need = photo_chats(2)
    admin._store_photo(tmp_path / "photos", have, b"x")
    nophoto = replace(need, id=-1001000199999, photo_id=None)
    worker.set_chats([have, need, nophoto, PUBLIC], autostart=False)

    worker.run_until_done()

    assert dl.batches == [[need.id]]


def test_busy_batch_waits_five_seconds_and_is_retried(tmp_path):
    dl = FakeDownloader(tmp_path / "photos", fail_first_with=SessionBusyError())
    worker, sleeps = make_worker(tmp_path, dl)
    chats = photo_chats(3)
    worker.set_chats(chats, autostart=False)

    worker.run_until_done()

    assert sleeps == [5.0]
    assert dl.batches == [[c.id for c in chats]]


def test_other_batch_error_skips_the_batch_and_logs_the_class_only(tmp_path, caplog):
    dl = FakeDownloader(tmp_path / "photos", fail_first_with=RuntimeError("Secret Title"))
    worker, _ = make_worker(tmp_path, dl, batch_size=2)
    chats = photo_chats(3)
    worker.set_chats(chats, autostart=False)

    worker.run_until_done()

    assert dl.batches == [[chats[2].id]]  # first batch skipped, no endless retry
    assert "RuntimeError" in caplog.text and "Secret Title" not in caplog.text


def test_progress_counts_loaded_over_total_while_running(tmp_path):
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl, batch_size=2)
    chats = photo_chats(5)
    worker.set_chats(chats[:1] + chats[1:], autostart=False)
    assert worker.progress() == (0, 5)

    worker.run_once()
    assert worker.progress() == (2, 5)
    worker.run_until_done()
    assert worker.progress() is None


def test_lock_keeps_refresh_and_a_batch_from_overlapping(tmp_path):
    lock = threading.Lock()
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl, lock=lock)
    worker.set_chats(photo_chats(3), autostart=False)
    in_batch, release = threading.Event(), threading.Event()
    in_refresh = []

    def hold():
        in_batch.set()
        assert release.wait(5)

    dl.hook = hold

    def fetch():
        in_refresh.append(dl.inside)  # no session may be open while the refresh fetches
        return snapshot()

    app = AdminApp(fetch, Allowlist(tmp_path / "a.json"), LOGIN, COOKIE, CSRF, lock=lock, photos=worker)
    t_worker = threading.Thread(target=worker.run_once)
    t_worker.start()
    assert in_batch.wait(5)
    t_refresh = threading.Thread(target=app.refresh)
    t_refresh.start()
    t_refresh.join(0.05)
    assert t_refresh.is_alive()  # waiting for the batch's session
    release.set()
    t_worker.join(5)
    t_refresh.join(5)

    assert in_refresh == [0]
    assert dl.overlaps == 0


def test_refresh_restarts_the_queue_with_the_new_snapshots_missing_photos(tmp_path):
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl)
    first, second = photo_chats(1), photo_chats(2, start=10)
    app = AdminApp(
        lambda: snapshot(chats=second), Allowlist(tmp_path / "a.json"), LOGIN, COOKIE, CSRF,
        photo_dir=tmp_path / "photos", photos=worker,
    )
    worker.set_chats(first, autostart=False)
    worker.run_until_done()
    assert worker.progress() is None

    app.refresh()  # restarts: one thread now downloads the new chats
    worker.join(5)

    assert dl.batches[-1] == [c.id for c in second]


def test_page_shows_icon_progress_only_while_the_worker_runs(tmp_path):
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl, batch_size=1)
    chats = photo_chats(2)
    h = Harness(tmp_path, lambda: snapshot(chats=chats))
    try:
        h.app.photos = worker
        worker.set_chats(chats, autostart=False)
        assert "Icons: 0/2" in h.get("/")[1]
        worker.run_once()
        assert "Icons: 1/2" in h.get("/")[1]
        worker.run_until_done()
        assert "Icons:" not in h.get("/")[1]
    finally:
        h.close()


def test_server_answers_before_any_photo_is_downloaded(tmp_path):
    dl = FakeDownloader(tmp_path / "photos")
    worker, _ = make_worker(tmp_path, dl)
    h = Harness(tmp_path, lambda: snapshot())
    try:
        h.app.photos = worker
        worker.set_chats(snapshot().chats, autostart=False)  # queued, nothing run yet
        resp, body = h.get("/")
        assert resp.status == 200 and "Family" in body
        assert dl.batches == []
    finally:
        h.close()


def test_tme_link_rules():
    link = admin._tme_link
    assert link(make_chat(id=-1001000000001, kind="channel", username="pub", top_message_id=3)) == "https://t.me/pub"
    assert link(make_chat(id=-1001000000001, kind="supergroup", username=None, top_message_id=3)) == "https://t.me/c/1000000001/3"
    assert link(make_chat(id=-1001000000001, kind="supergroup", username=None)) is None  # no top message id
    assert link(make_chat(id=-1001000000001, kind="supergroup", username="bad name!", top_message_id=3)) == "https://t.me/c/1000000001/3"
    assert link(make_chat(id=-5, kind="group", username=None, top_message_id=3)) is None
    assert link(make_user_chat(make_person(id=42, username="anna_k"))) == "https://t.me/anna_k"
    assert link(make_user_chat(make_person(id=42, username=None))) == "tg://user?id=42"
    assert link(make_user_chat(make_person(id=42, username="a b"))) == "tg://user?id=42"
    assert link(make_user_chat(make_person(id=42, username="x"), is_self=True)) is None


def test_script_has_feedback_texts_and_no_chat_data(harness):
    _, body = harness.get("/")
    script = body.split("<script", 1)[1]
    assert "Copied" in script and "Copy failed" in script
    assert "Family" not in script and "t.me" not in script


def test_person_without_username_gets_a_tg_link_row(tmp_path):
    h = Harness(tmp_path, lambda: snapshot())
    try:
        _, body = h.get("/?tab=people")
        row = next(r for r in body.split('<div class="row') if "Helper Bot" in r)
        assert 'href="tg://user?id=77"' in row
    finally:
        h.close()


# --- stable login link (C1) ---


def test_token_file_is_created_600_and_reused(tmp_path):
    session = tmp_path / "sess" / "tg.session"
    first = admin_token_file(session).load_or_create()
    path = tmp_path / "sess" / "tg.admin-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert admin_token_file(session).load_or_create() == first and len(first) >= 32


@pytest.mark.parametrize("content", ["", "  \n", "short"])
def test_malformed_token_file_refuses(tmp_path, content):
    session = tmp_path / "tg.session"
    (tmp_path / "tg.admin-token").write_text(content)
    with pytest.raises(BadTokenFile, match="malformed"):
        admin_token_file(session).load_or_create()
    assert (tmp_path / "tg.admin-token").read_text() == content


def test_cookie_and_csrf_are_stable_per_token_and_differ_between_tokens(tmp_path):
    session = tmp_path / "tg.session"
    token = admin_token_file(session).load_or_create()
    c1, s1 = admin.derive_secrets(token)
    c2, s2 = admin.derive_secrets(admin_token_file(session).load_or_create())
    assert (c1, s1) == (c2, s2) and c1 != s1 and token not in (c1, s1)

    new = admin_token_file(session).rotate()
    assert new != token and stat.S_IMODE((tmp_path / "tg.admin-token").stat().st_mode) == 0o600
    c3, s3 = admin.derive_secrets(new)
    assert c3 != c1 and s3 != s1


def test_login_cookie_lasts_a_year(harness):
    resp, _ = harness.get(f"/login?token={LOGIN}", cookie=None)
    assert "Max-Age=31536000" in resp.getheader("Set-Cookie")


def _cli_env(monkeypatch, tmp_path):
    env = {**CREDS, "TG_BRIDGE_SESSION": str(tmp_path / "tg.session")}
    monkeypatch.setattr(admin, "load_settings", lambda: load_settings(env=env, config_file=tmp_path / "none.env"))


def _boom(*a, **k):
    raise AssertionError("must not touch Telegram")


def test_link_flag_prints_without_telegram_or_server(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(admin, "fetch_snapshot", _boom)
    monkeypatch.setattr(admin, "make_server", _boom)
    admin.main(["--link"])
    err = capsys.readouterr().err
    token = admin_token_file(tmp_path / "tg.session").load_or_create()
    assert f"/login?token={token}" in err


def test_new_link_flag_replaces_the_token_and_exits(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)
    monkeypatch.setattr(admin, "fetch_snapshot", _boom)
    monkeypatch.setattr(admin, "make_server", _boom)
    old = admin_token_file(tmp_path / "tg.session").load_or_create()
    admin.main(["--new-link"])
    new = admin_token_file(tmp_path / "tg.session").load_or_create()
    assert new != old and f"token={new}" in capsys.readouterr().err


def test_malformed_token_file_exits_with_message(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)
    (tmp_path / "tg.admin-token").write_text("")
    with pytest.raises(SystemExit) as e:
        admin.main(["--link"])
    assert e.value.code == 2 and "--new-link" in capsys.readouterr().err


# --- bot chat guard (feature 11) ---


@pytest.fixture
def guarded(tmp_path):
    h = Harness(tmp_path, lambda: snapshot(), hidden=frozenset({BOT.id}))
    yield h
    h.close()


def test_admin_bot_chat_locked_no_switch(guarded):
    _, page = guarded.get("/?tab=people")
    assert "locked (receives login links)" in page
    assert f'name="chat_id" value="{BOT.id}"' not in page
    assert f'name="chat_id" value="{DM.id}"' in page  # other people keep their switch


def test_admin_post_toggle_bot_chat_400(guarded):
    assert guarded.post("/toggle", form(BOT.id, "1", tab="people"))[0].status == 400
    assert guarded.allowlist.load() == frozenset()


# --- rotating token source, Secure cookie, Host check (feature 11) ---

TOKEN_A, TOKEN_B = "a" * 40, "b" * 40


class Rotating:
    def __init__(self, token):
        self.token = token

    def __call__(self):
        return self.token


def cookie_for(token):
    return admin.derive_secrets(token)[0]


def test_token_source_rotation_logs_out_old_cookie(tmp_path):
    src = Rotating(TOKEN_A)
    h = Harness(tmp_path, lambda: snapshot(), token_source=src)
    try:
        resp, _ = h.get(f"/login?token={TOKEN_A}", cookie=None)
        assert resp.status == 303
        assert h.get("/", cookie=cookie_for(TOKEN_A))[0].status == 200
        src.token = TOKEN_B
        assert h.get("/", cookie=cookie_for(TOKEN_A))[0].status == 403
        assert h.get(f"/login?token={TOKEN_A}", cookie=None)[0].status == 403
        assert h.get(f"/login?token={TOKEN_B}", cookie=None)[0].status == 303
        assert h.get("/", cookie=cookie_for(TOKEN_B))[0].status == 200
    finally:
        h.close()


def test_token_source_none_refuses_login(tmp_path):
    src = Rotating(None)
    h = Harness(tmp_path, lambda: snapshot(), token_source=src)
    try:
        assert h.get(f"/login?token={TOKEN_A}", cookie=None)[0].status == 403
        assert h.get("/login?token=", cookie=None)[0].status == 403
        assert h.get("/", cookie="")[0].status == 403
        assert h.post("/toggle", form(GROUP.id, "1"), cookie="")[0].status == 403
    finally:
        h.close()


@pytest.mark.parametrize("secure", [True, False])
def test_secure_cookie_flag(tmp_path, secure):
    h = Harness(tmp_path, lambda: snapshot(), server_kw={"secure_cookie": secure})
    try:
        resp, _ = h.get(f"/login?token={LOGIN}", cookie=None)
        assert ("Secure" in resp.getheader("Set-Cookie").split("; ")) is secure
    finally:
        h.close()


def test_host_check(tmp_path):
    h = Harness(tmp_path, lambda: snapshot(), server_kw={"allowed_hosts": frozenset({"tg-bridge.tail4be1a2.ts.net"})})
    try:
        assert h.get("/", headers={"Host": "tg-bridge.tail4be1a2.ts.net"})[0].status == 200
        assert h.get("/", headers={"Host": "192.168.10.22:8765"})[0].status == 400
        assert h.get(f"/login?token={LOGIN}", cookie=None, headers={"Host": "evil.example"})[0].status == 400
        assert h.post("/refresh", {"csrf": CSRF}, headers={"Host": "evil.example"})[0].status == 400
    finally:
        h.close()


def test_no_allowed_hosts_means_no_host_check(harness):
    assert harness.get("/", headers={"Host": "anything.example"})[0].status == 200
