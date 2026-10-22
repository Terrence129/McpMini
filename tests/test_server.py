"""最小测试：证明 server 的工具能被 MCP 客户端发现并调用。

`Client(mcp)` 是 v2 SDK 的「进程内连接」能力：不起子进程、不占端口，
直接把 server 对象交给 client —— 写测试非常方便。
"""

from __future__ import annotations

import pytest

from mcp import Client

from mcpmini.server import mcp


@pytest.mark.asyncio
async def test_list_tools() -> None:
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
        names = {t.name for t in tools}
        assert {"add", "now"} <= names


@pytest.mark.asyncio
async def test_call_add() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("add", {"a": 40, "b": 2})
        assert not result.is_error
        assert result.structured_content == {"result": 42.0}


@pytest.mark.asyncio
async def test_unknown_tool_is_error() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("does_not_exist", {})
        assert result.is_error
