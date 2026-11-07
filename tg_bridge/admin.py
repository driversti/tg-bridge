"""`tg-bridge-admin`: an owner-only page to switch private chats (groups, channels, people, bots, Saved Messages) on or off
for `public` scope.

A separate process on purpose: LM Studio starts a fresh stdio server per chat message, so the page cannot live in
it. It talks to Telegram for a few seconds at start and on Refresh, then lets go of the session.

Security model: binds 127.0.0.1 only (phone access goes through `tailscale serve`); every route but /login needs
the session cookie, which only the login link hands out (stable: the token lives in a 600 file next to the session); every POST needs a CSRF
token. Chat titles are attacker-controlled text and are always HTML-escaped. Nothing secret is ever logged: the
request log of http.server contains the login token, so it is switched off.
"""

import argparse
import asyncio
import hashlib
import hmac
import html
import logging
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from tg_bridge.config import Settings, load_settings
from tg_bridge.errors import ConfigError, SessionBusyError, TgBridgeError
from tg_bridge.folders import folder_contains
from tg_bridge.logging_setup import configure_logging
from tg_bridge.resolve import normalize
from tg_bridge.scope import is_public, needs_allowlist
from tg_bridge.source.allowlist import Allowlist, allowlist_path
from tg_bridge.source.dialog_cache import DialogCache
from tg_bridge.source.live import LiveSource
from tg_bridge.source.models import Chat, Folder
from tg_bridge.telegram.client import ReadOnlyTelegram
from tg_bridge.token_file import BadTokenFile, admin_token_file

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
COOKIE_NAME = "tgb_admin"
COOKIE_MAX_AGE = 365 * 24 * 3600
IN_PROCESS_TIMEOUT = 120  # seconds a page request waits for the MCP loop
MAX_BODY = 16 * 1024
BUSY_TEXT = "Telegram session is busy (LM Studio is using it); try Refresh in a moment."
REFRESH_COOLDOWN = 10.0  # seconds; a double click must not fetch twice
PHOTO_BATCH = 25
PHOTO_PAUSE = 2.0  # seconds between batches: the session is free for LM Studio meanwhile
PHOTO_BUSY_WAIT = 5.0
TABS = ("groups", "channels", "people", "mine")
DEFAULT_TAB = "groups"
TAB_LABELS = {"groups": "Groups", "channels": "Channels", "people": "People", "mine": "Mine"}
_USERNAME = re.compile(r"[A-Za-z0-9_]{1,64}")


def photo_dir_for(session_path: Path) -> Path:
    return session_path.with_name(f"{session_path.stem}.photos")


def derive_secrets(token: str) -> tuple[str, str]:
    """(session cookie, CSRF token): stable across restarts, different for every login token."""
    key = token.encode("ascii")
    return (
        hmac.new(key, b"cookie", hashlib.sha256).hexdigest(),
        hmac.new(key, b"csrf", hashlib.sha256).hexdigest(),
    )


def listed_on_page(chat: Chat) -> bool:
    """Everything private, plus the owner's own public chats (shown as "always on" in Mine)."""
    return needs_allowlist(chat) or (is_public(chat) and chat.is_creator)


def in_tab(chat: Chat, tab: str) -> bool:
    if tab == "mine":
        return listed_on_page(chat) and (chat.is_self or chat.is_creator)
    if tab == "people":
        return chat.kind in ("user", "bot") and not chat.is_self
    if not needs_allowlist(chat):
        return False
    if tab == "channels":
        return chat.kind == "channel"
    return chat.kind in ("group", "supergroup")


@dataclass(frozen=True)
class Snapshot:
    chats: list[Chat]
    folders: list[Folder]
    fetched_at: datetime


def _photo_file(photo_dir: Path, chat: Chat) -> Path:
    return photo_dir / f"{chat.id}_{chat.photo_id}.jpg"


def _store_photo(photo_dir: Path, chat: Chat, data: bytes) -> None:
    """Dir 700, file 600, atomic; the chat's older photos go away."""
    photo_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=photo_dir, prefix=".photo.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        target = _photo_file(photo_dir, chat)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    prefix = f"{chat.id}_"
    for old in photo_dir.iterdir():
        if old.name.startswith(prefix) and old.suffix == ".jpg" and old != target:
            old.unlink(missing_ok=True)


