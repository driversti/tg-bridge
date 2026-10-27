"""`uv run python -m tg_bridge.login`: create the Telegram session (dir 700, file 600)."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from tg_bridge.config import load_settings
from tg_bridge.errors import ConfigError
from tg_bridge.logging_setup import configure_logging
from tg_bridge.telegram.login import interactive_login

log = logging.getLogger(__name__)


def prepare_session_dir(session_path: Path) -> None:
    """Create the session dir as 700; an existing dir may be shared (even ~), so only warn about it."""
    folder = session_path.parent
    if not folder.exists():
        folder.mkdir(mode=0o700, parents=True)
    elif folder.stat().st_mode & 0o077:
        log.warning("%s is accessible by other users; consider: chmod 700 %s", folder, folder)


def secure_session_file(session_path: Path) -> None:
    for path in (session_path, session_path.with_name(session_path.name + "-journal")):
        if path.exists():
            path.chmod(0o600)


def main() -> None:
    os.umask(0o077)  # the session is created 600 from the first byte, not chmod-ed afterwards
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge login: {e}", file=sys.stderr)
        sys.exit(2)
    prepare_session_dir(settings.session_path)
    me = asyncio.run(interactive_login(settings))
    secure_session_file(settings.session_path)
    print(f"Logged in as {me.display_name} (id {me.id}). Session: {settings.session_path}")
    print("You can review or revoke this session in Telegram → Settings → Devices.")


if __name__ == "__main__":
    main()
