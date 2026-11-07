"""`tg-bridge-net`: the long-running networked process (feature 11).

One asyncio process, one Telegram client: MCP over streamable HTTP behind a bearer token on the LAN address, and the
admin page on loopback in a thread. `token` and `link` are short separate commands that only touch the token files
(and the Bot API), so a rotation needs no IPC with the running service.

Exit codes: 2 = config error, 3 = not logged in (neither may restart under systemd), 1 = anything else.
"""

import argparse
import asyncio
import logging
import sys

import uvicorn

from tg_bridge.admin import start_admin_in_thread
from tg_bridge.config import Settings, check_network_settings, load_settings
from tg_bridge.errors import ConfigError, NotLoggedInError
from tg_bridge.http_app import build_http_app
from tg_bridge.logging_setup import configure_logging
from tg_bridge.notify import admin_link, send_login_link
from tg_bridge.server import build_live, build_server, build_source, hidden_chats
from tg_bridge.telegram.client import NOT_LOGGED_IN
from tg_bridge.token_file import BadTokenFile, admin_token_file, mcp_token_file

log = logging.getLogger(__name__)

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_NOT_LOGGED_IN = 0, 1, 2, 3


async def serve(settings: Settings) -> int:
    """Run the service until it is stopped; returns the process exit code."""
    mcp_tokens = mcp_token_file(settings.session_path)
    admin_tokens = admin_token_file(settings.session_path)
    if mcp_tokens.read() is None:
        print("no MCP token: run tg-bridge-net token --new", file=sys.stderr)
        return EXIT_CONFIG

    live = build_live(settings)
    source = build_source(settings, live=live, warn_no_bot=True)
    try:
        # Connect and check authorization before anything is sent or opened.
        try:
            await live.whoami()
        except NotLoggedInError:
            print(NOT_LOGGED_IN, file=sys.stderr)
            return EXIT_NOT_LOGGED_IN
        except Exception as e:  # noqa: BLE001 - systemd retries after RestartSec
            log.error("telegram connect failed: %s", type(e).__name__)
            return EXIT_FAILED

        loop = asyncio.get_running_loop()
        admin_server = start_admin_in_thread(settings, live, loop, hidden_chats(settings))
        send_task = None
        try:
            try:
                token = admin_tokens.load_or_create()
            except BadTokenFile as e:
                print(f"{e}; run tg-bridge-net link --new", file=sys.stderr)
                return EXIT_CONFIG
            # A slow or failing bot must never delay MCP.
            send_task = loop.run_in_executor(None, send_login_link, settings, admin_link(settings, token))

            app = build_http_app(build_server(source, settings), settings, mcp_tokens.read)
            log.info(
                "starting tg-bridge-net (http %s:%d, admin 127.0.0.1:%d, scope=%s)",
                settings.mcp_host,
                settings.mcp_port,
                settings.admin_port,
                settings.scope,
            )
            config = uvicorn.Config(
                app, host=settings.mcp_host, port=settings.mcp_port, log_config=None, access_log=False, lifespan="on"
            )
            await uvicorn.Server(config).serve()
            return EXIT_OK
        finally:
            # shutdown() waits for the page thread, which may be waiting for this loop: keep the loop free.
            await loop.run_in_executor(None, admin_server.shutdown)
            admin_server.server_close()
            if send_task is not None:
                await send_task
    finally:
        await source.close()  # MCP's lifespan closes it too; LiveSource.close is idempotent


def _load() -> Settings:
    try:
        return load_settings()
    except ConfigError as e:
        print(f"tg-bridge-net: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


def _serve_command() -> int:
    settings = _load()
    try:
        check_network_settings(settings)
    except ConfigError as e:
        print(f"tg-bridge-net: {e}", file=sys.stderr)
        return EXIT_CONFIG
    return asyncio.run(serve(settings))


def _token_command(args: argparse.Namespace) -> int:
    settings = _load()
    tokens = mcp_token_file(settings.session_path)
    if args.new:
        # Outside `logging` on purpose: the token must never reach a log.
        print(tokens.rotate(), file=sys.stderr)
        print(
            "New MCP token printed above (shown once). Put it into Hermes' ~/.hermes/config.yaml "
            "(mcp_servers.tg-bridge.headers.Authorization: \"Bearer <token>\") and restart the gateway.",
            file=sys.stderr,
        )
        return EXIT_OK
    token = tokens.read()
    if token is None:
        print("no MCP token: run tg-bridge-net token --new", file=sys.stderr)
        return EXIT_CONFIG
    print(token)
    return EXIT_OK


def _link_command(args: argparse.Namespace) -> int:
    settings = _load()
    tokens = admin_token_file(settings.session_path)
    try:
        token = tokens.rotate() if args.new else tokens.load_or_create()
    except BadTokenFile as e:
        print(f"tg-bridge-net: {e}; run with --new to replace it", file=sys.stderr)
        return EXIT_CONFIG
    link = admin_link(settings, token)
    print(link)
    if args.send and not send_login_link(settings, link):
        print("tg-bridge-net: the link was not sent (check the bot settings and the log above)", file=sys.stderr)
        return EXIT_FAILED
    return EXIT_OK


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tg-bridge-net")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the MCP service (HTTP) and the admin page")
    token = sub.add_parser("token", help="manage the MCP bearer token")
    group = token.add_mutually_exclusive_group(required=True)
    group.add_argument("--new", action="store_true", help="rotate the token and print it once")
    group.add_argument("--show", action="store_true", help="print the current token")
    link = sub.add_parser("link", help="print the admin page login link")
    link.add_argument("--new", action="store_true", help="rotate the admin token first (old links and cookies die)")
    link.add_argument("--send", action="store_true", help="also send the link through the Homelab bot")
    args = parser.parse_args(argv)

    configure_logging()
    if args.command == "serve":
        code = _serve_command()
    elif args.command == "token":
        code = _token_command(args)
    else:
        code = _link_command(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
