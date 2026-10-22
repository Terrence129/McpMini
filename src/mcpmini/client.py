"""最简 MCP Client —— 亲手完成一次 MCP 会话的完整生命周期。

MCP 的会话只有三步（见规范 Lifecycle 一节）：

    1. initialize        双方交换协议版本 + 各自能力 (capabilities)
    2. 正常通信           list_tools / call_tool / read_resource ...
    3. shutdown          关闭连接

v2 SDK 的高层 `Client` 把这三步包进了 `async with`。它的 `mode="auto"` 会
先探测服务端走哪个协议时代，再自动回退到经典握手 —— 所以你不需要自己处理版本协商。

本文件演示「手工驱动」一遍，方便你以后调试真实 server 时可以直接复用。

跑起来：  uv run python -m mcpmini.client
"""

from __future__ import annotations

import asyncio

from mcp import Client
from mcp.client.stdio import StdioServerParameters


def server_params() -> StdioServerParameters:
    """告诉 Client 如何启动我们的 server 子进程。

    - command/args：用同一个解释器跑 `python -m mcpmini.server`
    - cwd：保证能找到 src 包（通过 pyproject 的 src-layout 安装到 venv 后其实都可以，
           这里显式指定更稳，也方便你自己改 command 试别家的 server）
    """
    import sys
    from pathlib import Path

    project_root = Path(__file__).resolve().parents[2]
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcpmini.server"],
        cwd=str(project_root),
    )


async def demo() -> None:
    # async with = 建立连接(握手) + 退出时自动 shutdown
    async with Client(server_params()) as client:
        # ---- 握手后立刻能读到的事实 ----
        print("== 握手结果 ==")
        print("协议版本 :", client.protocol_version)
        print("服务端   :", client.server_info)
        print("能力     :", client.server_capabilities)
        print("说明     :", client.instructions)

        # ---- 第 1 步：发现工具（tools/list）----
        print("\n== 工具清单 ==")
        tools = (await client.list_tools()).tools
        for tool in tools:
            print(f"- {tool.name}: {tool.description}")
            print(f"  输入 Schema: {tool.input_schema}")

        # ---- 第 2 步：调用工具（tools/call）----
        print("\n== 调用 add(a=40, b=2) ==")
        result = await client.call_tool("add", {"a": 40, "b": 2})
        print("is_error        :", result.is_error)
        print("content(给模型) :", result.content)
        print("structured(给代码):", result.structured_content)


def main() -> None:
    asyncio.run(demo())


if __name__ == "__main__":
    main()
