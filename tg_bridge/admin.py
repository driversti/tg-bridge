"""`tg-bridge-admin`: an owner-only page to switch private groups and channels on or off for `public` scope.

A separate process on purpose: LM Studio starts a fresh stdio server per chat message, so the page cannot live in
it. It talks to Telegram for a few seconds at start and on Refresh, then lets go of the session.

Security model: binds 127.0.0.1 only (phone access goes through `tailscale serve`); every route but /login needs
the session cookie, which only the one-time login link printed to the terminal hands out; every POST needs a CSRF
token. Chat titles are attacker-controlled text and are always HTML-escaped. Nothing secret is ever logged: the
request log of http.server contains the login token, so it is switched off.
"""

import asyncio
import hmac
import html
import logging
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from tg_bridge.config import Settings, load_settings
from tg_bridge.errors import ConfigError, SessionBusyError, TgBridgeError
from tg_bridge.folders import folder_contains
from tg_bridge.logging_setup import configure_logging
from tg_bridge.resolve import normalize
from tg_bridge.scope import needs_allowlist
from tg_bridge.source.allowlist import Allowlist, allowlist_path
from tg_bridge.source.dialog_cache import DialogCache
from tg_bridge.source.live import LiveSource
from tg_bridge.source.models import Chat, Folder
from tg_bridge.telegram.client import ReadOnlyTelegram

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
COOKIE_NAME = "tgb_admin"
MAX_BODY = 16 * 1024
BUSY_TEXT = "Telegram session is busy (LM Studio is using it); try Refresh in a moment."


@dataclass(frozen=True)
class Snapshot:
    chats: list[Chat]
    folders: list[Folder]
    photos: dict[int, bytes]  # chat id -> small JPEG, in memory only
    fetched_at: datetime


async def fetch_snapshot(settings: Settings) -> Snapshot:
    """Dialogs, folders and small photos of the chats the page lists; the Telegram connection is closed afterwards."""
    telegram = ReadOnlyTelegram(settings)
    cache = DialogCache(settings.session_path.with_name(f"{settings.session_path.stem}.dialogs.json"))
    live = LiveSource(telegram, dialog_cache=cache)
    try:
        chats = await live.list_dialogs(refresh=True)
        folders = await live.list_folders()
        photos: dict[int, bytes] = {}
        for chat in chats:
            if needs_allowlist(chat):
                data = await telegram.download_chat_photo(chat.id)
                if data:
                    photos[chat.id] = data
    finally:
        await live.close()
    log.info("admin snapshot: %d dialogs, %d folders, %d photos", len(chats), len(folders), len(photos))
    return Snapshot(chats, folders, photos, datetime.now().astimezone())


class AdminApp:
    """State and rendering; the HTTP layer only dispatches. `fetch` is synchronous so tests can inject a snapshot."""

    def __init__(
        self, fetch: Callable[[], Snapshot], allowlist: Allowlist, login_token: str, session_cookie: str, csrf: str
    ):
        self._fetch = fetch
        self.allowlist = allowlist
        self.login_token = login_token
        self.session_cookie = session_cookie
        self.csrf = csrf
        self.snapshot: Snapshot | None = None
        self.notice: str | None = None

    def refresh(self) -> None:
        """Re-fetch; on failure the previous snapshot stays and `notice` says why."""
        try:
            self.snapshot = self._fetch()
            self.notice = None
        except SessionBusyError:
            self.notice = BUSY_TEXT
        except TgBridgeError as e:  # messages of anticipated errors carry no PII
            self.notice = str(e)
        except Exception as e:  # noqa: BLE001 - the page must survive; the class name is all that is safe to show
            log.warning("admin fetch failed: %s", type(e).__name__)
            self.notice = f"Could not fetch chats ({type(e).__name__}). Check the terminal."

    def listed(self) -> dict[int, Chat]:
        return {c.id: c for c in (self.snapshot.chats if self.snapshot else []) if needs_allowlist(c)}

    # --- rendering -------------------------------------------------------------

    def render(self, q: str, folder_id: int | None, nonce: str) -> str:
        snap = self.snapshot
        allowed = self.allowlist.load()
        listed = list(self.listed().values())
        folders = snap.folders if snap else []
        chosen = next((f for f in folders if f.id == folder_id), None)
        shown = listed
        if chosen is not None:
            shown = [c for c in shown if folder_contains(chosen, c)]
        needle = normalize(q)
        if needle:
            shown = [c for c in shown if needle in normalize(c.title)]
        shown.sort(key=lambda c: (c.id not in allowed, c.title.casefold()))
        on_count = sum(c.id in allowed for c in listed)
        keep = {k: v for k, v in (("q", q), ("folder", str(chosen.id) if chosen else "")) if v}

        parts = [
            "<!doctype html><html lang=en><head><meta charset=utf-8>",
            '<meta name=viewport content="width=device-width, initial-scale=1">',
            "<title>tg-bridge allowlist</title>",
            f"<style>{_CSS}</style></head><body><main>",
            "<h1>Private chats for the cloud model</h1>",
        ]
        if self.notice:
            parts.append(f'<p class="notice">{html.escape(self.notice)}</p>')
        if snap is not None:
            parts.append(
                f'<p class="meta">{len(listed)} private chats · {on_count} on · '
                f"fetched {html.escape(snap.fetched_at.strftime('%Y-%m-%d %H:%M'))}</p>"
            )
        parts.append(self._toolbar(q, chosen, folders, keep))
        for chat in shown:
            parts.append(self._row(chat, chat.id in allowed, snap, keep))
        if snap is not None and not shown:
            parts.append('<p class="meta">No private chats match.</p>')
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
            f'<select name="folder">{"".join(options)}</select><button type="submit">Filter</button></form>'
            '<form method="post" action="/refresh">'
            f'<input type="hidden" name="csrf" value="{html.escape(self.csrf)}">{_hidden(keep)}'
            '<button type="submit">Refresh from Telegram</button></form>'
        )

    def _row(self, chat: Chat, on: bool, snap: Snapshot | None, keep: dict[str, str]) -> str:
        title = html.escape(chat.title)
        if snap is not None and chat.id in snap.photos:
            icon = f'<img class="avatar" src="/photo/{chat.id}" alt="" width=40 height=40>'
        else:
            letter = html.escape((chat.title.strip() or "?")[0].upper())
            icon = f'<div class="avatar">{letter}</div>'
        kind = "forum" if chat.is_forum else chat.kind
        tags = f'<span class="tag">{kind}</span>' + ('<span class="tag">archived</span>' if chat.is_archived else "")
        link = _tme_link(chat.id)
        link_html = (
            f'<button type="button" class="copy" data-link="{html.escape(link, quote=True)}">Copy link</button>'
            if link
            else '<span class="meta">no link</span>'
        )
        switch = (
            '<form method="post" action="/toggle">'
            f'<input type="hidden" name="csrf" value="{html.escape(self.csrf)}">'
            f'<input type="hidden" name="chat_id" value="{chat.id}">'
            f'<input type="hidden" name="on" value="{0 if on else 1}">{_hidden(keep)}'
            f'<button type="submit" class="switch {"on" if on else "off"}">{"On" if on else "Off"}</button></form>'
        )
        return (
            f'<div class="row">{icon}<div class="body"><div class="title">{title}</div>'
            f"<div>{tags} {link_html}</div></div>{switch}</div>"
        )


