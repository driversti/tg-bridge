import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tests.fakes import FakeSource, make_person
from tg_bridge import tools
from tg_bridge.errors import NotLoggedInError, RateLimitedError
from tg_bridge.telegram.client import NOT_LOGGED_IN


async def test_whoami_with_username():
    assert await tools.whoami(FakeSource()) == "Anna K (@anna_k), id 42"


async def test_whoami_without_username():
    source = FakeSource(make_person(username=None))

    assert await tools.whoami(source) == "Anna K (no username), id 42"


async def test_not_logged_in_becomes_tool_error():
    with pytest.raises(ToolError, match="tg_bridge.login"):
        await tools.whoami(FakeSource(error=NotLoggedInError(NOT_LOGGED_IN)))


async def test_rate_limited_becomes_tool_error():
    with pytest.raises(ToolError, match="retry in 42 s"):
        await tools.whoami(FakeSource(error=RateLimitedError(42)))