async def fetch_snapshot(settings: Settings) -> Snapshot:
    """Dialogs and folders only (photos come later, from the PhotoWorker). The Telegram connection is closed
    afterwards."""
    telegram = ReadOnlyTelegram(settings)
    cache = DialogCache(settings.session_path.with_name(f"{settings.session_path.stem}.dialogs.json"))
    live = LiveSource(telegram, dialog_cache=cache)
    try:
        return await snapshot_from(live)
    finally:
        await live.close()


async def snapshot_from(live: LiveSource) -> Snapshot:
    """Dialogs and folders from a source somebody else owns: nothing is created or closed here."""
    chats = await live.list_dialogs(refresh=True)
    folders = await live.list_folders()
    log.info("admin snapshot: %d dialogs, %d folders", len(chats), len(folders))
    return Snapshot(chats, folders, datetime.now().astimezone())


async def download_photos(settings: Settings, photo_dir: Path, chats: list[Chat]) -> None:
    """One short session for one batch. A busy session propagates (the worker retries); any other failure skips
    just that chat."""
    telegram = ReadOnlyTelegram(settings)
    await telegram.connect()
    try:
        for chat in chats:
            try:
                data = await telegram.download_chat_photo(chat.id)
                if data:
                    _store_photo(photo_dir, chat, data)
            except SessionBusyError:
                raise
            except Exception as e:  # noqa: BLE001 - a missing icon is not worth stopping for
                log.warning("admin photo skipped: %s", type(e).__name__)
    finally:
        await telegram.disconnect()


async def download_photos_with(live: LiveSource, photo_dir: Path, chats: list[Chat]) -> None:
    """Like `download_photos`, but on a connected source shared with the MCP tools."""
    for chat in chats:
        try:
            data = await live.download_chat_photo(chat.id)
            if data:
                _store_photo(photo_dir, chat, data)
        except SessionBusyError:
            raise
        except Exception as e:  # noqa: BLE001 - a missing icon is not worth stopping for
            log.warning("admin photo skipped: %s", type(e).__name__)


class PhotoWorker:
    """Downloads missing icons of listed chats in the background, a batch per short Telegram session.

    `lock` is shared with AdminApp: it guards every Telegram use of this process, so a Refresh and a batch never
    overlap. The lock is released before each pause, so the session is never held while idle. `download` is
    synchronous (tests inject a fake; main() wraps `download_photos` in asyncio.run).
    """

    def __init__(
        self,
        download: Callable[[list[Chat]], None],
        photo_dir: Path,
        lock: AbstractContextManager,
        sleep: Callable[[float], None] = time.sleep,
        batch_size: int = PHOTO_BATCH,
        pause: float = PHOTO_PAUSE,
        busy_wait: float = PHOTO_BUSY_WAIT,
    ):
        self._download = download
        self._photo_dir = photo_dir
        self._lock = lock
        self._sleep = sleep
        self._batch_size = batch_size
        self._pause = pause
        self._busy_wait = busy_wait
        self._state = threading.Lock()  # guards the fields below; never held across Telegram or sleep
        self._chats: list[Chat] = []
        self._pending: list[Chat] = []
        self._running = False
        self._thread: threading.Thread | None = None

    def set_chats(self, chats: list[Chat], autostart: bool = True) -> None:
        """(Re)start the queue from a snapshot's chats; `autostart=False` is for tests that drive `run_once`."""
        wanted = [c for c in chats if listed_on_page(c) and c.photo_id is not None]
        missing = [c for c in wanted if not _photo_file(self._photo_dir, c).exists()]
        with self._state:
            self._chats = wanted
            self._pending = missing
            self._running = bool(missing)
            if self._running and autostart and (self._thread is None or not self._thread.is_alive()):
                self._thread = threading.Thread(target=self.run_until_done, name="photo-worker", daemon=True)
                self._thread.start()

    def progress(self) -> tuple[int, int] | None:
        """(loaded, total) while the queue is not empty; None when there is nothing left to load."""
        with self._state:
            if not self._running:
                return None
            chats = list(self._chats)
        loaded = sum(_photo_file(self._photo_dir, c).exists() for c in chats)
        return loaded, len(chats)

    def run_once(self) -> float | None:
        """One batch. Returns the seconds to wait before the next one, or None when the queue is done."""
        with self._state:
            batch = self._pending[: self._batch_size]
            if not batch:
                self._running = False
                return None
        delay = self._pause
        try:
            with self._lock:
                self._download(batch)
        except SessionBusyError:
            return self._busy_wait  # the batch stays queued
        except Exception as e:  # noqa: BLE001 - class name only: the message may carry chat data
            log.warning("admin photo batch skipped: %s", type(e).__name__)
        done = {c.id for c in batch}
        with self._state:
            self._pending = [c for c in self._pending if c.id not in done]
            if not self._pending:
                self._running = False
                return None
        return delay

    def run_until_done(self) -> None:
        while (delay := self.run_once()) is not None:
            self._sleep(delay)

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)


