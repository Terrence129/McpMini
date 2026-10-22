"""最简 MCP Server —— 用模型能理解的方式暴露「能力」。

MCP 三要素里，Server 提供三种原语（primitive）：

    tool     （模型控制）模型可以主动调用，有输入 JSON Schema，有执行结果
    resource （应用控制）只读数据，用 URI 寻址，像"文件"
    prompt   （用户控制）预置提示词模板，用户从菜单里选

本骨架只做一个 tool，其余两个后面在练习里加。

跑起来：  uv run python -m mcpmini.server

注意：stdio 传输下，stdout 是协议通道（JSON-RPC），
所以日志一定要写 stderr，绝不能用 print() —— 这也是新手最常见的坑。
"""

from __future__ import annotations

import sys

from mcp.server import MCPServer

# 1) 创建 server。名字和 instructions 会随 initialize 响应发给客户端。
mcp = MCPServer(
    "McpMini",
    instructions="一个教学用的迷你 MCP server，提供计算器工具。",
)


# 2) 用装饰器把普通 Python 函数注册成 tool。
#    关键点：函数签名 + 类型注解 + docstring，会被 SDK 自动转成 JSON Schema，
#    这份 Schema 就是 LLM 决定怎么调用的唯一依据 —— 所以写得越清楚，模型调用越准。
@mcp.tool(title="加法计算")
def add(a: float, b: float) -> float:
    """把两个数字相加并返回结果。用于任何形式的加法运算。"""
    return a + b


@mcp.tool(title="当前时间")
def now() -> str:
    """返回当前的本地日期和时间。当用户问现在几点、今天是几号时使用。"""
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 3) 入口：默认就是 stdio 传输。
#    Host 会以子进程方式启动它，通过 stdin/stdout 发 JSON-RPC 消息。
def main() -> None:
    print("[server] McpMini starting on stdio ...", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
