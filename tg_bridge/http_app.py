"""Streamable-HTTP MCP app behind a bearer token (network mode, feature 11)."""

import hmac
import logging
import re
from collections.abc import Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

from tg_bridge.config import Settings

log = logging.getLogger(__name__)

_BEARER = re.compile(rb"^[Bb][Ee][Aa][Rr][Ee][Rr] +(\S+) *$")
_BODY = b'{"error":"unauthorized"}'


class BearerAuth:
    """Pure ASGI middleware: exactly one `Authorization: Bearer <token>` header, else 401.

    The token comes from a callable so a rotation applies without a restart; `None` refuses everything.
    """

    def __init__(self, app: ASGIApp, token: Callable[[], str | None]):
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self._app(scope, receive, send)
            return
        # Never log the header value or the token.
        log.warning("mcp auth refused")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_BODY)).encode()),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _BODY})

    def _authorized(self, scope: Scope) -> bool:
        expected = self._token()
        if expected is None:
            return False
        values = [v for k, v in scope["headers"] if k.lower() == b"authorization"]
        if len(values) != 1:
            return False
        match = _BEARER.match(values[0])
        return match is not None and hmac.compare_digest(match.group(1), expected.encode())


def build_http_app(server: MCPServer, settings: Settings, token: Callable[[], str | None]) -> ASGIApp:
    host = settings.mcp_host
    assert host is not None, "check_network_settings must run first"
    security = TransportSecuritySettings(allowed_hosts=[f"{host}:{settings.mcp_port}", host])
    app = server.streamable_http_app(streamable_http_path="/mcp", transport_security=security, host=host)
    return BearerAuth(app, token)