class AdminApp:
    """State and rendering; the HTTP layer only dispatches. `fetch` is synchronous so tests can inject a snapshot."""

    def __init__(
        self,
        fetch: Callable[[], Snapshot],
        allowlist: Allowlist,
        login_token: str = "",
        session_cookie: str = "",
        csrf: str = "",
        photo_dir: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock: AbstractContextManager | None = None,
        photos: "PhotoWorker | None" = None,
        hidden: frozenset[int] = frozenset(),
        token_source: Callable[[], str | None] | None = None,
    ):
        self.hidden = hidden  # chats that can never be switched on (e.g. the bot chat that gets login links)
        self._fetch = fetch
        self.allowlist = allowlist
        self._fixed = (login_token, session_cookie, csrf)
        # Network mode: the token file is the only truth and is consulted on every request, so a rotation
        # logs out the old cookie at once. None means "no valid token": every route refuses.
        self._token_source = token_source
        self._derived: tuple[str, str, str] | None = None
        self.snapshot: Snapshot | None = None
        self.notice: str | None = None
        self.photo_dir = photo_dir
        self._clock = clock
        self._last_ok: float | None = None
        self.lock = lock if lock is not None else threading.Lock()  # every Telegram use takes it
        self.photos = photos

    def _secrets(self) -> tuple[str, str, str] | None:
        """(login token, session cookie, csrf), or None when a token source has no valid token."""
        if self._token_source is None:
            return self._fixed
        token = self._token_source()
        if token is None:
            return None
        if self._derived is None or self._derived[0] != token:
            self._derived = (token, *derive_secrets(token))
        return self._derived

    @property
    def login_token(self) -> str | None:
        found = self._secrets()
        return None if found is None else found[0]

    @property
    def session_cookie(self) -> str | None:
        found = self._secrets()
        return None if found is None else found[1]

    @property
    def csrf(self) -> str | None:
        found = self._secrets()
        return None if found is None else found[2]

    def refresh_unless_recent(self) -> None:
        """The Refresh button: a second press right after a successful fetch is ignored."""
        if self._last_ok is not None and self._clock() - self._last_ok < REFRESH_COOLDOWN:
            return
        self.refresh()

    def refresh(self) -> None:
        """Re-fetch; on failure the previous snapshot stays and `notice` says why."""
        try:
            with self.lock:  # waits for a running photo batch
                snapshot = self._fetch()
            self.snapshot = snapshot
            self.notice = None
            self._last_ok = self._clock()
            if self.photos is not None:
                self.photos.set_chats(snapshot.chats)
        except SessionBusyError:
            self.notice = BUSY_TEXT
        except TgBridgeError as e:  # messages of anticipated errors carry no PII
            self.notice = str(e)
        except Exception as e:  # noqa: BLE001 - the page must survive; the class name is all that is safe to show
            log.warning("admin fetch failed: %s", type(e).__name__)
            self.notice = f"Could not fetch chats ({type(e).__name__}). Check the terminal."

    def listed(self) -> dict[int, Chat]:
        return {c.id: c for c in (self.snapshot.chats if self.snapshot else []) if listed_on_page(c)}

    def switchable(self) -> dict[int, Chat]:
        return {c.id: c for c in (self.snapshot.chats if self.snapshot else []) if needs_allowlist(c) and c.id not in self.hidden}

    def photo_bytes(self, chat_id: int) -> bytes | None:
        """Only for chats the page lists, and only the file that matches the chat's current photo id."""
        chat = self.listed().get(chat_id)
        if chat is None or chat.photo_id is None or self.photo_dir is None:
            return None
        try:
            return _photo_file(self.photo_dir, chat).read_bytes()
        except OSError:
            return None

    # --- rendering -------------------------------------------------------------

    def render(self, q: str, folder_id: int | None, nonce: str, tab: str = DEFAULT_TAB) -> str:
        tab = tab if tab in TABS else DEFAULT_TAB
        snap = self.snapshot
        allowed = self.allowlist.load()
        listed = list(self.listed().values())
        counts = {t: sum(in_tab(c, t) for c in listed) for t in TABS}
        folders = snap.folders if snap else []
        chosen = next((f for f in folders if f.id == folder_id), None)
        in_current = [c for c in listed if in_tab(c, tab)]
        shown = in_current
        if chosen is not None:
            shown = [c for c in shown if folder_contains(chosen, c)]
        needle = normalize(q)
        if needle:
            shown = [c for c in shown if needle in normalize(c.title)]
        shown.sort(key=lambda c: (not (c.id in allowed or is_public(c)), c.title.casefold()))
        on_count = sum(c.id in allowed for c in in_current)
        filters = {k: v for k, v in (("q", q), ("folder", str(chosen.id) if chosen else "")) if v}
        keep = {"tab": tab, **filters}

        parts = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            "<title>tg-bridge allowlist</title>",
            f"<style>{_CSS}</style></head><body><main>",
            "<h1>Private chats for the cloud model</h1>",
        ]
        if self.notice:
            parts.append(f'<p class="notice">{html.escape(self.notice)}</p>')
        parts.append(_tabs_nav(tab, counts, filters))
        if snap is not None:
            progress = self.photos.progress() if self.photos is not None else None
            icons = f" · Icons: {progress[0]}/{progress[1]}" if progress else ""
            parts.append(
                f'<p class="meta">{len(in_current)} chats here · {on_count} on · '
                f"fetched {html.escape(snap.fetched_at.strftime('%Y-%m-%d %H:%M'))}{icons}</p>"
            )
        parts.append(self._toolbar(q, chosen, folders, keep))
        for chat in shown:
            parts.append(self._row(chat, chat.id in allowed, keep))
        if snap is not None and not shown:
            parts.append('<p class="meta">No chats match.</p>')
        parts.append(f'<script nonce="{nonce}">{_JS}</script></main></body></html>')
        return "".join(parts)

    def _toolbar(self, q: str, chosen: Folder | None, folders: list[Folder], keep: dict[str, str]) -> str:
        options = ['<option value="">All folders</option>']
        for f in folders:
            label = f"{f.title} {f.emoticon}" if f.emoticon else f.title
            sel = " selected" if chosen is not None and f.id == chosen.id else ""
            options.append(f'<option value="{f.id}"{sel}>{html.escape(label)}</option>')
        return (
            '<form class="bar" method="get" action="/">'
            f'<input type="search" name="q" placeholder="Search by title" value="{html.escape(q, quote=True)}">'
            f'<select name="folder">{"".join(options)}</select>'
            f'<input type="hidden" name="tab" value="{html.escape(keep["tab"], quote=True)}">'
            '<button type="submit">Filter</button></form>'
            '<form method="post" action="/refresh" data-refresh="1">'
            f'<input type="hidden" name="csrf" value="{html.escape(self.csrf or "")}">{_hidden(keep)}'
            '<button type="submit">Refresh from Telegram</button></form>'
        )

    def _row(self, chat: Chat, on: bool, keep: dict[str, str]) -> str:
        title = html.escape(chat.title)
        if self.photo_bytes(chat.id) is not None:
            icon = f'<img class="avatar" src="/photo/{chat.id}" alt="" width=40 height=40>'
        else:
            letter = html.escape((chat.title.strip() or "?")[0].upper())
            icon = f'<div class="avatar">{letter}</div>'
        kind = "forum" if chat.is_forum else chat.kind
        always = is_public(chat)
        shared = on and chat.kind in ("user", "bot")
        tags = f'<span class="tag">{kind}</span>' + ('<span class="tag">archived</span>' if chat.is_archived else "")
        if shared:
            tags += '<span class="tag shared">shared with cloud</span>'
        link = _tme_link(chat)
        link_html = (
            f'<a class="open" href="{html.escape(link, quote=True)}" target="_blank" rel="noopener noreferrer">Open</a> '
            f'<button type="button" class="copy" data-link="{html.escape(link, quote=True)}">Copy</button>'
            if link
            else '<span class="meta">no link</span>'
        )
        if chat.id in self.hidden:
            switch = '<span class="tag">locked (receives login links)</span>'
        elif always:
            switch = '<span class="tag">always on</span>'
        else:
            confirm = ' data-confirm="1"' if chat.kind in ("user", "bot") and not on else ""
            switch = (
                '<form method="post" action="/toggle">'
                f'<input type="hidden" name="csrf" value="{html.escape(self.csrf or "")}">'
                f'<input type="hidden" name="chat_id" value="{chat.id}">'
                f'<input type="hidden" name="on" value="{0 if on else 1}">{_hidden(keep)}'
                f'<button type="submit"{confirm} class="switch {"on" if on else "off"}">{"On" if on else "Off"}</button></form>'
            )
        cls = "row shared" if shared else "row"
        return (
            f'<div class="{cls}">{icon}<div class="body"><div class="title">{title}</div>'
            f"<div>{tags} {link_html}</div></div>{switch}</div>"
        )


