# McpMini —— 从零手把手实现一个 MCP Agent

> 用最小可运行代码，走通 **MCP Server → MCP Client → LLM Agent** 的完整链路。
>
> 技术栈：Python 3.12 + 官方 `mcp` Python SDK **v2.3.0** + stdio 传输 + OpenAI 兼容的 LLM API

---

## 0. 先搞懂：MCP 到底是什么

MCP（Model Context Protocol，模型上下文协议）是 Anthropic 在 2024 年底提出、现在由社区共同维护的**开放协议**。它解决的问题只有一句话：

> **怎么让 AI 应用（Host）用一种标准方式，接入任意多的外部能力（Server）？**

在 MCP 之前，每接一个数据源就要写一套胶水代码（N 个应用 × M 个数据源 = N×M 种集成）。
有了 MCP，就变成 N+M：应用只要实现一次客户端，数据源只要实现一次服务端。

官方文档：[modelcontextprotocol.io](https://modelcontextprotocol.io/docs/learn/architecture)、
[Python SDK v2 文档](https://py.sdk.modelcontextprotocol.io/)

### 0.1 三个角色

```
┌─────────────────────────── MCP Host（宿主：AI 应用，比如 Claude Desktop / 你的 Agent）──────────────────────────┐
│                                                                                                                  │
│    ┌──────────────┐   JSON-RPC    ┌────────────────────┐   JSON-RPC    ┌────────────────────┐                    │
│    │  MCP Client  │ ────────────▶ │  MCP Server A      │               │  MCP Server B      │                    │
│    │  (一个 Server │ ◀──────────── │  (文件系统)         │               │  (数据库)           │                    │
│    │   对应一个)    │               └────────────────────┘               └────────────────────┘                    │
│    └──────┬───────┘                                                                                              │
│           │ 工具清单翻译成 function schema                                                                          │
│    ┌──────▼───────┐                                                                                              │
│    │    LLM       │  决定调哪个工具 / 传什么参数                                                                      │
│    └──────────────┘                                                                                              │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

| 角色 | 是谁 | 职责 |
|---|---|---|
| **Host** | Claude Desktop、Cursor、你写的 Agent | 管理 LLM 和所有连接；决定要不要把工具给模型看 |
| **Client** | Host 内部的一个对象 | 与**某一个** Server 保持 1:1 连接，负责协议通信 |
| **Server** | 一个程序（本地子进程或远程服务） | 把能力包装成标准原语暴露出去 |

关键认知：**MCP 只管"上下文交换"，不管 LLM 怎么推理**。它是"AI 和外部世界之间的 USB-C 接口"，不负责帮你调模型。

### 0.2 两种分层：传输层 + 数据层

```
┌─────────────────────────────────────────────┐
│  数据层 (Data layer)  —— JSON-RPC 2.0        │
│  initialize / tools/list / tools/call ...   │
├─────────────────────────────────────────────┤
│  传输层 (Transport)                          │
│  stdio（本地子进程） | Streamable HTTP（远程）  │
└─────────────────────────────────────────────┘
```

- **传输层**只负责搬字节。本项目用 **stdio**：Host 启动 server 子进程，通过 stdin/stdout 收发消息。
  > ⚠️ 这就是为什么 server 里 **不能用 `print()`** —— 那会污染协议通道，必须写 `stderr`。
- **数据层**是 JSON-RPC 2.0 消息，定义了"说什么"。换传输层不影响数据层，这也是 MCP 能同时支持本地和远程的原因。

### 0.3 一次会话的生命周期

任何连接都必须按这个顺序走（[规范 lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)）：

```
Client                                     Server
  │──── initialize (版本 + 能力) ────────────▶│
  │◀─── result  (版本 + 能力 + serverInfo) ───│   ← 协商握手
  │──── notifications/initialized ──────────▶│
  │                                           │
  │──── tools/list ─────────────────────────▶│   ← 正常操作
  │◀─── [工具定义] ──────────────────────────│
  │──── tools/call {name, arguments} ───────▶│
  │◀─── CallToolResult {content, isError} ───│
  │                                           │
  │──── （关闭）─────────────────────────────▶│   ← shutdown
```

**能力协商（capability negotiation）**是核心：握手时双方各自声明支持什么。
Server 声明 `tools` / `resources` / `prompts`；Client 声明 `sampling` / `roots` / `elicitation`。
**只有对方声明过的能力，你才能用。**

### 0.4 Server 的三种原语（primitives）

| 原语 | 谁控制 | 用途 | 类比 |
|---|---|---|---|
| **Tools** | 模型（model-controlled） | 执行操作、查询数据 | "可以调用的函数" |
| **Resources** | 应用（application-controlled） | 只读数据，URI 寻址 | "文件" |
| **Prompts** | 用户（user-controlled） | 预置提示模板 | "快捷指令菜单" |

**Agent 开发中最常用的就是 Tools。** 本项目聚焦 Tools，Resources 和 Prompts 放在文末练习里。

### 0.5 为什么 MCP 和 LLM 能无缝拼接

MCP Tool 的定义天生就是**模型友好的**：

```json
{ "name": "add",
  "description": "把两个数字相加并返回结果。",
  "inputSchema": {"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]} }
```

对照 OpenAI function calling 的格式：

```json
{ "type": "function",
  "function": {"name": "add", "description": "...", "parameters": { ... 同一个 JSON Schema ... }} }
```

**是一一对应的。** 所以 Host 拿到 MCP 工具清单后，几乎零成本就能喂给 LLM —— 代码里 `to_openai_tools()` 做的就是这件事。

---

## 1. 项目结构

```
McpMini/
├── pyproject.toml              # uv 项目 + 依赖 + CLI 入口
├── README.md                   # 你正在读的这份教程
├── src/mcpmini/
│   ├── __init__.py
│   ├── server.py               # ★ 第 1 层：MCP Server（暴露 tools）
│   ├── client.py               # ★ 第 2 层：MCP Client（握手/发现/调用）
│   └── agent.py                # ★ 第 3 层：Host + LLM（tool-calling 闭环）
└── tests/
    └── test_server.py          # 最小测试（进程内连接，不用起子进程）
```

---

## 2. 环境准备

已装好（`uv` 会自动管理 venv）：

```bash
cd McpMini
uv sync                      # 安装依赖
uv run pytest -q             # 跑测试，应该 3 passed
```

> 版本注意：本机装的是 **mcp SDK v2.3.0**。v2 有几个**破坏性变更**，网上大量老教程还是 v1 写法，会直接报错：
>
> | v1 写法（过时） | v2 写法（本项目） |
> |---|---|
> | `from mcp.server.fastmcp import FastMCP` | `from mcp.server import MCPServer` |
> | `FastMCP("name")` | `MCPServer("name")` |
> | 低层客户端手工 `ClientSession` + `stdio_client` | 高层 `from mcp import Client` |
>
> 完整迁移表见 [v2 Migration Guide](https://py.sdk.modelcontextprotocol.io/v2/migration/)。

---

## 3. 第一层：写一个 MCP Server

打开 [`src/mcpmini/server.py`](src/mcpmini/server.py)，核心只有十几行：

```python
from mcp.server import MCPServer

mcp = MCPServer("McpMini", instructions="一个教学用的迷你 MCP server。")

@mcp.tool(title="加法计算")
def add(a: float, b: float) -> float:
    """把两个数字相加并返回结果。用于任何形式的加法运算。"""
    return a + b

mcp.run()          # 默认 stdio
```

### 关键点逐个拆解

**① `@mcp.tool()` 装饰器做了什么？**
它读取函数的**类型注解**生成输入 schema，读取 **docstring** 生成 `description`，
并把**返回值类型**生成输出 schema（v2 会自动产出 `structured_content`）。

返回值怎么被结构化，规则值得记一下：

| 返回类型 | `structured_content` | `content`（给模型） |
|---|---|---|
| Pydantic `BaseModel` / `TypedDict` / dataclass | 原样对象，不包装 | JSON 文本 |
| **标量** `str` / `int` / `float` / `bool` | **包一层** `{"result": ...}` | 值本身 |
| `list[...]` | 包一层 `{"result": [...]}`，或展开 | 每项一个 TextContent 块 |

所以本项目的 `add() -> float` 返回 `42.0`，client 侧看到的是 `structured_content == {"result": 42.0}` ——
这个 `<1.0` 包装是 SDK 自动加的，不是你写的。

这就是为什么**类型注解和 docstring 不是可选的文档，而是模型的行为依据**：

```
def add(a: float, b: float) -> float:
        ↑       ↑   ↑             ↑
        │       └─ 两个必填 number 参数
        └─ 注释写得好，模型才知道什么时候该调它
```

> 你之前问"AI 怎么知道该调哪个工具"——答案就在这里：**它只能看到 name / description / inputSchema 这三样东西**。

**② `instructions` 是什么？**
握手时随 `initialize` 响应下发的整体说明，Host 可以把它注入到系统提示里，帮助模型理解这个 server 的定位。

**③ `mcp.run()` 为什么不用指定 stdio？**
`run()` 的默认 transport 就是 `"stdio"`。它进入一个事件循环，从 stdin 读 JSON-RPC、往 stdout 写。

**④ 为什么所有日志都写 `sys.stderr`？**
stdio 传输下 `stdout` 是纯协议通道。你 print 一行"hello"，客户端那边就解析失败直接断开。

### 立刻验证它

不写任何 client，直接用裸 JSON-RPC 手工驱动（这正是理解协议最直接的方式）：

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"raw","version":"1"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"add","arguments":{"a":40,"b":2}}}' \
| uv run python -m mcpmini.server
```

你会看到 stdout 上逐行返回三条 JSON-RPC 响应 —— **这就是 MCP 的全部魔法**。

---

## 4. 第二层：写一个 MCP Client

打开 [`src/mcpmini/client.py`](src/mcpmini/client.py)：

```python
from mcp import Client
from mcp.client.stdio import StdioServerParameters

params = StdioServerParameters(
    command=sys.executable,          # 用同一个 Python 解释器
    args=["-m", "mcpmini.server"],   # 启动我们的 server
    cwd=str(project_root),
)

async with Client(params) as client:                           # ← 握手 + 退出时 shutdown
    print(client.protocol_version, client.server_info)         # ← 握手结果
    tools = (await client.list_tools()).tools                  # ← tools/list
    result = await client.call_tool("add", {"a": 40, "b": 2})  # ← tools/call
```

跑一下：

```bash
uv run mcpmini-client      # 或：uv run python -m mcpmini.client
```

实测输出（注意协商到的协议版本）：

```
协议版本 : 2026-07-28
服务端   : name='McpMini' ...
能力     : prompts=... resources=... tools=ToolsCapability(list_changed=True)
- add: 把两个数字相加并返回结果。用于任何形式的加法运算。
  输入 Schema: {'type': 'object', 'properties': {'a': {'type':'number'}, 'b': {'type':'number'}}, 'required': ['a','b']}
- now: 返回当前的本地日期和时间。...

== 调用 add(a=40, b=2) ==
is_error        : False
content(给模型) : [TextContent(type='text', text='42.0')]
structured(给代码): {'result': 42.0}
```

### 关键点

**① `Client(params)` 的四张脸** —— 这是 v2 最方便的设计：

| 你传什么 | 走什么传输 | 典型用途 |
|---|---|---|
| `StdioServerParameters` | 拉起子进程，走 stdin/stdout | 本地 server（本项目） |
| URL 字符串 `"http://..."` | Streamable HTTP | 远程 server |
| 一个 transport 对象 | 自定义 | 特殊网络环境 |
| **`MCPServer` 实例** | **进程内直连，无进程无端口** | **写测试** |

**② 一个返回值，三种读法：**

| 字段 | 给谁看 | 说明 |
|---|---|---|
| `result.content` | **模型**看 | content blocks（text / image / audio / resource link） |
| `result.structured_content` | **代码**看 | 结构化 JSON，符合 outputSchema |
| `result.is_error` | 你判断 | 工具"失败"也是正常结果，不抛异常 |

**③ 工具报错不会崩溃，但错误文本会"分级"。**

| server 侧怎么抛 | client 看到 `is_error` | client 看到 `content` 文本 | server 日志 |
|---|---|---|---|
| `raise ToolError("除数不能为0")` | `True` | `Error executing tool divide: 除数不能为0`（**保留你的消息**） | INFO |
| `raise ValueError("除数不能为0")` | `True` | 只有 `Error executing tool divide`（**原始文本被吞掉**） | ERROR + traceback |
| `raise MCPError(...)` | — | 不返回结果，client 侧**抛异常**（wire 上是 JSON-RPC error） | — |

> ⚠️ 这是 v2 的**重要变化**，很多老教程还写着"错误信息会传给模型"。
> 实际上只有 `ToolError` 才会把你的消息透给模型，普通异常只会得到一句干巴巴的 `Error executing tool <name>`，
> 因为 SDK 认为意外异常可能泄露内部细节。**想让模型看到错误原因并自我修正，就用 `ToolError`。**
>
> 导入路径（v2，不在 `__init__` 里导出）：
> ```python
> from mcp.server.mcpserver.exceptions import ToolError
> ```

这个"错误也分级"的设计是刻意的：**可预期的失败**应该让模型读到并重试；**意外崩溃**不该把堆栈泄露给外部。

---

## 5. 第三层：接上真实 LLM，变成一个 Agent

打开 [`src/mcpmini/agent.py`](src/mcpmini/agent.py)。这一层才是"Agent"。

### 5.1 核心循环

```python
async with Client(server_params()) as client:
    mcp_tools = (await client.list_tools()).tools      # 1. MCP 发现工具
    tools = to_openai_tools(mcp_tools)                 # 2. 翻译成 function schema

    for step in range(max_steps):
        msg = chat(messages, tools)                    # 3. LLM 决策
        if not msg.tool_calls:
            return msg.content                         #    不再要工具 → 输出最终答案

        for call in msg.tool_calls:                    # 4. 可能并行调多个
            result = await client.call_tool(call.name, call.args)   # MCP 执行
            messages.append({role: "tool", tool_call_id: ..., content: result_text})  # 5. 回填
```

画成图：

```
用户问题
   │
   ▼
┌─ LLM ────────────────────────────────────────┐
│  看到: 系统提示 + 用户问题 + [工具清单]          │
│  输出: {"tool_calls": [add(a=40,b=2)]}        │
└──────────────┬───────────────────────────────┘
               ▼
     MCP tools/call  ──▶  MCP Server 执行  ──▶  "42.0"
               ▼
   回填 role=tool 消息，再问 LLM
               │
               ▼
┌─ LLM ────────────────────────────────────────┐
│  看到: 工具返回 42.0                            │
│  输出: "40 + 2 = 42"                          │
└──────────────────────────────────────────────┘
```

**这就是 ReAct（Reason + Act）范式的具体实现**。MCP 负责"Act"的标准化，LLM 负责"Reason"。

### 5.2 先离线跑通（不需要 API key）

```bash
uv run mcpmini-agent --offline "计算 40 + 2，并告诉我现在的日期时间。"
```

实测输出：

```
[agent] 模式=offline(假模型)
[mcp] 发现 2 个工具: ['add', 'now']
[step 1] LLM 决定调用 -> add({'a': 40, 'b': 2})
[step 1] 工具返回 -> 42.0
[step 2] LLM 决定调用 -> now({})
[step 2] 工具返回 -> 2026-10-22 22:12:17

=== 最终回答 ===
（离线模拟）工具结果：42.0 | 2026-10-22 22:12:17
```

`--offline` 用一个按关键词决策的"假模型"替代 LLM。
**它的价值在于：把"LLM 的决策"和"MCP 的链路"解耦** —— 你先确认 MCP 这段没问题，再去排查模型问题。

### 5.3 接真实 LLM

```bash
export LLM_API_KEY=sk-xxxxxxxx
# 可选覆盖（默认就是 DeepSeek）：
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat

uv run mcpmini-agent "帮我算一下 123 乘 456 再告诉我现在几点"
```

支持任何 OpenAI 兼容接口（DeepSeek / OpenAI / 通义 / Moonshot / 本地 vLLM 等）。
如果你用 Anthropic 官方 API，需要把 `chat()` 换成 Messages API 的 tool_use 格式（结构不同，但映射思路一样）。

### 5.4 为什么 `to_openai_tools()` 这么短？

```python
def to_openai_tools(mcp_tools):
    return [{"type": "function",
             "function": {"name": t.name,
                          "description": t.description or "",
                          "parameters": t.input_schema}}   # ← 直接复用 MCP 的 JSON Schema
            for t in mcp_tools]
```

因为 MCP 的 `inputSchema` **本来就是 JSON Schema**，而 function calling 的 `parameters` 也要求 JSON Schema。
**这就是 MCP 在设计上对 Agent 开发者最大的善意：工具定义零转换成本。**

### 5.5 生产环境还要补什么

教学版故意保持极简。真实 Agent 你还需要：

| 缺失项 | 说明 |
|---|---|
| 多 server 聚合 | 一个 Agent 同时连 GitHub / DB / 文件系统 等多个 server，需处理**工具名冲突**（加前缀） |
| 工具调用人工确认 | "删文件"这类危险操作要有 human-in-the-loop |
| 超时 / 重试 / 并发 | `call_tool` 支持 `read_timeout_seconds` |
| 流式输出 | LLM 侧用 streaming，避免长等待 |
| 上下文压缩 | 工具返回内容过长时要截断或摘要 |
| 可观测性 | 记录每一步的 tool_call 及耗时 |

---

## 6. 调试工具箱

### 6.1 官方 Inspector（图形化调试）

```bash
uv run mcp dev src/mcpmini/server.py:mcp
```

实测输出（本机）：

```
Starting MCP inspector...

MCP Inspector Web is up and running at:
   http://127.0.0.1:6274?MCP_INSPECTOR_API_TOKEN=4e55b8a6...
   Sandbox (MCP Apps): http://127.0.0.1:6275/sandbox
```

**要点（这些是实测的，网上老教程基本都过时了）：**

| 项 | 实际情况 |
|---|---|
| Inspector 版本 | npm `latest` = **v2.12.0**（v1 已 deprecated，停在 1.0.3） |
| Node 要求 | **≥ 22.19.0**（本机 v22.20.0 ✅）。`mcp dev` 本身只检查 `npx` 在 PATH 上 |
| UI 端口 | **只有 6274**。v1 的 6277 proxy 端口在 v2 已取消 |
| 打开哪个 URL | 打开**终端打印出来的那个带 `MCP_INSPECTOR_API_TOKEN` 的 URL**，别手敲 `localhost:6274` |
| `mcp dev` 参数 | 只有 `file_spec`、`-e/--with-editable`、`--with`。**没有**端口/浏览器/transport 参数 |
| 背后做了什么 | `npx @modelcontextprotocol/inspector uv run --with mcp[...] mcp run <file_spec>` |

v2 的 UI 是重写的：没有 v1 那种单屏的 "Transport Type / Command / Arguments" 侧栏表单了。
现在是 **Servers 页 → Add Servers 添加一条记录（`type` / `command` / `args`）→ Connect**。
不过对 `mcp dev` 这条路你通常不用手动填 —— 它已经把 server 作为 ad-hoc 目标传进去了。

> 📌 也可以按传统方式直连（绕过 `mcp dev`）：
> ```bash
> npx @modelcontextprotocol/inspector uv run --python <venv-python> python -m mcpmini.server
> ```


### 6.2 换成 HTTP 传输（远程场景）

stdio 只适合本地。想跑成远程服务（或给别的机器用），一行命令切换：

```bash
uv run mcp run src/mcpmini/server.py:mcp --transport streamable-http
# 默认监听 http://localhost:8000/mcp
```

客户端只需把 `StdioServerParameters` 换成 URL 字符串：

```python
async with Client("http://localhost:8000/mcp") as client:
    ...
```

**server 代码一行都不用改** —— 这就是传输层与数据层分离的好处。
（注意：Streamable HTTP 有会话概念，首次 POST 拿 `mcp-session-id`，后续请求要带上。）

### 6.3 进程内测试

```python
from mcp import Client
from mcpmini.server import mcp

async with Client(mcp) as client:        # 直接把 server 对象交进去
    tools = (await client.list_tools()).tools
```

不起子进程、不占端口，速度快百倍。见 [`tests/test_server.py`](tests/test_server.py)。

---

## 7. 踩坑清单（我实测遇到的）

| 坑 | 表现 | 解决 |
|---|---|---|
| **`print()` 污染 stdout** | 客户端握手成功但后续请求无响应 / JSON 解析错误 | 所有日志写 `sys.stderr` |
| **用了 v1 的 `FastMCP`** | `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` | 改 `from mcp.server import MCPServer` |
| **忘记 `async with`** | `Client` 用完即弃，不能复用 | 整个会话包在一个 `async with` 里 |
| **以为工具报错会抛异常** | 拿不到结果 | 检查 `result.is_error`，错误在 `result.content` 里 |
| **docstring 写太随意** | 模型不调或调错工具 | docstring 是给模型的说明书，写清"什么时候用" |
| **路径找不到包** | server 子进程启动失败 | `StdioServerParameters(cwd=...)` 指定工作目录 |
| **`mcp dev` 报 npx not found** | Inspector 起不来 | Inspector 是 Node 应用，需要 `npx` 在 PATH 上，且 Node ≥ 22.19.0 |
| **打开 6274 后 API 报未授权** | 界面空白/请求 401 | 用终端打印的带 `?MCP_INSPECTOR_API_TOKEN=` 的完整 URL |

---

## 8. 动手练习（从易到难）

### 练习 1 ⭐ 加一个除法工具，并让它优雅地报错

在 `server.py` 里加：

```python
@mcp.tool(title="除法计算")
def divide(a: float, b: float) -> float:
    """两个数字相除。当用户要做除法时使用。"""
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b
```

**要观察的**：除以 0 时，client 侧拿到的是 `is_error=True` 还是异常？
更关键的：模型看到的错误**文本是什么**？

然后把它改成 `raise ToolError("除数不能为 0")`，再跑一次，对比两版的 `content` 差异。
（提示：看 4.x 节那张"错误分级"表。答案：`is_error=True`；但 `ValueError` 版本模型只能看到
`Error executing tool divide`，**完全不知道是除以零导致的** —— 这在真实 Agent 里会造成模型反复重试同样错误。）

> 这就是为什么生产环境的 tool 应该显式 `raise ToolError(可读的原因)`，而不是随手抛 `Exception`。

### 练习 2 ⭐⭐ 加上 Resources 原语

Resource 是"只读数据"，用 URI 寻址：

```python
@mcp.resource("config://settings")
def settings() -> dict:
    """返回本 server 的配置。"""
    return {"version": "0.1.0", "author": "you"}

@mcp.resource("docs://readme")
def readme() -> str:
    """返回一份说明文档。"""
    return "这是 McpMini 的说明。"
```

然后在 `client.py` 里用 `await client.list_resources()` 和 `await client.read_resource("config://settings")` 读取。

### 练习 3 ⭐⭐ 加上 Prompts 原语

```python
@mcp.prompt(title="写周报")
def weekly_report(work: str) -> str:
    """根据工作要点生成周报。"""
    return f"请把以下工作要点整理成一份简洁的周报：{work}"
```

client 侧用 `client.get_prompt("weekly_report", {"work": "..."})`，它返回的是 `messages` 列表，可以直接喂给 LLM。

### 练习 4 ⭐⭐⭐ 工具名冲突：连接两个 server

把 `server.py` 复制成 `server2.py`（换个 server 名、加一个同名但行为不同的工具），
在 `agent.py` 里同时连两个 server，把工具名加上 `server1__` / `server2__` 前缀后一起给 LLM。

**要思考的**：如果两个 server 都叫 `add`，模型调用时怎么区分？这就是为什么真实 Agent 框架都做前缀命名空间。

### 练习 5 ⭐⭐⭐ 接入真实 LLM 并用它做一次多步任务

设好 API key，然后问它一个需要**连续两次调工具**的问题，比如：

> "先算出 1999 + 2001，再告诉我结果和现在时间相差多少小时"

观察 trace 里模型是怎么一步步推进的。然后**故意把某个工具的 docstring 改坏**，看模型是否还会调用它 —— 体会"工具描述是提示工程"这件事。

### 练习 6 ⭐⭐⭐⭐ 加 human-in-the-loop

在 `agent.py` 执行工具前插入确认环节：

```python
if input(f"允许调用 {name}({args}) 吗？[y/N] ") != "y":
    messages.append({"role": "tool", "tool_call_id": call["id"],
                     "content": "用户拒绝执行该工具。"})
    continue
```

这就是 MCP 规范里强调的 "human in the loop" —— 危险操作必须可被拦截。

---

## 9. 延伸阅读

- [MCP 官方文档 · 架构概览](https://modelcontextprotocol.io/docs/learn/architecture)
- [MCP 规范 · Lifecycle](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)
- [MCP 规范 · Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP 规范 · Resources / Prompts](https://modelcontextprotocol.io/specification/2025-06-18/server)
- [Python SDK v2 文档](https://py.sdk.modelcontextprotocol.io/v2/)
- [Python SDK v2 迁移指南](https://py.sdk.modelcontextprotocol.io/v2/migration/)
- [官方参考 server 集合](https://github.com/modelcontextprotocol/servers)
