"""`uv run python -m tg_bridge.login [--method qr|code]`: create the Telegram session (dir 700, file 600)."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from tg_bridge.config import load_settings
from tg_bridge.errors import ConfigError, TgBridgeError
from tg_bridge.logging_setup import configure_logging
from tg_bridge.qr import render_qr
from tg_bridge.telegram.login import LoginMethod, interactive_login

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


METHOD_PROMPT = """How do you want to log in?
  1) QR code: scan it with Telegram on your phone (recommended)
  2) Login code: Telegram sends a code to your app or by SMS
Choice [1]: """

_METHODS: dict[str, LoginMethod] = {"": "qr", "1": "qr", "qr": "qr", "2": "code", "code": "code"}


def choose_method(ask=input, say=print) -> LoginMethod:
    while True:
        answer = ask(METHOD_PROMPT).strip().lower()
        if answer in _METHODS:
            return _METHODS[answer]
        say("Please enter 1 or 2.")


def show_qr(url: str) -> None:
    print(render_qr(url))


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m tg_bridge.login", description="Log in to Telegram.")
    parser.add_argument("--method", choices=["qr", "code"], help="skip the question and use this login method")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    os.umask(0o077)  # the session is created 600 from the first byte, not chmod-ed afterwards
    configure_logging()
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"tg-bridge login: {e}", file=sys.stderr)
        sys.exit(2)
    prepare_session_dir(settings.session_path)
    try:
        method = args.method or choose_method()
        me = asyncio.run(interactive_login(settings, method, show_qr=show_qr))
    except KeyboardInterrupt:
        print("\nLogin cancelled.", file=sys.stderr)
        sys.exit(130)
    except TgBridgeError as e:
        print(f"tg-bridge login: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        secure_session_file(settings.session_path)
    print(f"Logged in as {me.display_name} (id {me.id}). Session: {settings.session_path}")
    print("You can review or revoke this session in Telegram → Settings → Devices.")


if __name__ == "__main__":
    main()