def _tabs_nav(current: str, counts: dict[str, int], filters: dict[str, str]) -> str:
    items = []
    for t in TABS:
        label = f"{TAB_LABELS[t]} {counts[t]}"
        if t == current:
            items.append(f'<strong aria-current="page">{label}</strong>')
        else:
            href = html.escape("/?" + urlencode({"tab": t, **filters}), quote=True)
            items.append(f'<a href="{href}">{label}</a>')
    return '<nav class="tabs">' + " · ".join(items) + "</nav>"


def _hidden(fields: dict[str, str]) -> str:
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">' for k, v in fields.items()
    )


def _tme_link(chat: Chat) -> str | None:
    """Public chats and people by username; private `-100N` chats need a message id, or t.me/c/N opens Telegram's home."""
    if chat.is_self or chat.kind == "group":
        return None
    valid_name = bool(chat.username and _USERNAME.fullmatch(chat.username))
    if chat.kind in ("user", "bot"):
        if valid_name:
            return f"https://t.me/{chat.username}"
        return f"tg://user?id={chat.id}" if chat.id > 0 else None
    if valid_name:
        return f"https://t.me/{chat.username}"
    text = str(chat.id)
    if text.startswith("-100") and len(text) > 4 and chat.top_message_id:
        return f"https://t.me/c/{text[4:]}/{chat.top_message_id}"
    return None


