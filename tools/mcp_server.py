"""
Ling-Xi MCP Server
==================
把现有 LangChain tools 包装成 MCP stdio server，供 claude_code_sdk 使用。

用法:
    python -m tools.mcp_server [--challenge-code CODE] [--forum-id ID]
"""
import argparse
import asyncio
import json
import logging
import os
import sys

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types as mcp_types

logger = logging.getLogger(__name__)


class MCPToolInvocationError(RuntimeError):
    """将工具路由失败显式暴露给 MCP 调用方。"""


def _make_server(challenge_code: str = "", forum_id: int = 0) -> Server:
    server = Server("lingxi-tools")

    # ── 工具列表 ──
    @server.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        tools = [
            mcp_types.Tool(
                name="execute_command",
                description="在本地/Kali 执行 shell 命令",
                inputSchema={"type": "object", "properties": {"command": {"type": "string"}, "timeout": {"type": "integer", "default": 60}}, "required": ["command"]},
            ),
            mcp_types.Tool(
                name="execute_python",
                description="执行 Python 脚本（用于 exploit/PoC）",
                inputSchema={"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 60}}, "required": ["code"]},
            ),
        ]
        if challenge_code and not forum_id:
            tools += [
                mcp_types.Tool(
                    name="submit_flag",
                    description=f"提交 flag 到当前题目 {challenge_code}",
                    inputSchema={"type": "object", "properties": {"flag": {"type": "string"}}, "required": ["flag"]},
                ),
                mcp_types.Tool(
                    name="run_level2_cve_poc",
                    description="运行 Level-2 CVE PoC",
                    inputSchema={"type": "object", "properties": {"target": {"type": "string"}, "mode": {"type": "string", "default": "check"}, "extra": {"type": "string", "default": ""}}, "required": ["target"]},
                ),
            ]
        if forum_id:
            tools += [
                mcp_types.Tool(
                    name="execute_python",
                    description="执行 Python 脚本",
                    inputSchema={"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 60}}, "required": ["code"]},
                ),
                mcp_types.Tool(
                    name="forum_submit_flag",
                    description=f"提交论坛 flag 到题目 {forum_id}",
                    inputSchema={"type": "object", "properties": {"flag": {"type": "string"}}, "required": ["flag"]},
                ),
                mcp_types.Tool(
                    name="forum_get_challenges",
                    description="获取论坛挑战列表",
                    inputSchema={"type": "object", "properties": {}},
                ),
                mcp_types.Tool(
                    name="forum_get_my_agent_info",
                    description="获取我的 Agent 信息",
                    inputSchema={"type": "object", "properties": {}},
                ),
                mcp_types.Tool(
                    name="forum_get_agents",
                    description="获取 Agent 列表",
                    inputSchema={"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}},
                ),
                mcp_types.Tool(
                    name="forum_get_latest_posts",
                    description="获取最新帖子",
                    inputSchema={"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}},
                ),
                mcp_types.Tool(
                    name="forum_get_hot_posts",
                    description="获取热门帖子",
                    inputSchema={"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}},
                ),
                mcp_types.Tool(
                    name="forum_search_posts",
                    description="搜索帖子",
                    inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}, "required": ["query"]},
                ),
                mcp_types.Tool(
                    name="forum_get_post_detail",
                    description="获取帖子详情",
                    inputSchema={"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"]},
                ),
                mcp_types.Tool(
                    name="forum_get_post_comments",
                    description="获取帖子评论",
                    inputSchema={"type": "object", "properties": {"post_id": {"type": "integer"}, "page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}, "required": ["post_id"]},
                ),
                mcp_types.Tool(
                    name="forum_get_unread_messages",
                    description="获取未读私信",
                    inputSchema={"type": "object", "properties": {}},
                ),
                mcp_types.Tool(
                    name="forum_get_conversations",
                    description="获取会话列表",
                    inputSchema={"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}},
                ),
                mcp_types.Tool(
                    name="forum_get_conversation_messages",
                    description="获取会话消息",
                    inputSchema={"type": "object", "properties": {"conversation_id": {"type": "integer"}, "page": {"type": "integer", "default": 1}, "size": {"type": "integer", "default": 20}}, "required": ["conversation_id"]},
                ),
                mcp_types.Tool(
                    name="forum_send_direct_message",
                    description="发送私信",
                    inputSchema={"type": "object", "properties": {"agent_id": {"type": "integer"}, "content": {"type": "string"}}, "required": ["agent_id", "content"]},
                ),
                mcp_types.Tool(
                    name="forum_create_post",
                    description="创建帖子",
                    inputSchema={"type": "object", "properties": {"title": {"type": "string"}, "content": {"type": "string"}}, "required": ["title", "content"]},
                ),
                mcp_types.Tool(
                    name="forum_create_comment",
                    description="创建评论",
                    inputSchema={"type": "object", "properties": {"post_id": {"type": "integer"}, "content": {"type": "string"}, "parent_id": {"type": "integer", "default": 0}}, "required": ["post_id", "content"]},
                ),
                mcp_types.Tool(
                    name="forum_downvote",
                    description="点踩帖子",
                    inputSchema={"type": "object", "properties": {"post_id": {"type": "integer"}}, "required": ["post_id"]},
                ),
            ]
        # 去重
        seen = set()
        deduped = []
        for t in tools:
            if t.name not in seen:
                seen.add(t.name)
                deduped.append(t)
        return deduped

    # ── 工具调用 ──
    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[mcp_types.TextContent]:
        result = await _dispatch(name, arguments, challenge_code=challenge_code, forum_id=forum_id)
        return [mcp_types.TextContent(type="text", text=str(result))]

    return server


async def _dispatch(name: str, args: dict, *, challenge_code: str, forum_id: int) -> str:
    if name == "execute_command":
        from tools.shell import execute_command
        return await execute_command.ainvoke(args)

    if name == "execute_python":
        from tools.python_exec import execute_python
        return await execute_python.ainvoke(args)

    if name == "submit_flag" and challenge_code:
        from tools.platform_api import get_competition_tools_for_challenge
        tools = {t.name: t for t in get_competition_tools_for_challenge(challenge_code)}
        if "submit_flag" in tools:
            return await tools["submit_flag"].ainvoke(args)
        raise MCPToolInvocationError("submit_flag tool not available")

    if name == "run_level2_cve_poc":
        from tools.level2_cve_poc import run_level2_cve_poc
        return await run_level2_cve_poc.ainvoke(args)

    if name.startswith("forum_") and forum_id:
        from tools.forum_api import get_forum_tools_for_challenge
        tools = {t.name: t for t in get_forum_tools_for_challenge(forum_id)}
        if name in tools:
            return await tools[name].ainvoke(args)
        raise MCPToolInvocationError(f"{name} not available for forum_id={forum_id}")

    raise MCPToolInvocationError(f"unknown tool {name}")


async def _main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--challenge-code", default="")
    parser.add_argument("--forum-id", type=int, default=0)
    parsed = parser.parse_args()

    server = _make_server(challenge_code=parsed.challenge_code, forum_id=parsed.forum_id)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    asyncio.run(_main())
