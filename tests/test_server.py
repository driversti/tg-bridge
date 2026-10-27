from tests.fakes import FakeSource
from tg_bridge.errors import RateLimitedError
from tg_bridge.server import build_server


async def test_lists_whoami_tool():
    tools = await build_server(FakeSource()).list_tools()

    assert [t.name for t in tools] == ["whoami"]
    assert tools[0].annotations.read_only_hint is True


async def test_call_whoami_returns_plain_text():
    result = await build_server(FakeSource()).call_tool("whoami", {})

    assert result.is_error is False
    assert [c.text for c in result.content] == ["Anna K (@anna_k), id 42"]
    assert result.structured_content is None