_CSS = (
    ":root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#e2e2e6;--acc:#2a7de1;--ok:#1f9d55}"
    "@media (prefers-color-scheme:dark){:root{--bg:#161618;--fg:#ececf0;--mut:#9a9aa2;--line:#2c2c30;--acc:#5aa2ff;--ok:#3ec97c}}"
    "body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 system-ui,sans-serif}"
    "main{max-width:720px;margin:0 auto;padding:16px}h1{font-size:1.25rem}"
    ".meta{color:var(--mut);font-size:.9rem}.notice{border:1px solid var(--acc);padding:8px 12px;border-radius:8px}"
    ".bar{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}.bar input,.bar select{flex:1 1 140px;min-width:0}"
    "input,select,button{font:inherit;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}"
    "button{cursor:pointer}.row{display:flex;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}"
    ".avatar{width:40px;height:40px;border-radius:50%;object-fit:cover;flex:none;background:var(--line);display:flex;"
    "align-items:center;justify-content:center;font-weight:600}.body{flex:1;min-width:0}"
    ".title{overflow-wrap:anywhere;font-weight:600}.tag{display:inline-block;margin-right:6px;padding:0 8px;"
    "border-radius:999px;border:1px solid var(--line);font-size:.8rem;color:var(--mut)}"
    ".tabs{display:flex;flex-wrap:wrap;gap:4px 8px;margin:8px 0}.tabs a{color:var(--acc)}"
    ".row.shared{border-left:4px solid var(--acc);padding-left:8px}.tag.shared{color:var(--acc);border-color:var(--acc)}"
    ".copy{padding:2px 8px;font-size:.8rem}.switch{min-width:64px}.switch.on{background:var(--ok);color:#fff;border-color:var(--ok)}"
)
# No chat data in script: the copy button carries only the link, in a data attribute; the texts are generic.
_JS = (
    "document.addEventListener('click',function(e){var b=e.target.closest('[data-link]');"
    "if(!b||b.tagName==='A')return;var t=b.dataset.t||(b.dataset.t=b.textContent);"
    "var done=function(m){b.textContent=m;clearTimeout(b._h);b._h=setTimeout(function(){b.textContent=t},1500)};"
    "try{navigator.clipboard.writeText(b.dataset.link).then(function(){done('Copied \\u2713')},"
    "function(){done('Copy failed')})}catch(x){done('Copy failed')}});"
    "document.addEventListener('submit',function(e){var f=e.target;"
    "if(f.querySelector('[data-confirm]')&&!confirm('The cloud model will be able to read this whole conversation. "
    "Switch it on?')){e.preventDefault();return}"
    "if(f.dataset.refresh){var b=f.querySelector('button');b.disabled=true;b.textContent='Refreshing\\u2026'}})"
)


