"""LLM Agent —— 把 MCP 工具接到真实大模型上，走完 tool-calling 闭环。

这是 Agent 开发里 MCP 真正在用的位置：Agent 是 **Host**，
它通过 MCP Client 连接若干 MCP Server，把工具清单转成 LLM 的 function schema，
模型决定调哪个工具、传什么参数，Host 执行后把结果喂回模型，直到模型给出最终答案。

本文件用 OpenAI 兼容的 Chat Completions 接口（DeepSeek / OpenAI / 通义 等都兼容）。
只要设好环境变量即可：

    # 在项目根目录 .env 写入（推荐，已被 .gitignore 忽略）：
    #   DEEPSEEK_API_KEY=sk-xxxx
    # 可选覆盖，默认 DeepSeek：
    export LLM_BASE_URL=https://api.deepseek.com/v1
    export LLM_MODEL=deepseek-chat

跑起来：  uv run python -m mcpmini.agent "计算 40 + 2，再告诉我现在几点"

依赖说明：只用标准库 urllib 发 HTTP 请求，避免为了一个教学 demo 再多装 SDK。
生产环境建议换成官方 openai SDK。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from mcp import Client

from .client import server_params

# ---------------------------------------------------------------------------
# 一、LLM 调用（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------


def load_dotenv(path: str | Path = ".env") -> None:
    """读取项目根目录的 .env（极简实现，不引入额外依赖）。

    已经存在的环境变量优先，不会被 .env 覆盖 —— 方便临时用 shell 变量覆盖。
    """
    env_path = Path(path)
    if not env_path.is_absolute():
        env_path = Path(__file__).resolve().parents[2] / path
    if not env_path.is_file():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_dotenv()

# DeepSeek 的 key 变量名是 DEEPSEEK_API_KEY；也兼容通用的 LLM_API_KEY
API_KEY = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY", "")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

SYSTEM_PROMPT = (
    "你是一个助手。可以使用提供的工具来完成任务，"
    "需要精确计算或查询当前时间时必须调用工具，不要凭空猜测。"
)


OFFLINE = False
"""`--offline` 模式：用脚本化的假模型代替真实 LLM。

它用来在没有 API key 时验证「MCP -> tool schema -> 执行 -> 结果回填」这条链路本身，
帮助你把「LLM 的部分」和「MCP 的部分」分开理解。真实接入见 `chat()`。
"""


def fake_chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """一个极其朴素的假模型：按用户问题里的关键词决定调用哪个工具。"""
    user_text = next(
        (m.get("content") or "" for m in messages if m.get("role") == "user"), ""
    )
    already_called = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}
    tool_names = {t["function"]["name"] for t in tools}

    if "add" in tool_names and ("计算" in user_text or "+" in user_text) and "call_add" not in already_called:
        nums = [int(s) for s in re.findall(r"\d+", user_text)][:2] or [0, 0]
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_add",
                    "type": "function",
                    "function": {
                        "name": "add",
                        "arguments": json.dumps({"a": nums[0], "b": nums[1]}),
                    },
                }
            ],
        }

    if "now" in tool_names and ("时间" in user_text or "几点" in user_text or "日期" in user_text) and "call_now" not in already_called:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_now", "type": "function", "function": {"name": "now", "arguments": "{}"}}
            ],
        }

    observations = [m["content"] for m in messages if m.get("role") == "tool"]
    if observations:
        return {"role": "assistant", "content": "（离线模拟）工具结果：" + " | ".join(observations)}
    return {"role": "assistant", "content": "（离线模拟）未匹配到可用工具。"}


def chat(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    """发一次 chat/completions 请求，返回 assistant message。"""
    if OFFLINE:
        return fake_chat(messages, tools)

    if not API_KEY:
        raise SystemExit(
            "缺少 API key。两种方式任选：\n"
            "  1) 在项目根目录 .env 里写  DEEPSEEK_API_KEY=sk-xxx\n"
            "  2) export DEEPSEEK_API_KEY=sk-xxx\n"
            "可选：LLM_BASE_URL、LLM_MODEL（默认 DeepSeek deepseek-chat）"
        )

    payload: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    req = urllib.request.Request(
        f"{BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]


# ---------------------------------------------------------------------------
# 二、MCP tool -> OpenAI function schema 的转换
# ---------------------------------------------------------------------------


def to_openai_tools(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """MCP `Tool` 天然就有 name / description / inputSchema，
    和 OpenAI function calling 的 `{"type":"function","function":{...}}` 一一对应。
    这就是 MCP + LLM 能无缝拼起来的根本原因。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in mcp_tools
    ]


# ---------------------------------------------------------------------------
# 三、Agent 主循环
# ---------------------------------------------------------------------------


async def run_agent(question: str, max_steps: int = 5) -> str:
    async with Client(server_params()) as client:
        # 1) 发现工具 —— 这是 MCP 的 tools/list
        mcp_tools = (await client.list_tools()).tools
        tools = to_openai_tools(mcp_tools)
        print(f"[mcp] 发现 {len(tools)} 个工具: {[t['function']['name'] for t in tools]}")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]

        # 2) ReAct 式循环：思考 -> 调工具 -> 观察结果 -> 再思考
        for step in range(1, max_steps + 1):
            msg = chat(messages, tools)
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # 模型不再要工具，链路结束
                return msg.get("content") or ""

            for call in tool_calls:  # 现代模型可能一次并行调多个工具
                fn = call["function"]
                name = fn["name"]
                # 模型产出的是 JSON 字符串，解析后作为 MCP 的 arguments
                args = json.loads(fn.get("arguments") or "{}")

                print(f"[step {step}] LLM 决定调用 -> {name}({args})")
                # 3) 真正执行 —— MCP tools/call
                result = await client.call_tool(name, args)

                # 工具出错不会抛异常，而是 is_error=True，把错误文本交还给模型自我修正
                if result.is_error:
                    text = "".join(
                        getattr(b, "text", "") for b in result.content
                    ) or "tool error"
                    print(f"[step {step}] 工具报错 -> {text}")
                else:
                    text = (
                        "".join(getattr(b, "text", "") for b in result.content)
                        if result.content
                        else json.dumps(result.structured_content, ensure_ascii=False)
                    )
                    print(f"[step {step}] 工具返回 -> {text}")

                # 4) 把工具结果作为 role=tool 消息拼回对话
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": text,
                    }
                )

        return "（达到最大步数上限，未能得出最终答案）"


def main() -> None:
    global OFFLINE
    import asyncio
    import sys

    argv = sys.argv[1:]
    if "--offline" in argv:
        OFFLINE = True
        argv.remove("--offline")

    question = " ".join(argv) or "计算 40 + 2，并告诉我现在的日期时间。"
    print(f"[agent] 模式={'offline(假模型)' if OFFLINE else f'live({MODEL})'}")
    answer = asyncio.run(run_agent(question))
    print("\n=== 最终回答 ===")
    print(answer)


if __name__ == "__main__":
    main()
