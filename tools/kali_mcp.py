"""
Kali MCP 工具桥接
=================
在 Kali 容器内启动 kali-server-mcp HTTP server，
再通过 stdio 运行 client.py 连接，包装为 LangChain StructuredTool。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, Optional

from kali_container import (
    DEFAULT_KALI_CONTAINER_NAME,
    KaliContainerResolutionError,
    get_kali_container_name,
)
from pydantic import BaseModel, Field, create_model

try:
    from langchain_core.tools import StructuredTool
except ImportError:
    StructuredTool = None

logger = logging.getLogger(__name__)

_KALI_MCP_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("KALI_MCP_MAX_WORKERS", "2") or 2)),
    thread_name_prefix="kali-mcp",
)

_kali_mcp_runner: Optional["KaliMCPRunner"] = None

DEFAULT_KALI_SERVER_PORT = 5001
KALI_CLIENT_PATH = "/usr/share/mcp-kali-server/client.py"
_KALI_SERVER_CLEANUP_OK_CODES = {0, 143}

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


class KaliMCPError(Exception):
    pass


@dataclass
class _CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise KaliMCPError(f"环境变量 {name} 不是合法整数: {raw}") from exc


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise KaliMCPError(f"环境变量 {name} 不是合法数字: {raw}") from exc


def _resolve_kali_container_name(container: Optional[str] = None) -> str:
    try:
        return get_kali_container_name(container, strict=True, log=logger)
    except KaliContainerResolutionError as exc:
        raise KaliMCPError(str(exc)) from exc


def _kali_server_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _json_schema_to_pydantic(name: str, schema: Dict[str, Any]) -> type[BaseModel]:
    properties = dict(schema.get("properties") or {})
    required = set(schema.get("required") or [])
    type_map = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
    fields: Dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        field_type = type_map.get(field_schema.get("type"), Any)
        default = ... if field_name in required else field_schema.get("default", None)
        description = str(field_schema.get("description", "") or "")
        fields[field_name] = (field_type, Field(default=default, description=description))
    if not fields:
        return create_model(name)
    return create_model(name, **fields)


def _tool_description_from_spec(spec: Any, container: str) -> str:
    raw = str(getattr(spec, "description", "") or getattr(spec, "name", "kali_mcp_tool")).strip()
    prefix = (
        f"Kali Docker MCP 工具。运行位置: 本地 Docker 容器 `{container}`。"
        "适用于内网主机发现、端口/服务枚举、SMB/LDAP/Kerberos/WinRM/SSH/MSSQL 认证测试、AD 枚举与横向移动。"
    )
    return f"{prefix} {raw}".strip()


class KaliMCPRunner:
    """在 Kali 容器内启动 HTTP server，再通过 stdio 运行 client.py。"""

    def __init__(self, container: Optional[str] = None, port: Optional[int] = None):
        if ClientSession is None:
            raise KaliMCPError("未安装 mcp SDK")
        self.container = _resolve_kali_container_name(container)
        self.port = _env_int("KALI_MCP_SERVER_PORT", DEFAULT_KALI_SERVER_PORT) if port is None else int(port)
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._started = False
        self._init_error: Optional[BaseException] = None
        self._session = None
        self._tool_specs: Dict[str, Any] = {}
        self._shutdown_event = None
        self._startup_timeout = _env_float("KALI_MCP_START_TIMEOUT", 30.0)
        self._healthcheck_interval = _env_float("KALI_MCP_HEALTHCHECK_INTERVAL", 0.5)
        self._server_proc = None
        self._server_wait_task = None
        self._server_stream_tasks: list[Any] = []
        self._server_output: deque[str] = deque(maxlen=max(20, _env_int("KALI_MCP_SERVER_LOG_LINES", 80)))

    def start(self):
        if self._started:
            return
        self._ready.clear()
        self._init_error = None
        self._started = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="kali-mcp-runner")
        self._thread.start()
        self._ready.wait(timeout=self._startup_timeout + 5.0)
        if self._init_error is not None:
            self.stop()
            raise KaliMCPError(str(self._init_error))
        if not self._ready.is_set():
            self.stop()
            raise KaliMCPError("Kali MCP 启动超时")

    def _thread_main(self):
        import asyncio
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._shutdown_event = asyncio.Event()
        self._loop.create_task(self._async_main())
        try:
            self._loop.run_forever()
        finally:
            pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _run_command(self, *args: str, timeout: float = 15.0) -> _CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise KaliMCPError(f"命令执行超时: {shlex.join(args)}") from exc
        return _CommandResult(
            args=tuple(args),
            returncode=process.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )

    async def _docker_exec(self, *args: str, timeout: float = 15.0) -> _CommandResult:
        return await self._run_command("docker", "exec", self.container, *args, timeout=timeout)

    async def _docker_bash(self, script: str, timeout: float = 15.0) -> _CommandResult:
        return await self._docker_exec("bash", "-lc", script, timeout=timeout)

    async def _ensure_container_running(self) -> None:
        result = await self._run_command(
            "docker",
            "inspect",
            "--format",
            "{{.State.Running}}",
            self.container,
            timeout=10.0,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise KaliMCPError(f"Kali 容器不可用: {self.container} | {detail or 'docker inspect 失败'}")
        if result.stdout.strip().lower() != "true":
            raise KaliMCPError(f"Kali 容器未运行: {self.container}")

    async def _ensure_container_prerequisites(self) -> None:
        checks = [
            ("python3", "command -v python3 >/dev/null", "容器内未找到 python3"),
            ("kali-server-mcp", "command -v kali-server-mcp >/dev/null", "容器内未找到 kali-server-mcp"),
            (
                KALI_CLIENT_PATH,
                f"test -f {shlex.quote(KALI_CLIENT_PATH)}",
                f"容器内缺少 Kali MCP client: {KALI_CLIENT_PATH}",
            ),
        ]
        for _, script, error_message in checks:
            result = await self._docker_bash(script, timeout=10.0)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise KaliMCPError(f"{error_message} | {detail or '检查失败'}")

    def _server_command(self) -> list[str]:
        return ["kali-server-mcp", "--port", str(self.port), "--ip", "0.0.0.0"]

    def _server_match_pattern(self) -> str:
        return f"kali-server-mcp|server.py.*{self.port}"

    async def _capture_server_stream(self, stream, label: str) -> None:
        if stream is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if not text:
                continue
            entry = f"{label}: {text}"
            self._server_output.append(entry)
            logger.debug("[KaliMCP][server][%s] %s", label, text)

    def _server_log_tail(self, limit: int = 12) -> str:
        if not self._server_output:
            return ""
        return " | ".join(list(self._server_output)[-limit:])

    async def _probe_server_health(self) -> tuple[bool, str]:
        script = (
            "import http.client, sys; "
            f"conn = http.client.HTTPConnection('127.0.0.1', {self.port}, timeout=2); "
            "conn.request('GET', '/'); "
            "resp = conn.getresponse(); "
            "sys.exit(0 if 100 <= resp.status < 600 else 1)"
        )
        result = await self._docker_exec("python3", "-c", script, timeout=5.0)
        if result.returncode == 0:
            return True, "ok"
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"healthcheck exit={result.returncode}"

    async def _wait_for_server_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            if self._server_wait_task is not None and self._server_wait_task.done():
                returncode = await self._server_wait_task
                detail = self._server_log_tail()
                message = (
                    f"Kali MCP server 提前退出: container={self.container} "
                    f"port={self.port} returncode={returncode}"
                )
                if detail:
                    message = f"{message} | {detail}"
                raise KaliMCPError(message)

            healthy, detail = await self._probe_server_health()
            if healthy:
                return
            last_error = detail
            await asyncio.sleep(self._healthcheck_interval)

        detail = self._server_log_tail()
        message = f"Kali MCP server 健康检查超时: container={self.container} port={self.port}"
        if last_error:
            message = f"{message} | last_probe={last_error}"
        if detail:
            message = f"{message} | {detail}"
        raise KaliMCPError(message)

    async def _start_server(self) -> None:
        stop_result = await self._docker_bash(
            f"pkill -f {shlex.quote(self._server_match_pattern())} 2>/dev/null || true",
            timeout=10.0,
        )
        if stop_result.returncode not in _KALI_SERVER_CLEANUP_OK_CODES:
            detail = (stop_result.stderr or stop_result.stdout or "").strip()
            raise KaliMCPError(f"清理旧的 Kali MCP server 失败: {detail or stop_result.returncode}")

        self._server_output.clear()
        self._server_proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            self.container,
            "bash",
            "-lc",
            f"exec {shlex.join(self._server_command())}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._server_wait_task = asyncio.create_task(self._server_proc.wait())
        self._server_stream_tasks = [
            asyncio.create_task(self._capture_server_stream(self._server_proc.stdout, "stdout")),
            asyncio.create_task(self._capture_server_stream(self._server_proc.stderr, "stderr")),
        ]
        await self._wait_for_server_ready()

    async def _stop_server(self) -> None:
        stop_script = f"pkill -f {shlex.quote(self._server_match_pattern())} 2>/dev/null || true"
        try:
            await self._docker_bash(stop_script, timeout=5.0)
        except Exception:
            pass

        if self._server_wait_task is not None and not self._server_wait_task.done():
            try:
                await asyncio.wait_for(self._server_wait_task, timeout=5.0)
            except asyncio.TimeoutError:
                if self._server_proc is not None:
                    self._server_proc.terminate()
                try:
                    await asyncio.wait_for(self._server_wait_task, timeout=5.0)
                except asyncio.TimeoutError:
                    if self._server_proc is not None:
                        self._server_proc.kill()
                    await asyncio.gather(self._server_wait_task, return_exceptions=True)

        for task in self._server_stream_tasks:
            task.cancel()
        if self._server_stream_tasks:
            await asyncio.gather(*self._server_stream_tasks, return_exceptions=True)
        self._server_stream_tasks = []
        self._server_wait_task = None
        self._server_proc = None

    async def _async_main(self):
        import contextlib

        stack = contextlib.AsyncExitStack()
        try:
            await self._ensure_container_running()
            await self._ensure_container_prerequisites()
            await self._start_server()

            # 通过 docker exec -i 运行 client.py 作为 stdio 子进程
            params = StdioServerParameters(
                command="docker",
                args=["exec", "-i", self.container, "python3", KALI_CLIENT_PATH,
                      "--server", _kali_server_url(self.port)],
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            session = ClientSession(read_stream, write_stream)
            self._session = await stack.enter_async_context(session)
            await self._session.initialize()
            tool_result = await self._session.list_tools()
            self._tool_specs = {tool.name: tool for tool in tool_result.tools}
            logger.info(
                "[KaliMCP] 已连接: container=%s port=%s tools=%d",
                self.container,
                self.port,
                len(self._tool_specs),
            )
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
            except Exception:
                pass
            finally:
                await self._stop_server()
                if self._loop is not None and self._loop.is_running():
                    self._loop.call_soon(self._loop.stop)

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        import asyncio
        future = asyncio.run_coroutine_threadsafe(
            self._call_tool_async(name, arguments or {}), self._loop
        )
        return future.result(timeout=120)

    async def _call_tool_async(self, name: str, arguments: Dict[str, Any]) -> str:
        if self._session is None:
            raise KaliMCPError("Kali MCP 会话未初始化")
        result = await self._session.call_tool(name, arguments)
        parts = [str(getattr(item, "text", item)) for item in result.content]
        text = "\n".join(p for p in parts if p).strip()
        if result.isError:
            raise KaliMCPError(text or f"MCP 工具 {name} 调用失败")
        return text

    def list_tool_specs(self) -> Dict[str, Any]:
        if not self._started:
            self.start()
        return dict(self._tool_specs)

    def stop(self, timeout: float = 10.0):
        if not self._started:
            return
        loop, thread, ev = self._loop, self._thread, self._shutdown_event
        self._started = False
        if loop is not None and loop.is_running():
            try:
                if ev is not None:
                    loop.call_soon_threadsafe(ev.set)
                else:
                    loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = self._loop = self._shutdown_event = self._session = None
        self._tool_specs = {}


def _build_kali_tool(spec: Any, container: str) -> StructuredTool:
    if StructuredTool is None:
        raise KaliMCPError("未安装 langchain_core")
    name = str(spec.name)
    schema = dict(getattr(spec, "inputSchema", {}) or {})
    args_schema = _json_schema_to_pydantic(f"KALI_MCP_{name}_Args", schema)

    async def _coroutine(**kwargs):
        import asyncio
        loop = asyncio.get_running_loop()
        runner = get_kali_mcp_runner()
        try:
            return await loop.run_in_executor(
                _KALI_MCP_EXECUTOR, runner.call_tool, name, kwargs
            )
        except KaliMCPError as exc:
            logger.warning("[KaliMCP] 工具调用失败，尝试重连: %s | %s", name, exc)
            try:
                await loop.run_in_executor(
                    _KALI_MCP_EXECUTOR,
                    initialize_kali_mcp,
                    runner.container,
                    runner.port,
                )
                return await loop.run_in_executor(
                    _KALI_MCP_EXECUTOR, get_kali_mcp_runner().call_tool, name, kwargs
                )
            except Exception as retry_exc:
                raise KaliMCPError(
                    f"Kali MCP 工具调用失败: {exc} | 重连后仍失败: {retry_exc}"
                ) from retry_exc

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=name,
        description=_tool_description_from_spec(spec, container),
        args_schema=args_schema,
        infer_schema=False,
    )


def initialize_kali_mcp(container: Optional[str] = None, port: Optional[int] = None) -> KaliMCPRunner:
    global _kali_mcp_runner
    if _kali_mcp_runner is not None:
        _kali_mcp_runner.stop()
    _kali_mcp_runner = KaliMCPRunner(container=container, port=port)
    _kali_mcp_runner.start()
    logger.info("[KaliMCP] 已连接 kali-server-mcp: container=%s port=%s", _kali_mcp_runner.container, _kali_mcp_runner.port)
    return _kali_mcp_runner


def get_kali_mcp_runner() -> KaliMCPRunner:
    global _kali_mcp_runner
    if _kali_mcp_runner is None:
        _kali_mcp_runner = initialize_kali_mcp()
    return _kali_mcp_runner


def shutdown_kali_mcp():
    global _kali_mcp_runner
    if _kali_mcp_runner is None:
        return
    runner, _kali_mcp_runner = _kali_mcp_runner, None
    runner.stop()


def get_kali_mcp_tools() -> list[StructuredTool]:
    """返回 Kali MCP 工具列表，失败时返回空列表。"""
    try:
        runner = get_kali_mcp_runner()
        return [_build_kali_tool(spec, runner.container) for spec in runner.list_tool_specs().values()]
    except Exception as exc:
        logger.warning("[KaliMCP] 获取工具列表失败: %s", exc)
        return []