def make_server(
    app: AdminApp, port: int, *, secure_cookie: bool = False, allowed_hosts: frozenset[str] | None = None
) -> HTTPServer:
    """Plain single-threaded HTTPServer on loopback: snapshot refresh and allowlist writes never race.

    `secure_cookie` adds `Secure` (the page is reached over HTTPS via Tailscale); `allowed_hosts`, when given,
    answers any other `Host` with 400."""
    allowed = None if allowed_hosts is None else frozenset(h.lower() for h in allowed_hosts)

    class Handler(BaseHTTPRequestHandler):
        server_version = "tg-bridge-admin"

        def log_message(self, format, *args):  # noqa: A002 - the request line holds the login token
            pass

        # --- helpers
        def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8", **headers: str):
            nonce = headers.pop("nonce", "")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
                f"script-src 'nonce-{nonce}'; form-action 'self'; frame-ancestors 'none'",
            )
            for k, v in headers.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()
            self.wfile.write(body)

        def _text(self, status: int, text: str):
            self._send(status, text.encode())

        def _redirect(self, location: str, **headers: str):
            self._send(303, b"", Location=location, **headers)

        def _authorized(self) -> bool:
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            morsel = jar.get(COOKIE_NAME)
            expected = app.session_cookie
            return morsel is not None and expected is not None and hmac.compare_digest(morsel.value, expected)

        def _forbidden(self):
            self._text(403, "Open the login link printed in the terminal.")

        def _form(self) -> dict[str, str] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if not 0 <= length <= MAX_BODY:
                return None
            raw = self.rfile.read(length).decode("utf-8", "replace")
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _csrf_ok(self, form: dict[str, str]) -> bool:
            expected = app.csrf
            return expected is not None and hmac.compare_digest(form.get("csrf", ""), expected)

        def _host_ok(self) -> bool:
            if allowed is None:
                return True
            if self.headers.get("Host", "").lower() in allowed:
                return True
            self._text(400, "Unknown host.")
            return False

        @staticmethod
        def _back(form: dict[str, str]) -> str:
            keep = {k: form[k] for k in ("tab", "q", "folder") if form.get(k)}
            return "/?" + urlencode(keep) if keep else "/"

        # --- routes
        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/login":
                token = query.get("token", "")
                expected = app.login_token
                if expected is not None and hmac.compare_digest(token, expected):
                    cookie = f"{COOKIE_NAME}={app.session_cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age={COOKIE_MAX_AGE}"
                    if secure_cookie:
                        cookie += "; Secure"
                    return self._redirect("/", Set_Cookie=cookie)
                return self._forbidden()
            if not self._authorized():
                return self._forbidden()
            if url.path == "/":
                folder = query.get("folder", "")
                folder_id = int(folder) if folder.isascii() and folder.isdigit() else None
                nonce = secrets.token_urlsafe(16)
                page = app.render(query.get("q", ""), folder_id, nonce, query.get("tab", DEFAULT_TAB))
                return self._send(200, page.encode(), "text/html; charset=utf-8", nonce=nonce)
            if url.path.startswith("/photo/"):
                return self._photo(url.path.removeprefix("/photo/"))
            self._text(404, "Not found.")

        def _photo(self, raw_id: str):
            try:
                chat_id = int(raw_id)
            except ValueError:
                return self._text(404, "Not found.")
            data = app.photo_bytes(chat_id)
            if data is None:
                return self._text(404, "Not found.")
            self._send(200, data, "image/jpeg")

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return
            path = urlsplit(self.path).path
            if not self._authorized():
                return self._forbidden()
            if path not in ("/toggle", "/refresh"):
                return self._text(404, "Not found.")
            form = self._form()
            if form is None:
                return self._text(400, "Bad request.")
            if not self._csrf_ok(form):
                return self._text(403, "Bad CSRF token: reload the page.")
            if path == "/refresh":
                app.refresh_unless_recent()
                return self._redirect(self._back(form))
            try:
                chat_id = int(form.get("chat_id", ""))
            except ValueError:
                return self._text(400, "Bad chat id.")
            on = form.get("on")
            if on not in ("0", "1") or chat_id not in app.switchable():
                return self._text(400, "Bad request.")
            app.allowlist.set(chat_id, on == "1")
            log.info("chat %d switched %s", chat_id, "on" if on == "1" else "off")
            self._redirect(self._back(form))

    return HTTPServer((HOST, port), Handler)


