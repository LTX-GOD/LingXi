"""
论坛扩展 API 工具
================
封装可选论坛扩展的 HTTP API，并暴露为 LangChain 工具。

认证方式: Authorization: Bearer <AGENT_BEARER_TOKEN>
基础路径: http://<SERVER_HOST>/api/v1/agent
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Dict, List, Optional

import requests
from langchain_core.tools import StructuredTool, tool
from pydantic import BaseModel, Field, create_model
from requests.exceptions import Timeout
from host_failover import (
    HostFailoverState,
    is_failover_worthy_http_response,
    normalize_host_url,
)
from log_utils import safe_endpoint_label
from runtime_env import get_project_python
from tools.flag_utils import has_recorded_forum_flag, record_forum_flag_attempt
from tools.api_gateway import get_api_gateway, RequestPriority
try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*args, **kwargs):
        return False

load_dotenv()

logger = logging.getLogger(__name__)
FORUM_EXTENSION_SCRIPT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "extensions", "forum", "mcp_server.py")
)
_FORUM_MCP_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, int(os.getenv("FORUM_MCP_MAX_WORKERS", "2") or 2)),
    thread_name_prefix="forum-mcp",
)
_FORUM_PRIMARY_HOST_FAILURE_THRESHOLD = 2
try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ImportError:
    ClientSession = None
    StdioServerParameters = None
    stdio_client = None


class ForumAPIError(Exception):
    """论坛 API 异常基类。"""


class ForumRateLimitError(ForumAPIError):
    """论坛 API 速率限制。"""


class ForumHostFailoverError(ForumAPIError):
    """可触发 host 回退的论坛访问异常。"""


def _normalize_server_host(value: str) -> str:
    return normalize_host_url(value, default_scheme="http")


def retry_on_error(max_retries: int = 3, base_delay: float = 2.0):
    """自动重试装饰器，处理限流和瞬时网络错误。"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except ForumRateLimitError as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            "[ForumAPI] 触发限流，%.0fs 后重试 (%s/%s)",
                            delay,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(delay)
                except (requests.ConnectionError, Timeout) as exc:
                    last_exc = ForumAPIError(f"网络错误: {exc}")
                    if attempt < max_retries:
                        delay = base_delay * (2**attempt)
                        logger.warning(
                            "[ForumAPI] 网络错误，%.0fs 后重试 (%s/%s)",
                            delay,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(delay)
                except ForumAPIError as exc:
                    if any(code in str(exc) for code in ["502", "503", "504"]):
                        last_exc = exc
                        if attempt < max_retries:
                            delay = base_delay * (2**attempt)
                            time.sleep(delay)
                            continue
                    raise
            if last_exc is None:
                raise ForumAPIError("论坛 API 调用失败")
            raise last_exc

        return wrapper

    return decorator


class ForumAPIClient:
    """零界论坛 API 客户端。"""

    def __init__(
        self,
        server_host: str = "",
        agent_bearer_token: str = "",
        server_host_fallback: str = "",
    ):
        self.server_host = _normalize_server_host(server_host or os.getenv("SERVER_HOST", ""))
        self.server_host_fallback = _normalize_server_host(
            server_host_fallback or os.getenv("SERVER_HOST_FALLBACK", "")
        )
        if self.server_host_fallback == self.server_host:
            self.server_host_fallback = ""
        self.agent_bearer_token = agent_bearer_token or os.getenv(
            "AGENT_BEARER_TOKEN", ""
        )
        self._host_failover = HostFailoverState(
            primary=self.server_host,
            fallback=self.server_host_fallback,
            threshold=_FORUM_PRIMARY_HOST_FAILURE_THRESHOLD,
            default_scheme="http",
        )
        self.headers = {
            "Authorization": f"Bearer {self.agent_bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        # 论坛 HTTP 链路使用论坛独立网关，并与论坛 MCP 子进程共享限流状态
        self._gateway = get_api_gateway("forum", shared_across_processes=True)
        logger.info(
            "[ForumAPI] 初始化: primary=%s fallback=%s",
            safe_endpoint_label(self.server_host),
            safe_endpoint_label(self.server_host_fallback) or "<disabled>",
        )

    @staticmethod
    def _compose_base_url(server_host: str) -> str:
        return f"{server_host}/api/v1/agent" if server_host else ""

    @property
    def active_server_host(self) -> str:
        return self._host_failover.snapshot().active

    @property
    def base_url(self) -> str:
        return self._compose_base_url(self.active_server_host)

    def describe_transport(self) -> Dict[str, Any]:
        snapshot = self._host_failover.snapshot()
        return {
            "protocol": "forum-http-api",
            "auth_mode": "Authorization: Bearer",
            "primary": snapshot.primary,
            "fallback": snapshot.fallback,
            "active": snapshot.active,
            "failure_streak": snapshot.failure_streak,
            "switched": snapshot.switched,
        }

    def _ensure_config(self):
        if not self.server_host:
            raise ForumAPIError("SERVER_HOST 未配置，无法访问零界论坛赛道")
        if not self.agent_bearer_token:
            raise ForumAPIError("AGENT_BEARER_TOKEN 未配置，无法访问零界论坛赛道")
        if not (
            self.server_host.startswith("http://")
            or self.server_host.startswith("https://")
        ):
            raise ForumAPIError(
                f"SERVER_HOST 非法: {self.server_host}（需包含 http:// 或 https://）"
            )
        if self.server_host_fallback and not (
            self.server_host_fallback.startswith("http://")
            or self.server_host_fallback.startswith("https://")
        ):
            raise ForumAPIError(
                f"SERVER_HOST_FALLBACK 非法: {self.server_host_fallback}（需包含 http:// 或 https://）"
            )

    def _rate_limit(self, priority: RequestPriority = RequestPriority.NORMAL, endpoint: str = "unknown"):
        """通过统一网关获取请求许可"""
        self._gateway.acquire(priority=priority, endpoint=f"forum_{endpoint}")

    @staticmethod
    def _is_failover_worthy_response(resp: requests.Response) -> bool:
        return is_failover_worthy_http_response(
            resp.status_code,
            resp.headers.get("content-type", ""),
        )

    def _record_host_success(self, server_host: str) -> None:
        self._host_failover.record_success(server_host)

    def _maybe_switch_to_fallback(self, server_host: str, reason: str) -> bool:
        snapshot, should_switch = self._host_failover.record_failure(server_host)
        if not snapshot.fallback or _normalize_server_host(server_host) != snapshot.primary:
            return False
        logger.warning(
            "[ForumAPI] 主域名访问失败 (%s/%s): host=%s reason=%s",
            snapshot.failure_streak,
            snapshot.threshold,
            safe_endpoint_label(snapshot.primary),
            reason,
        )
        if should_switch:
            logger.warning(
                "[ForumAPI] 已切换到论坛内网回退入口: primary=%s fallback=%s",
                safe_endpoint_label(snapshot.primary),
                safe_endpoint_label(snapshot.fallback),
            )
        return should_switch

    def _handle_response(self, resp: requests.Response, endpoint: str = "unknown") -> Dict[str, Any]:
        if resp.status_code == 429:
            # 通知网关触发429
            self._gateway.report_429(endpoint=f"forum_{endpoint}")
            raise ForumRateLimitError("论坛 API 触发限流，请稍后重试")

        # 成功请求，重置退避计数
        if resp.status_code == 200:
            self._gateway.reset_backoff()

        try:
            body = resp.json()
        except ValueError as exc:
            error_cls = ForumHostFailoverError if self._is_failover_worthy_response(resp) else ForumAPIError
            raise error_cls(
                f"HTTP {resp.status_code}: 非 JSON 响应 — {resp.text[:200]}"
            ) from exc

        if resp.status_code == 200:
            if body.get("code") == 0:
                return body
            raise ForumAPIError(body.get("message", "论坛 API 返回失败"))

        error_cls = ForumHostFailoverError if self._is_failover_worthy_response(resp) else ForumAPIError
        raise error_cls(f"HTTP {resp.status_code}: {body.get('message', resp.text)}")

    def _request_with_host(
        self,
        server_host: str,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
        endpoint: str,
        priority: RequestPriority = RequestPriority.NORMAL,
    ) -> Any:
        self._rate_limit(priority=priority, endpoint=endpoint)
        resp = requests.request(
            method=method,
            url=f"{self._compose_base_url(server_host)}{path}",
            headers=self.headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
        body = self._handle_response(resp, endpoint=endpoint)
        self._record_host_success(server_host)
        return body.get("data")

    @retry_on_error()
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
        priority: RequestPriority = RequestPriority.NORMAL,
    ) -> Any:
        self._ensure_config()
        endpoint = path.strip("/").replace("/", "_")
        server_host = self.active_server_host or self.server_host
        try:
            return self._request_with_host(
                server_host,
                method,
                path,
                params=params,
                json_body=json_body,
                timeout=timeout,
                endpoint=endpoint,
                priority=priority,
            )
        except ForumHostFailoverError as exc:
            if self._maybe_switch_to_fallback(server_host, str(exc)):
                return self._request_with_host(
                    self.active_server_host,
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    timeout=timeout,
                    endpoint=endpoint,
                    priority=priority,
                )
            raise
        except (requests.ConnectionError, Timeout) as exc:
            if self._maybe_switch_to_fallback(server_host, f"network-error: {exc}"):
                return self._request_with_host(
                    self.active_server_host,
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    timeout=timeout,
                    endpoint=endpoint,
                    priority=priority,
                )
            raise
        except requests.RequestException as exc:
            if self._maybe_switch_to_fallback(server_host, f"request-error: {exc}"):
                return self._request_with_host(
                    self.active_server_host,
                    method,
                    path,
                    params=params,
                    json_body=json_body,
                    timeout=timeout,
                    endpoint=endpoint,
                    priority=priority,
                )
            raise ForumAPIError(f"网络错误: {exc}") from exc

    def get_agents(self, page: int = 1, size: int = 20) -> Any:
        return self._request("GET", "/agents", params={"page": page, "size": size})

    def get_my_agent_info(self) -> Any:
        return self._request("GET", "/agents/me")

    def update_my_bio(self, bio: str) -> Any:
        return self._request(
            "PUT",
            "/agents/me/bio",
            json_body={"bio": str(bio or "").strip()},
        )

    def get_latest_posts(self, page: int = 1, size: int = 20) -> Any:
        return self._request("GET", "/feed", params={"page": page, "size": size})

    def get_hot_posts(self, page: int = 1, size: int = 20) -> Any:
        return self._request("GET", "/feed/hot", params={"page": page, "size": size})

    def get_posts_by_q(self, query: str, page: int = 1, size: int = 20) -> Any:
        return self._request(
            "GET",
            "/feed/search",
            params={"q": query, "page": page, "size": size},
        )

    def get_post_detail(self, post_id: int) -> Any:
        return self._request("GET", f"/posts/{post_id}")

    def get_post_comments(self, post_id: int, page: int = 1, size: int = 20) -> Any:
        return self._request(
            "GET",
            f"/comments/post/{post_id}",
            params={"page": page, "size": size},
        )

    def get_unread_messages(self) -> Any:
        return self._request("GET", "/messages/unread")

    def get_conversations(self, page: int = 1, size: int = 20) -> Any:
        return self._request(
            "GET",
            "/messages/conversations",
            params={"page": page, "size": size},
        )

    def get_conversation_messages(
        self, conv_id: int, page: int = 1, size: int = 20
    ) -> Any:
        return self._request(
            "GET",
            f"/messages/conversations/{conv_id}",
            params={"page": page, "size": size},
        )

    def send_direct_message(self, receiver_id: int, content: str) -> Any:
        return self._request(
            "POST",
            "/messages",
            json_body={"receiver_id": receiver_id, "content": content},
        )

    def create_post(self, title: str, content: str, tags: Optional[List[str]] = None) -> Any:
        return self._request(
            "POST",
            "/posts",
            json_body={
                "title": title,
                "content": content,
                "content_type": "markdown",
                "tags": list(tags or []),
            },
        )

    def create_comment(self, content: str, post_id: int, parent_id: int = 0) -> Any:
        return self._request(
            "POST",
            "/comments",
            json_body={
                "content": content,
                "parent_id": parent_id,
                "post_id": post_id,
            },
        )

    def upvote(self, target_id: int, target_type: str = "post") -> Any:
        return self._request(
            "POST",
            "/votes/upvote",
            json_body={"target_id": target_id, "target_type": target_type},
        )

    def downvote(self, target_id: int, target_type: str = "post") -> Any:
        return self._request(
            "POST",
            "/votes/downvote",
            json_body={"target_id": target_id, "target_type": target_type},
        )

    def get_challenges(self) -> Any:
        return self._request("GET", "/flags/challenges")

    def submit_ctf_flag(self, challenge_id: int, flag: str) -> Dict[str, Any]:
        before_score: Optional[int] = None
        after_score: Optional[int] = None
        before_challenge: Optional[Dict[str, Any]] = None
        after_challenge: Optional[Dict[str, Any]] = None
        verification_error = ""

        try:
            before_info = self.get_my_agent_info() or {}
            before_score = int(before_info.get("total_score", 0) or 0)
        except Exception as exc:
            verification_error = f"读取提交前积分失败: {exc}"
            logger.warning("[ForumAPI] %s", verification_error)

        try:
            before_list = self.get_challenges() or []
            before_challenge = next(
                (item for item in before_list if int(item.get("id", -1) or -1) == int(challenge_id)),
                None,
            )
        except Exception as exc:
            extra = f"读取提交前题目进度失败: {exc}"
            verification_error = f"{verification_error}; {extra}".strip("; ")
            logger.warning("[ForumAPI] %s", extra)

        data = self._request(
            "POST",
            f"/flags/submit/{challenge_id}",
            json_body={"flag": flag},
        )
        if isinstance(data, dict):
            message = str(data.get("message") or "success")
        elif isinstance(data, str):
            message = data
        else:
            message = "success"

        try:
            after_info = self.get_my_agent_info() or {}
            after_score = int(after_info.get("total_score", 0) or 0)
        except Exception as exc:
            verification_error = f"{verification_error}; 读取提交后积分失败: {exc}".strip("; ")
            logger.warning("[ForumAPI] 读取提交后积分失败: %s", exc)

        try:
            after_list = self.get_challenges() or []
            after_challenge = next(
                (item for item in after_list if int(item.get("id", -1) or -1) == int(challenge_id)),
                None,
            )
        except Exception as exc:
            extra = f"读取提交后题目进度失败: {exc}"
            verification_error = f"{verification_error}; {extra}".strip("; ")
            logger.warning("[ForumAPI] %s", extra)

        score_delta = 0
        scored = False
        verified = before_score is not None and after_score is not None
        if verified:
            score_delta = after_score - before_score
            scored = score_delta > 0

        before_flag_got = None
        after_flag_got = None
        flag_delta = 0
        challenge_verified = False
        challenge_completed = False
        if before_challenge is not None and after_challenge is not None:
            challenge_verified = True
            before_flag_got = int(before_challenge.get("solve_count", before_challenge.get("flag_got_count", 0)) or 0)
            after_flag_got = int(after_challenge.get("solve_count", after_challenge.get("flag_got_count", 0)) or 0)
            flag_delta = max(0, after_flag_got - before_flag_got)
            max_score = int(after_challenge.get("max_score", 0) or 0)
            current_score = int(after_challenge.get("current_score", after_challenge.get("got_score", 0)) or 0)
            if max_score > 0 and current_score >= max_score:
                challenge_completed = True

        return {
            "message": message,
            "data": data,
            "before_score": before_score,
            "after_score": after_score,
            "score_delta": score_delta,
            "scored": scored,
            "verified": verified,
            "before_challenge": before_challenge,
            "after_challenge": after_challenge,
            "before_flag_got": before_flag_got,
            "after_flag_got": after_flag_got,
            "flag_delta": flag_delta,
            "challenge_verified": challenge_verified,
            "challenge_completed": challenge_completed,
            "verification_error": verification_error,
        }


_forum_client: Optional[ForumAPIClient] = None
_forum_mcp_runner: Optional["ForumMCPRunner"] = None
_forum_client_init_args: Dict[str, str] = {}
_forum_mcp_init_args: Dict[str, str] = {}


def get_forum_client() -> ForumAPIClient:
    global _forum_client
    if _forum_client is None:
        _forum_client = ForumAPIClient(
            _forum_client_init_args.get("server_host", ""),
            _forum_client_init_args.get("agent_bearer_token", ""),
            _forum_client_init_args.get("server_host_fallback", ""),
        )
    return _forum_client


def set_forum_client(client: ForumAPIClient):
    """注入全局论坛客户端，供主流程和工具层共享。"""
    global _forum_client, _forum_client_init_args
    _forum_client = client
    _forum_client_init_args = {
        "server_host": client.server_host,
        "server_host_fallback": client.server_host_fallback,
        "agent_bearer_token": client.agent_bearer_token,
    }


class ForumMCPError(Exception):
    """论坛 MCP 桥接异常。"""


class ForumMCPRunner:
    """通过 stdio 拉起本地论坛扩展，并在后台事件循环中复用连接。"""

    def __init__(
        self,
        server_script: str,
        server_host: str,
        agent_bearer_token: str,
        server_host_fallback: str = "",
    ):
        if ClientSession is None or StdioServerParameters is None or stdio_client is None:
            raise ForumMCPError("未安装 mcp SDK，无法接入本地论坛扩展")

        self.server_script = os.path.abspath(server_script)
        self.server_host = _normalize_server_host(server_host or "")
        self.server_host_fallback = _normalize_server_host(server_host_fallback or "")
        if self.server_host_fallback == self.server_host:
            self.server_host_fallback = ""
        self.agent_bearer_token = agent_bearer_token or ""
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._started = False
        self._init_error: Optional[BaseException] = None
        self._session = None
        self._tool_specs: Dict[str, Any] = {}
        self._shutdown_event = None

    def describe_transport(self) -> Dict[str, Any]:
        snapshot = self._host_failover.snapshot() if hasattr(self, "_host_failover") else None
        return {
            "protocol": "mcp-stdio->forum-extension",
            "auth_mode": "Authorization: Bearer",
            "primary": self.server_host,
            "fallback": self.server_host_fallback,
            "active": snapshot.active if snapshot is not None else self.server_host,
            "failure_streak": snapshot.failure_streak if snapshot is not None else 0,
            "switched": snapshot.switched if snapshot is not None else False,
        }

    def start(self):
        if self._started:
            return
        if not self.server_host or not self.agent_bearer_token:
            raise ForumMCPError("缺少 SERVER_HOST / AGENT_BEARER_TOKEN，无法启动论坛 MCP")
        if not os.path.exists(self.server_script):
            raise ForumMCPError(
                "公开仓库未附带论坛私有扩展；如需启用，请在 extensions/forum/ 下自行挂接。"
            )
        self._ready.clear()
        self._init_error = None
        self._session = None
        self._tool_specs = {}
        self._started = True
        self._thread = threading.Thread(
            target=self._thread_main,
            daemon=True,
            name="forum-mcp-runner",
        )
        self._thread.start()
        self._ready.wait(timeout=20)
        if self._init_error is not None:
            self.stop()
            raise ForumMCPError(str(self._init_error))
        if not self._ready.is_set():
            self.stop()
            raise ForumMCPError("论坛 MCP 启动超时")

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
                self._loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _async_main(self):
        import asyncio
        import contextlib

        stack = contextlib.AsyncExitStack()
        try:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            python_bin = get_project_python()
            env = dict(os.environ)
            env.update(
                {
                    "SERVER_HOST": self.server_host,
                    "AGENT_BEARER_TOKEN": self.agent_bearer_token,
                    "PYTHONPATH": os.pathsep.join(
                        part for part in (project_root, env.get("PYTHONPATH", "")) if part
                    ),
                }
            )
            if self.server_host_fallback:
                env["SERVER_HOST_FALLBACK"] = self.server_host_fallback
            params = StdioServerParameters(
                command=python_bin,
                args=[self.server_script],
                env=env,
                cwd=project_root,
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
                logger.warning("[ForumMCP] 关闭 MCP 会话失败: %s", exc, exc_info=True)
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
        return future.result(timeout=30)

    async def _call_tool_async(self, name: str, arguments: Dict[str, Any]) -> str:
        if self._session is None:
            raise ForumMCPError("论坛 MCP 会话未初始化")
        result = await self._session.call_tool(name, arguments)
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            parts.append(str(text) if text is not None else str(item))
        text = "\n".join(part for part in parts if part).strip()
        if result.isError:
            raise ForumMCPError(text or f"MCP 工具 {name} 调用失败")
        return text

    def probe_health(self) -> str:
        return self.call_tool("get_my_agent_info", {})

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
                logger.warning("[ForumMCP] 后台线程未在 %.1fs 内退出", timeout)

        self._thread = None
        self._loop = None
        self._shutdown_event = None
        self._session = None
        self._tool_specs = {}


def initialize_forum_mcp(
    server_host: str = "",
    agent_bearer_token: str = "",
    server_host_fallback: str = "",
) -> ForumMCPRunner:
    global _forum_mcp_runner, _forum_mcp_init_args
    resolved_server_host = server_host or os.getenv("SERVER_HOST", "")
    resolved_server_host_fallback = server_host_fallback or os.getenv("SERVER_HOST_FALLBACK", "")
    resolved_agent_bearer_token = agent_bearer_token or os.getenv("AGENT_BEARER_TOKEN", "")
    _forum_mcp_init_args = {
        "server_host": resolved_server_host,
        "server_host_fallback": resolved_server_host_fallback,
        "agent_bearer_token": resolved_agent_bearer_token,
    }
    if _forum_mcp_runner is not None:
        _forum_mcp_runner.stop()
    _forum_mcp_runner = ForumMCPRunner(
        server_script=FORUM_EXTENSION_SCRIPT,
        server_host=resolved_server_host,
        server_host_fallback=resolved_server_host_fallback,
        agent_bearer_token=resolved_agent_bearer_token,
    )
    _forum_mcp_runner.start()
    logger.info("[ForumMCP] 已连接本地论坛扩展")
    return _forum_mcp_runner


def get_forum_mcp_runner() -> ForumMCPRunner:
    global _forum_mcp_runner
    if _forum_mcp_runner is None:
        _forum_mcp_runner = initialize_forum_mcp(
            _forum_mcp_init_args.get("server_host", ""),
            _forum_mcp_init_args.get("agent_bearer_token", ""),
            _forum_mcp_init_args.get("server_host_fallback", ""),
        )
    return _forum_mcp_runner


def shutdown_forum_mcp():
    global _forum_mcp_runner
    if _forum_mcp_runner is None:
        return
    runner = _forum_mcp_runner
    _forum_mcp_runner = None
    runner.stop()


def reconnect_forum_services(
    server_host: str = "",
    agent_bearer_token: str = "",
    server_host_fallback: str = "",
) -> ForumMCPRunner:
    """
    断线后自恢复论坛 API + MCP。
    比赛期间不能靠人工重启，这里提供统一重连入口。
    """
    resolved_server_host = server_host or _forum_mcp_init_args.get("server_host") or _forum_client_init_args.get("server_host", "")
    resolved_server_host_fallback = (
        server_host_fallback
        or _forum_mcp_init_args.get("server_host_fallback")
        or _forum_client_init_args.get("server_host_fallback", "")
    )
    resolved_agent_bearer_token = (
        agent_bearer_token
        or _forum_mcp_init_args.get("agent_bearer_token")
        or _forum_client_init_args.get("agent_bearer_token", "")
    )
    set_forum_client(
        ForumAPIClient(
            resolved_server_host,
            resolved_agent_bearer_token,
            resolved_server_host_fallback,
        )
    )
    return initialize_forum_mcp(
        resolved_server_host,
        resolved_agent_bearer_token,
        resolved_server_host_fallback,
    )


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
    return str(getattr(spec, "description", "") or getattr(spec, "name", "forum_mcp_tool"))


def _build_mcp_tool(spec: Any) -> StructuredTool:
    name = str(spec.name)
    schema = dict(getattr(spec, "inputSchema", {}) or {})
    args_schema = _json_schema_to_pydantic(f"MCP_{name}_Args", schema)

    async def _coroutine(**kwargs):
        import asyncio
        loop = asyncio.get_running_loop()

        try:
            return await loop.run_in_executor(_FORUM_MCP_EXECUTOR, get_forum_mcp_runner().call_tool, name, kwargs)
        except ForumMCPError as exc:
            logger.warning("[ForumMCP] 工具调用失败，尝试自动重连后重试: %s | %s", name, exc)
            try:
                await loop.run_in_executor(_FORUM_MCP_EXECUTOR, reconnect_forum_services)
                return await loop.run_in_executor(_FORUM_MCP_EXECUTOR, get_forum_mcp_runner().call_tool, name, kwargs)
            except Exception as retry_exc:
                raise ForumMCPError(
                    f"论坛 MCP 工具调用失败: {exc} | 自动重连后仍失败: {retry_exc}"
                ) from retry_exc

    return StructuredTool.from_function(
        coroutine=_coroutine,
        name=name,
        description=_tool_description_from_spec(spec),
        args_schema=args_schema,
        infer_schema=False,
    )


def _format_payload(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@tool
def forum_get_challenges() -> str:
    """论坛工具：获取零界论坛挑战列表。"""
    try:
        return _format_payload(get_forum_client().get_challenges())
    except ForumAPIError as exc:
        return f"获取论坛挑战列表失败: {exc}"


@tool
def forum_get_my_agent_info() -> str:
    """论坛工具：获取当前队伍在零界论坛中的智能体信息。"""
    try:
        return _format_payload(get_forum_client().get_my_agent_info())
    except ForumAPIError as exc:
        return f"获取当前论坛智能体信息失败: {exc}"


@tool
def forum_get_agents(page: int = 1, size: int = 20) -> str:
    """论坛工具：获取其他智能体列表，适合赛题二密钥交换。"""
    try:
        return _format_payload(get_forum_client().get_agents(page=page, size=size))
    except ForumAPIError as exc:
        return f"获取论坛智能体列表失败: {exc}"


@tool
def forum_get_latest_posts(page: int = 1, size: int = 20) -> str:
    """论坛工具：获取最新帖子流，适合赛题四监控线索。"""
    try:
        return _format_payload(
            get_forum_client().get_latest_posts(page=page, size=size)
        )
    except ForumAPIError as exc:
        return f"获取最新帖子失败: {exc}"


@tool
def forum_get_hot_posts(page: int = 1, size: int = 20) -> str:
    """论坛工具：获取热门帖子，适合赛题三观察热点内容。"""
    try:
        return _format_payload(get_forum_client().get_hot_posts(page=page, size=size))
    except ForumAPIError as exc:
        return f"获取热门帖子失败: {exc}"


@tool
def forum_search_posts(query: str, page: int = 1, size: int = 20) -> str:
    """论坛工具：按关键词搜索帖子标题和内容。"""
    try:
        return _format_payload(
            get_forum_client().get_posts_by_q(query=query, page=page, size=size)
        )
    except ForumAPIError as exc:
        return f"搜索论坛帖子失败: {exc}"


@tool
def forum_get_post_detail(post_id: int) -> str:
    """论坛工具：获取指定帖子的详细信息。"""
    try:
        return _format_payload(get_forum_client().get_post_detail(post_id))
    except ForumAPIError as exc:
        return f"获取帖子详情失败: {exc}"


@tool
def forum_get_post_comments(post_id: int, page: int = 1, size: int = 20) -> str:
    """论坛工具：获取指定帖子的评论列表。"""
    try:
        return _format_payload(
            get_forum_client().get_post_comments(post_id=post_id, page=page, size=size)
        )
    except ForumAPIError as exc:
        return f"获取帖子评论失败: {exc}"


@tool
def forum_get_unread_messages() -> str:
    """论坛工具：获取未读消息统计及会话信息。"""
    try:
        return _format_payload(get_forum_client().get_unread_messages())
    except ForumAPIError as exc:
        return f"获取未读消息失败: {exc}"


@tool
def forum_get_conversations(page: int = 1, size: int = 20) -> str:
    """论坛工具：获取私信会话列表。"""
    try:
        return _format_payload(
            get_forum_client().get_conversations(page=page, size=size)
        )
    except ForumAPIError as exc:
        return f"获取私信会话失败: {exc}"


@tool
def forum_get_conversation_messages(
    conv_id: int, page: int = 1, size: int = 20
) -> str:
    """论坛工具：获取某个私信会话的消息列表。"""
    try:
        return _format_payload(
            get_forum_client().get_conversation_messages(
                conv_id=conv_id,
                page=page,
                size=size,
            )
        )
    except ForumAPIError as exc:
        return f"获取私信消息失败: {exc}"


@tool
def forum_send_direct_message(receiver_id: int, content: str) -> str:
    """论坛工具：向指定智能体发送私信，适合赛题二密钥交换。"""
    try:
        get_forum_client().send_direct_message(receiver_id=receiver_id, content=content)
        return "✅ 私信发送成功。"
    except ForumAPIError as exc:
        return f"发送私信失败: {exc}"


@tool
def forum_create_post(title: str, content: str, tags: Optional[List[str]] = None) -> str:
    """论坛工具：发布新帖子，可用于赛题三内容影响力竞争。"""
    try:
        get_forum_client().create_post(title=title, content=content, tags=tags)
        return "✅ 发帖成功。"
    except ForumAPIError as exc:
        return f"发帖失败: {exc}"


@tool
def forum_create_comment(content: str, post_id: int, parent_id: int = 0) -> str:
    """论坛工具：在帖子或评论下发表评论。"""
    try:
        get_forum_client().create_comment(
            content=content,
            post_id=post_id,
            parent_id=parent_id,
        )
        return "✅ 评论发表成功。"
    except ForumAPIError as exc:
        return f"评论发表失败: {exc}"


@tool
def forum_upvote(target_id: int, target_type: str = "post") -> str:
    """论坛工具：给帖子或评论点赞。"""
    try:
        get_forum_client().upvote(target_id=target_id, target_type=target_type)
        return "✅ 点赞成功。"
    except ForumAPIError as exc:
        return f"点赞失败: {exc}"


@tool
def forum_downvote(target_id: int, target_type: str = "post") -> str:
    """论坛工具：给帖子或评论点踩。"""
    try:
        get_forum_client().downvote(target_id=target_id, target_type=target_type)
        return "✅ 点踩成功。"
    except ForumAPIError as exc:
        return f"点踩失败: {exc}"


@tool
def forum_submit_flag(challenge_id: int, flag: str) -> str:
    """论坛工具：提交零界论坛 Flag。challenge_id 通常为 1、2 或 4。"""
    normalized = (flag or "").strip()
    if not normalized:
        return "❌ 论坛 Flag 不能为空。"
    if not normalized.startswith("flag{") or not normalized.endswith("}"):
        return f"❌ 论坛 Flag 格式错误，必须是 flag{{...}}。当前: {normalized}"
    if has_recorded_forum_flag(normalized):
        return f"⚠️ 论坛 Flag 已在本地记录过，跳过重复提交: {normalized}"

    try:
        result = get_forum_client().submit_ctf_flag(
            challenge_id=challenge_id, flag=normalized
        )
        record_forum_flag_attempt(
            normalized,
            int(challenge_id),
            scored=bool(result.get("scored")),
            verified=result.get("verified"),
            message=str(result.get("message", "") or result.get("verification_error", "") or ""),
        )
        if result.get("scored"):
            return (
                "✅ 论坛 Flag 得分成功: "
                f"+{result.get('score_delta', 0)} 分 | "
                f"Flag增量 {result.get('flag_delta', 0)} | "
                f"完成={result.get('challenge_completed', False)} | "
                f"{result.get('message', 'success')}"
            )
        if result.get("verified"):
            return (
                "⚠️ 论坛 Flag 未得分，按错误处理: "
                f"{result.get('message', 'success')} | 当前总分 {result.get('after_score', 0)}"
            )
        return (
            "⚠️ 论坛 Flag 已提交，但未能验证得分，按未成功处理: "
            f"{result.get('message', 'success')} | {result.get('verification_error', '积分校验失败')}"
        )
    except ForumAPIError as exc:
        return f"❌ 论坛 Flag 提交失败: {exc}"


FORUM_TOOLS = [
    forum_get_challenges,
    forum_get_my_agent_info,
    forum_get_agents,
    forum_get_latest_posts,
    forum_get_hot_posts,
    forum_search_posts,
    forum_get_post_detail,
    forum_get_post_comments,
    forum_get_unread_messages,
    forum_get_conversations,
    forum_get_conversation_messages,
    forum_send_direct_message,
    forum_create_post,
    forum_create_comment,
    forum_upvote,
    forum_downvote,
    forum_submit_flag,
]


def get_forum_tools() -> List:
    return get_forum_mcp_tools()


def get_forum_mcp_tools() -> List:
    specs = get_forum_mcp_runner().list_tool_specs()
    return [_build_mcp_tool(spec) for spec in specs.values()]


def get_forum_tools_for_challenge(challenge_id: Optional[int]) -> List:
    """
    为单个论坛赛题返回最小必要工具集合。

    这样每个论坛模块只会暴露本题需要的接口，避免 1/2/3/4 题互相串线。
    """
    specs = get_forum_mcp_runner().list_tool_specs()

    if challenge_id is None:
        return [_build_mcp_tool(spec) for spec in specs.values()]

    base_names = {"get_challenges", "get_my_agent_info", "update_my_bio"}
    message_names = {
        "get_agents",
        "get_unread_messages",
        "get_conversations",
        "get_conversation_messages",
        "send_direct_message",
    }
    read_names = {
        "get_latest_posts",
        "get_hot_posts",
        "get_posts_by_q",
        "get_post_detail",
        "get_post_comments",
    }
    challenge_names: Dict[int, set[str]] = {
        1: read_names | message_names | {"create_post", "create_comment"},
        2: read_names | message_names | {"create_post", "create_comment", "downvote"},
        3: read_names | {"create_post", "create_comment", "upvote"},
        4: read_names | message_names | {"create_post", "create_comment"},
    }
    allowed = set(base_names) | set(challenge_names.get(int(challenge_id), read_names))

    tools: list = []
    for tool_name in allowed:
        spec = specs.get(tool_name)
        if spec is not None:
            tools.append(_build_mcp_tool(spec))

    if int(challenge_id) in {1, 2, 4}:

        @tool("forum_submit_flag")
        def scoped_forum_submit_flag(flag: str) -> str:
            """提交当前论坛赛题的 Flag。该工具已自动绑定到当前模块，无需传 challenge_id。"""
            normalized = (flag or "").strip()
            if not normalized:
                return "❌ 论坛 Flag 不能为空。"
            if not normalized.startswith("flag{") or not normalized.endswith("}"):
                return f"❌ 论坛 Flag 格式错误，必须是 flag{{...}}。当前: {normalized}"
            if has_recorded_forum_flag(normalized):
                return f"⚠️ 论坛 Flag 已在本地记录过，跳过重复提交: {normalized}"

            try:
                result = get_forum_client().submit_ctf_flag(
                    challenge_id=int(challenge_id),
                    flag=normalized,
                )
                record_forum_flag_attempt(
                    normalized,
                    int(challenge_id),
                    scored=bool(result.get("scored")),
                    verified=result.get("verified"),
                    message=str(result.get("message", "") or result.get("verification_error", "") or ""),
                )
                if result.get("scored"):
                    return (
                        "✅ 论坛 Flag 得分成功: "
                        f"+{result.get('score_delta', 0)} 分 | "
                        f"Flag增量 {result.get('flag_delta', 0)} | "
                        f"完成={result.get('challenge_completed', False)} | "
                        f"{result.get('message', 'success')}"
                    )
                if result.get("verified"):
                    return (
                        "⚠️ 论坛 Flag 未得分，按错误处理: "
                        f"{result.get('message', 'success')} | 当前总分 {result.get('after_score', 0)}"
                    )
                return (
                    "⚠️ 论坛 Flag 已提交，但未能验证得分，按未成功处理: "
                    f"{result.get('message', 'success')} | {result.get('verification_error', '积分校验失败')}"
                )
            except ForumAPIError as exc:
                return f"❌ 论坛 Flag 提交失败: {exc}"

        tools.append(scoped_forum_submit_flag)

    return tools
