"""Check that an older `mcp` streamable-HTTP client (Hermes ships 2.0.0) works against tg-bridge.

    uv run --isolated --no-project --python 3.11 --with 'mcp==2.0.0' python scripts/hermes_compat.py URL TOKEN
    uv run python scripts/hermes_compat.py --fake [--stateless]   # serve a FakeSource on 127.0.0.1:8799

Prints `OK <n> tools` on success. Throwaway but kept for re-runs. Never put a real token in shell history.
"""

import argparse
import asyncio
import sys

FAKE_PORT = 8799


async def check(url: str, token: str) -> int:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    import httpx2 as httpx  # the SDK's HTTP client package (2.0.0 and 2.3 both ship it)

    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                await session.call_tool("now", {})
                return len(tools.tools)


def serve_fake(token: str, stateless: bool) -> None:
    import threading
    import time
    from dataclasses import replace
    from pathlib import Path

    import uvicorn

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tests.fakes import FakeSource
    from tg_bridge import http_app
    from tg_bridge.config import load_settings
    from tg_bridge.server import build_server

    settings = replace(
        load_settings(env={"TG_API_ID": "1", "TG_API_HASH": "x"}, config_file=Path("/nonexistent.env")),
        mcp_host="127.0.0.1",
        mcp_port=FAKE_PORT,
    )
    server = build_server(FakeSource(), settings)
    if stateless:
        orig = server.streamable_http_app
        server.streamable_http_app = lambda **kw: orig(stateless_http=True, json_response=True, **kw)
    app = http_app.build_http_app(server, settings, lambda: token)
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=FAKE_PORT, log_config=None))
    threading.Thread(target=uv.run, daemon=True).start()
    while not uv.started:
        time.sleep(0.05)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?")
    parser.add_argument("token", nargs="?")
    parser.add_argument("--fake", action="store_true", help="serve a FakeSource and check it (needs the project env)")
    parser.add_argument("--stateless", action="store_true")
    args = parser.parse_args()
    if args.fake:
        token = "z" * 40
        serve_fake(token, args.stateless)
        print(f"fake server ready on 127.0.0.1:{FAKE_PORT}; press Ctrl+C to stop", file=sys.stderr)
        import time

        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return
    count = asyncio.run(check(args.url, args.token))
    print(f"OK {count} tools")


if __name__ == "__main__":
    main()