def start_admin_in_thread(
    settings: Settings, live: LiveSource, loop: asyncio.AbstractEventLoop, hidden: frozenset[int]
) -> HTTPServer:
    """The page inside the network process: the stdlib server runs in its own thread and hands every Telegram
    call to the main loop, so there is one client for the page and the MCP tools. Returns the bound server;
    the caller `shutdown()`s it. The first refresh runs in the server thread, so startup never waits for it."""

    def on_loop(coro):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=IN_PROCESS_TIMEOUT)

    photo_dir = photo_dir_for(settings.session_path)
    lock = threading.Lock()
    worker = PhotoWorker(lambda chats: on_loop(download_photos_with(live, photo_dir, chats)), photo_dir, lock)
    app = AdminApp(
        lambda: on_loop(snapshot_from(live)),
        Allowlist(allowlist_path(settings.session_path)),
        photo_dir=photo_dir,
        lock=lock,
        photos=worker,
        hidden=hidden,
        token_source=admin_token_file(settings.session_path).read,
    )
    allowed = {f"{HOST}:{settings.admin_port}", f"localhost:{settings.admin_port}"}
    secure = False
    if settings.admin_url:
        allowed.add(urlsplit(settings.admin_url).netloc)
        secure = settings.admin_url.startswith("https://")
    server = make_server(app, settings.admin_port, secure_cookie=secure, allowed_hosts=frozenset(allowed))

    def run() -> None:
        app.refresh()  # also queues the missing icons
        server.serve_forever()

    threading.Thread(target=run, name="admin-page", daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-bridge-admin")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--link", action="store_true", help="print the login link and exit")
    group.add_argument("--new-link", action="store_true", help="replace the login token (old links and cookies stop working), print the link and exit")
    args = parser.parse_args(argv)

    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge-admin: {e}", file=sys.stderr)
        sys.exit(2)

    try:
        tokens = admin_token_file(settings.session_path)
        token = tokens.rotate() if args.new_link else tokens.load_or_create()
    except BadTokenFile as e:
        print(f"tg-bridge-admin: {e}; run with --new-link to replace it", file=sys.stderr)
        sys.exit(2)
    # Outside `logging` on purpose: the token must never reach a log.
    if args.link or args.new_link:
        print(f"Open http://{HOST}:{settings.admin_port}/login?token={token}", file=sys.stderr)
        return
    cookie, csrf = derive_secrets(token)

    photo_dir = photo_dir_for(settings.session_path)
    lock = threading.Lock()
    worker = PhotoWorker(lambda chats: asyncio.run(download_photos(settings, photo_dir, chats)), photo_dir, lock)
    app = AdminApp(
        lambda: asyncio.run(fetch_snapshot(settings)),
        Allowlist(allowlist_path(settings.session_path)),
        login_token=token,
        session_cookie=cookie,
        csrf=csrf,
        photo_dir=photo_dir,
        lock=lock,
        photos=worker,
    )
    app.refresh()  # also queues the missing icons; the worker starts at once
    if app.notice:
        print(f"tg-bridge-admin: {app.notice}", file=sys.stderr)
    try:
        server = make_server(app, settings.admin_port)
    except OSError as e:
        print(f"tg-bridge-admin: cannot listen on {HOST}:{settings.admin_port} ({e.strerror})", file=sys.stderr)
        sys.exit(2)
    port = server.server_address[1]
    print(f"Open http://{HOST}:{port}/login?token={app.login_token}", file=sys.stderr)
    print(
        f"From the phone: run `tailscale serve --bg {port}`, then open the same path on your tailnet name.",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
