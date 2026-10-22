"""McpMini —— 用最小代码走通 MCP (Model Context Protocol) 的完整链路。

三层结构，从下到上：

    server.py    MCP Server：把「能力」暴露成 tools（模型可调用）
    client.py    MCP Client：连接 server，握手、列工具、调工具
    agent.py     宿主 (Host)：LLM 读工具清单 -> 决定调用 -> 拿到结果 -> 生成回答
"""

__version__ = "0.1.0"