def _hidden(fields: dict[str, str]) -> str:
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">' for k, v in fields.items()
    )


def _tme_link(chat_id: int) -> str | None:
    """`-100N` ids (supergroups, channels) have a t.me/c/N link; basic groups have none."""
    text = str(chat_id)
    return f"https://t.me/c/{text[4:]}" if text.startswith("-100") and len(text) > 4 else None


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
    ".copy{padding:2px 8px;font-size:.8rem}.switch{min-width:64px}.switch.on{background:var(--ok);color:#fff;border-color:var(--ok)}"
)
# No user data in script: the copy button carries only the link, in a data attribute.
_JS = "document.addEventListener('click',function(e){var b=e.target.closest('[data-link]');if(b){navigator.clipboard.writeText(b.dataset.link)}})"


def make_server(app: AdminApp, port: int) -> HTTPServer:
    """Plain single-threaded HTTPServer on loopback: snapshot refresh and allowlist writes never race."""

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
            return morsel is not None and hmac.compare_digest(morsel.value, app.session_cookie)

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
            return hmac.compare_digest(form.get("csrf", ""), app.csrf)

        @staticmethod
        def _back(form: dict[str, str]) -> str:
            keep = {k: form[k] for k in ("q", "folder") if form.get(k)}
            return "/?" + urlencode(keep) if keep else "/"

        # --- routes
        def do_GET(self):  # noqa: N802
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/login":
                token = query.get("token", "")
                if hmac.compare_digest(token, app.login_token):
                    cookie = f"{COOKIE_NAME}={app.session_cookie}; HttpOnly; SameSite=Strict; Path=/"
                    return self._redirect("/", Set_Cookie=cookie)
                return self._forbidden()
            if not self._authorized():
                return self._forbidden()
            if url.path == "/":
                folder = query.get("folder", "")
                folder_id = int(folder) if folder.isascii() and folder.isdigit() else None
                nonce = secrets.token_urlsafe(16)
                page = app.render(query.get("q", ""), folder_id, nonce)
                return self._send(200, page.encode(), "text/html; charset=utf-8", nonce=nonce)
            if url.path.startswith("/photo/"):
                return self._photo(url.path.removeprefix("/photo/"))
            self._text(404, "Not found.")

        def _photo(self, raw_id: str):
            try:
                chat_id = int(raw_id)
            except ValueError:
                return self._text(404, "Not found.")
            data = app.snapshot.photos.get(chat_id) if app.snapshot else None
            if data is None or chat_id not in app.listed():
                return self._text(404, "Not found.")
            self._send(200, data, "image/jpeg")

        def do_POST(self):  # noqa: N802
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
                app.refresh()
                return self._redirect(self._back(form))
            try:
                chat_id = int(form.get("chat_id", ""))
            except ValueError:
                return self._text(400, "Bad chat id.")
            on = form.get("on")
            if on not in ("0", "1") or chat_id not in app.listed():
                return self._text(400, "Bad request.")
            app.allowlist.set(chat_id, on == "1")
            log.info("chat %d switched %s", chat_id, "on" if on == "1" else "off")
            self._redirect(self._back(form))

    return HTTPServer((HOST, port), Handler)


def main() -> None:
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge-admin: {e}", file=sys.stderr)
        sys.exit(2)

    app = AdminApp(
        lambda: asyncio.run(fetch_snapshot(settings)),
        Allowlist(allowlist_path(settings.session_path)),
        login_token=secrets.token_urlsafe(32),
        session_cookie=secrets.token_urlsafe(32),
        csrf=secrets.token_urlsafe(32),
    )
    app.refresh()
    if app.notice:
        print(f"tg-bridge-admin: {app.notice}", file=sys.stderr)
    try:
        server = make_server(app, settings.admin_port)
    except OSError as e:
        print(f"tg-bridge-admin: cannot listen on {HOST}:{settings.admin_port} ({e.strerror})", file=sys.stderr)
        sys.exit(2)
    # Outside `logging` on purpose: the token must never reach a log.
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
