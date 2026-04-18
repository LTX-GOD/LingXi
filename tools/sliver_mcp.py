"""
Sliver MCP 工具桥接
==================
通过 stdio 拉起 `sliver-client mcp --config <operator.cfg>`，并把官方 MCP tools
包装为 LangChain StructuredTool 供原创 Agent 直接调用。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from runtime_env import get_project_root

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*args, **kwargs):
        return False

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None

load_dotenv()

logger = logging.getLogger(__name__)
_SLIVER_MCP_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("SLIVER_MCP_MAX_WORKERS", "2") or 2)),
    thread_name_prefix="sliver-mcp",
)

_sliver_mcp_runner: Optional["SliverMCPRunner"] = None
_sliver_mcp_init_args: Dict[str, str] = {}


class SliverMCPError(Exception):
    """Sliver MCP 桥接异常。"""


def _resolve_project_path(value: str, *, default: str = "") -> str:
    raw = str(value or default or "").strip()
    if not raw:
        return ""
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(get_project_root(), raw))


def sliver_mcp_enabled() -> bool:
    enabled_raw = os.getenv("SLIVER_ENABLED")
    client_path = _resolve_project_path(
        os.getenv("SLIVER_CLIENT_PATH", "./bin/sliver-client")
    )
    client_config_path = _resolve_project_path(
        os.getenv("SLIVER_CLIENT_CONFIG", "./sliver-config")
    )
    if enabled_raw is None or not enabled_raw.strip():
        auto_enable = (
            str(os.getenv("SLIVER_AUTO_ENABLE_IF_PRESENT", "false")).strip().lower()
            == "true"
        )
        return auto_enable and os.path.exists(client_path) and os.path.exists(client_config_path)
    return enabled_raw.strip().lower() == "true"


def _json_schema_to_pydantic(name: str, schema: Dict[str, Any]) -> type[BaseModel]:
    properties = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    fields: Dict[str, Any] = {}

    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    for field_name, field_schema in properties.items():
        field_type = type_map.get(field_schema.get("type"), Any)
        default = ... if field_name in required else field_schema.get("default", None)
        description = str(field_schema.get("description", "") or "")
        fields[field_name] = (field_type, Field(default=default, description=description))

    if not fields:
        return create_model(name)
    return create_model(name, **fields)


def _tool_description_from_spec(spec: Any) -> str:
    raw = str(getattr(spec, "description", "") or getattr(spec, "name", "sliver_mcp_tool")).strip()
    prefix = (
        "Sliver MCP 工具。适用于已拿到落点后的会话管理、远程执行、文件搜索/下载、隧道与代理控制。"
        "拿到 shell/RCE 后优先使用，不要继续沉迷原始 Web exploit。"
    )
    return f"{prefix} {raw}".strip()


class SliverMCPRunner:
    """通过 stdio 拉起 sliver-client mcp，并在后台事件循环中复用连接。"""

    def __init__(
        self,
        client_path: str,
        client_config_path: str,
        client_root_dir: str = "",
    ):
        if ClientSession is None or StdioServerParameters is None or stdio_client is None:
            raise SliverMCPError("未安装 mcp SDK，无法接入 Sliver MCP")

        self.client_path = _resolve_project_path(client_path)
        self.client_config_path = _resolve_project_path(client_config_path)
        self.client_root_dir = _resolve_project_path(client_root_dir)
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._started = False
        self._init_error: Optional[BaseException] = None
        self._session = None
        self._tool_specs: Dict[str, Any] = {}
        self._shutdown_event = None

    def describe_transport(self) -> Dict[str, Any]:
        return {
            "protocol": "mcp-stdio->sliver-client",
            "client_path": self.client_path,
            "client_config_path": self.client_config_path,
            "client_root_dir": self.client_root_dir or "<default>",
        }

    def start(self):
        if self._started:
            return
        if not os.path.exists(self.client_path):
            raise SliverMCPError(f"Sliver client 不存在: {self.client_path}")
        if not os.path.exists(self.client_config_path):
            raise SliverMCPError(f"Sliver operator 配置不存在: {self.client_config_path}")
        self._ready.clear()
        self._init_error = None
        self._session = None
        self._tool_specs = {}
        self._started = True
        self._thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
            name="sliver-mcp-runner",
        )
        self._thread.start()
        self._ready.wait(timeout=20)
        if self._init_error is not None:
            self.stop()
            raise SliverMCPError(str(self._init_error))
        if not self._ready.is_set():
            self.stop()
            raise SliverMCPError("Sliver MCP 启动超时")

    def _thread_main(self):
        import asyncio

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._shutdown_event = asyncio.Event()
        self._loop.create_task(self._async_main())
        try:
            self._loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(self._loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _async_main(self):
        import asyncio
        import contextlib

        stack = contextlib.AsyncExitStack()
        try:
            env = dict(os.environ)
            env["SLIVER_NO_UPDATE_CHECK"] = env.get("SLIVER_NO_UPDATE_CHECK", "1")
            if self.client_root_dir:
                os.makedirs(self.client_root_dir, exist_ok=True)
                env["SLIVER_CLIENT_ROOT_DIR"] = self.client_root_dir

            params = StdioServerParameters(
                command=self.client_path,
                args=["mcp", "--config", self.client_config_path],
                env=env,
                cwd=str(get_project_root()),
            )

            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = ClientSession(read_stream, write_stream)
            self._session = await stack.enter_async_context(session)
            await self._session.initialize()
            tool_result = await self._session.list_tools()
            self._tool_specs = {tool.name: tool for tool in tool_result.tools}
            self._ready.set()
            await self._shutdown_event.wait()
        except BaseException as exc:
            self._init_error = exc
            self._ready.set()
        finally:
            self._session = None
            self._tool_specs = {}
            try:
                await stack.aclose()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[SliverMCP] 关闭 MCP 会话失败: %s", exc, exc_info=True)
            finally:
                if self._loop is not None and self._loop.is_running():
                    self._loop.call_soon(self._loop.stop)

    def ensure_started(self):
        if not self._started:
            self.start()

    def list_tool_specs(self) -> Dict[str, Any]:
        self.ensure_started()
        return dict(self._tool_specs)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        self.ensure_started()
        import asyncio

        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(name, arguments or {}),
            self._loop,
        )
        return future.result(timeout=60)

    async def _call_tool_async(self, name: str, arguments: Dict[str, Any]) -> str:
        if self._session is None:
            raise SliverMCPError("Sliver MCP 会话未初始化")
        result = await self._session.call_tool(name, arguments)
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            parts.append(str(text) if text is not None else str(item))
        text = "\n".join(part for part in parts if part).strip()
        if result.isError:
            raise SliverMCPError(text or f"MCP 工具 {name} 调用失败")
        return text

    def stop(self, timeout: float = 10.0):
        if not self._started:
            return

        thread = self._thread
        loop = self._loop
        shutdown_event = self._shutdown_event
        self._started = False

        if loop is not None and loop.is_running():
            try:
                if shutdown_event is not None:
                    loop.call_soon_threadsafe(shutdown_event.set)
                else:
                    loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("[SliverMCP] 后台线程未在 %.1fs 内退出", timeout)

        self._thread = None
        self._loop = None
        self._shutdown_event = None
        self._session = None
        self._tool_specs = {}


def _build_mcp_tool(spec: Any) -> StructuredTool:
    name = str(spec.name)
    schema = dict(getattr(spec, "inputSchema", {}) or {})
    args_schema = _json_schema_to_pydantic(f"SLIVER_MCP_{name}_Args", schema)

    async def _coroutine(**kwargs):
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                _SLIVER_MCP_EXECUTOR,
                get_sliver_mcp_runner().call_tool,
                name,
                kwargs,
            )
        except SliverMCPError as exc:
            logger.warning("[SliverMCP] 工具调用失败，尝试自动重连后重试: %s | %s", name, exc)
            try:
                await loop.run_in_executor(_SLIVER_MCP_EXECUTOR, reconnect_sliver_mcp)
                return await loop.run_in_executor(
                    _SLIVER_MCP_EXECUTOR,
                    get_sliver_mcp_runner().call_tool,
                    name,
                    kwargs,
                )
            except Exception as retry_exc:
                raise SliverMCPError(
                    f"Sliver MCP 工具调用失败: {exc} | 自动重连后仍失败: {retry_exc}"
                ) from retry_exc

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=name,
        description=_tool_description_from_spec(spec),
        args_schema=args_schema,
        infer_schema=False,
    )


def initialize_sliver_mcp(
    client_path: str = "",
    client_config_path: str = "",
    client_root_dir: str = "",
) -> SliverMCPRunner:
    global _sliver_mcp_runner, _sliver_mcp_init_args
    resolved_client_path = client_path or os.getenv("SLIVER_CLIENT_PATH", "./bin/sliver-client")
    resolved_client_config_path = client_config_path or os.getenv("SLIVER_CLIENT_CONFIG", "./sliver-config")
    resolved_client_root_dir = client_root_dir or os.getenv("SLIVER_CLIENT_ROOT_DIR", "./sliver-workdir")
    _sliver_mcp_init_args = {
        "client_path": resolved_client_path,
        "client_config_path": resolved_client_config_path,
        "client_root_dir": resolved_client_root_dir,
    }
    if _sliver_mcp_runner is not None:
        _sliver_mcp_runner.stop()
    _sliver_mcp_runner = SliverMCPRunner(
        client_path=resolved_client_path,
        client_config_path=resolved_client_config_path,
        client_root_dir=resolved_client_root_dir,
    )
    _sliver_mcp_runner.start()
    logger.info("[SliverMCP] 已连接 sliver-client mcp")
    return _sliver_mcp_runner


def get_sliver_mcp_runner() -> SliverMCPRunner:
    global _sliver_mcp_runner
    if _sliver_mcp_runner is None:
        _sliver_mcp_runner = initialize_sliver_mcp(
            _sliver_mcp_init_args.get("client_path", ""),
            _sliver_mcp_init_args.get("client_config_path", ""),
            _sliver_mcp_init_args.get("client_root_dir", ""),
        )
    return _sliver_mcp_runner


def shutdown_sliver_mcp():
    global _sliver_mcp_runner
    if _sliver_mcp_runner is None:
        return
    runner = _sliver_mcp_runner
    _sliver_mcp_runner = None
    runner.stop()


def reconnect_sliver_mcp() -> SliverMCPRunner:
    return initialize_sliver_mcp(
        _sliver_mcp_init_args.get("client_path", ""),
        _sliver_mcp_init_args.get("client_config_path", ""),
        _sliver_mcp_init_args.get("client_root_dir", ""),
    )


def get_sliver_mcp_tools() -> List:
    if not sliver_mcp_enabled():
        return []
    specs = get_sliver_mcp_runner().list_tool_specs()
    return [_build_mcp_tool(spec) for spec in specs.values()]
