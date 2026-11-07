"""Secret token files shared by the admin page and the network MCP process.

Files live next to the session (dir 700, files 600). `read()` re-checks the file on every call
(cheap stat, re-read only when it changed), so a rotation done by another process applies at once.
"""

import os
import re
import secrets
import tempfile
from pathlib import Path

from tg_bridge.errors import TgBridgeError

MIN_TOKEN_LEN = 32
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]+")


class BadTokenFile(TgBridgeError):
    """The token file exists but is unreadable, empty or malformed; never replaced silently."""


class TokenFile:
    def __init__(self, path: Path):
        self.path = path
        self._stamp: tuple[int, int, int] | None = None
        self._value: str | None = None

    def read(self) -> str | None:
        """The stripped token, or None if the file is missing, empty or too short/malformed."""
        try:
            st = self.path.stat()
        except OSError:
            self._stamp = self._value = None
            return None
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        if stamp == self._stamp:
            return self._value
        try:
            raw = self.path.read_text(encoding="ascii").strip()
        except (OSError, ValueError):
            self._stamp = self._value = None
            return None
        value = raw if len(raw) >= MIN_TOKEN_LEN and _TOKEN_CHARS.fullmatch(raw) else None
        self._stamp, self._value = stamp, value
        return value

    def rotate(self) -> str:
        token = secrets.token_urlsafe(32)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkstemp creates the file 600; replace() makes the swap atomic.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(token + "\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return token

    def load_or_create(self) -> str:
        token = self.read()
        if token is not None:
            return token
        if self.path.exists():
            raise BadTokenFile(f"the token file {self.path.name} is unreadable, empty or malformed")
        return self.rotate()


def admin_token_file(session_path: Path) -> TokenFile:
    # Name unchanged from feature 17, so the Mac's existing login link keeps working.
    return TokenFile(session_path.with_name(f"{session_path.stem}.admin-token"))


def mcp_token_file(session_path: Path) -> TokenFile:
    return TokenFile(session_path.with_name(f"{session_path.stem}.mcp-token"))
