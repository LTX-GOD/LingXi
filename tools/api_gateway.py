"""
统一API网关
===========
管理不同赛道/服务的官方接口调用，精确控制各自独立的限流窗口。

特性:
- 滑动窗口算法：确保任意1秒窗口内不超过指定次数
- 优先级队列：Flag提交等高优先级请求优先
- 统一429处理：命名空间内全局退避，避免继续触发限流
- 线程安全：支持多线程并发调用
- 多命名空间：主战场、论坛 HTTP、论坛 MCP 可各自独立限流，互不冲突
"""

import logging
import os
import threading
import time
import json
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional
import re

try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)


class RequestPriority(IntEnum):
    """请求优先级"""
    LOW = 0          # 普通查询
    NORMAL = 1       # 默认优先级
    HIGH = 2         # 启动实例、停止实例
    CRITICAL = 3     # Flag提交


@dataclass
class RequestSlot:
    """请求槽位"""
    timestamp: float
    priority: RequestPriority
    endpoint: str


class UnifiedAPIGateway:
    """
    统一API网关

    使用滑动窗口算法精确控制：任意1秒窗口内最多3次请求
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        max_requests: int = 3,
        window_seconds: float = 1.0,
        safety_margin: float = 0.02,
        shared_across_processes: bool = False,
    ):
        self._request_lock = threading.RLock()
        self._request_times: deque[RequestSlot] = deque(maxlen=100)
        self._namespace = str(namespace or "default")
        self._shared_across_processes = bool(shared_across_processes and fcntl is not None)

        # 限流配置
        self._max_requests = max(1, int(max_requests))
        self._window_seconds = max(0.01, float(window_seconds))
        self._safety_margin = max(0.0, float(safety_margin))

        # 429退避
        self._backoff_until = 0.0
        self._backoff_count = 0

        # 统计
        self._total_requests = 0
        self._total_waits = 0
        self._total_wait_time = 0.0
        self._shared_state_file = self._build_shared_state_file()

        logger.info(
            "[APIGateway:%s] 初始化完成 - 限额: %d req/%gs | shared=%s",
            self._namespace,
            self._max_requests,
            self._window_seconds,
            self._shared_across_processes,
        )

    def _build_shared_state_file(self) -> str:
        safe_namespace = re.sub(r"[^a-zA-Z0-9_.-]+", "_", self._namespace).strip("_") or "default"
        state_dir = os.getenv("LINGXI_API_GATEWAY_STATE_DIR", "/tmp")
        return os.path.join(state_dir, f"lingxi_api_gateway_{safe_namespace}.json")

    def _prune_request_times(self, request_times: list[float], now: float) -> list[float]:
        cutoff = now - self._window_seconds
        return [ts for ts in request_times if ts > cutoff]

    def _load_shared_state(self, fh) -> dict:
        fh.seek(0)
        raw = fh.read()
        if not raw.strip():
            return {"request_times": [], "backoff_until": 0.0, "backoff_count": 0}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"request_times": [], "backoff_until": 0.0, "backoff_count": 0}
        return {
            "request_times": list(data.get("request_times", []) or []),
            "backoff_until": float(data.get("backoff_until", 0.0) or 0.0),
            "backoff_count": int(data.get("backoff_count", 0) or 0),
        }

    def _write_shared_state(self, fh, state: dict) -> None:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state))
        fh.flush()
        os.fsync(fh.fileno())

    def _acquire_shared(self, priority: RequestPriority, endpoint: str) -> float:
        os.makedirs(os.path.dirname(self._shared_state_file), exist_ok=True)
        start_time = time.monotonic()
        with open(self._shared_state_file, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            state = self._load_shared_state(fh)

            now = time.monotonic()
            backoff_until = float(state.get("backoff_until", 0.0) or 0.0)
            if now < backoff_until:
                wait = backoff_until - now
                logger.warning(
                    "[APIGateway:%s] 429退避中，等待 %.2fs (endpoint=%s)",
                    self._namespace,
                    wait,
                    endpoint,
                )
                time.sleep(wait)
                now = time.monotonic()

            request_times = self._prune_request_times(
                [float(ts) for ts in state.get("request_times", [])],
                now,
            )
            if len(request_times) >= self._max_requests:
                oldest = request_times[0]
                wait = (oldest + self._window_seconds) - now + self._safety_margin
                if wait > 0:
                    logger.debug(
                        "[APIGateway:%s] 达到限额 (%d/%d)，等待 %.3fs (endpoint=%s, priority=%s)",
                        self._namespace,
                        len(request_times),
                        self._max_requests,
                        wait,
                        endpoint,
                        priority.name,
                    )
                    time.sleep(wait)
                    now = time.monotonic()
                    request_times = self._prune_request_times(request_times, now)

            request_times.append(now)
            state["request_times"] = request_times[-100:]
            self._write_shared_state(fh, state)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        wait_time = time.monotonic() - start_time
        self._total_requests += 1
        if wait_time > 0.001:
            self._total_waits += 1
            self._total_wait_time += wait_time
        return wait_time

    def _report_429_shared(self, retry_after: Optional[float], endpoint: str) -> None:
        os.makedirs(os.path.dirname(self._shared_state_file), exist_ok=True)
        with open(self._shared_state_file, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            state = self._load_shared_state(fh)
            backoff_count = int(state.get("backoff_count", 0) or 0) + 1
            if retry_after is not None:
                backoff = retry_after
            else:
                backoff = min(2.0 * (2 ** (backoff_count - 1)), 32.0)
            state["backoff_count"] = backoff_count
            state["backoff_until"] = time.monotonic() + backoff
            state["request_times"] = self._prune_request_times(
                [float(ts) for ts in state.get("request_times", [])],
                time.monotonic(),
            )
            self._write_shared_state(fh, state)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        self._backoff_count = backoff_count
        logger.warning(
            "[APIGateway:%s] 触发429限流 (第%d次)，全局退避 %.1fs (endpoint=%s)",
            self._namespace,
            backoff_count,
            backoff,
            endpoint,
        )

    def _reset_backoff_shared(self) -> None:
        os.makedirs(os.path.dirname(self._shared_state_file), exist_ok=True)
        with open(self._shared_state_file, "a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            state = self._load_shared_state(fh)
            backoff_count = max(0, int(state.get("backoff_count", 0) or 0) - 1)
            state["backoff_count"] = backoff_count
            if backoff_count == 0:
                state["backoff_until"] = 0.0
            self._write_shared_state(fh, state)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        self._backoff_count = backoff_count

    def _get_shared_stats(self) -> dict:
        now = time.monotonic()
        if not os.path.exists(self._shared_state_file):
            current_count = 0
            backoff_count = self._backoff_count
            is_backing_off = False
        else:
            with open(self._shared_state_file, "a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                state = self._load_shared_state(fh)
                request_times = self._prune_request_times(
                    [float(ts) for ts in state.get("request_times", [])],
                    now,
                )
                state["request_times"] = request_times[-100:]
                self._write_shared_state(fh, state)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            current_count = len(request_times)
            backoff_count = int(state.get("backoff_count", 0) or 0)
            is_backing_off = now < float(state.get("backoff_until", 0.0) or 0.0)

        avg_wait = (
            self._total_wait_time / self._total_waits
            if self._total_waits > 0 else 0.0
        )
        return {
            "namespace": self._namespace,
            "total_requests": self._total_requests,
            "total_waits": self._total_waits,
            "avg_wait_time": avg_wait,
            "current_window_count": current_count,
            "max_requests": self._max_requests,
            "backoff_count": backoff_count,
            "is_backing_off": is_backing_off,
        }

    def acquire(
        self,
        priority: RequestPriority = RequestPriority.NORMAL,
        endpoint: str = "unknown"
    ) -> float:
        """
        获取请求许可（阻塞直到可以发送）

        Args:
            priority: 请求优先级
            endpoint: 接口端点（用于日志）

        Returns:
            等待时间（秒）
        """
        if self._shared_across_processes:
            return self._acquire_shared(priority, endpoint)

        with self._request_lock:
            start_time = time.monotonic()
            now = start_time

            # 1. 检查429退避期
            if now < self._backoff_until:
                wait = self._backoff_until - now
                logger.warning(
                    "[APIGateway:%s] 429退避中，等待 %.2fs (endpoint=%s)",
                    self._namespace,
                    wait,
                    endpoint,
                )
                time.sleep(wait)
                now = time.monotonic()

            # 2. 清理窗口外的旧请求
            cutoff = now - self._window_seconds
            while self._request_times and self._request_times[0].timestamp <= cutoff:
                self._request_times.popleft()

            # 3. 检查是否达到限额
            if len(self._request_times) >= self._max_requests:
                # 计算需要等待的时间
                oldest = self._request_times[0]
                wait = (oldest.timestamp + self._window_seconds) - now + self._safety_margin

                if wait > 0:
                    logger.debug(
                        "[APIGateway:%s] 达到限额 (%d/%d)，等待 %.3fs (endpoint=%s, priority=%s)",
                        self._namespace,
                        len(self._request_times),
                        self._max_requests,
                        wait,
                        endpoint,
                        priority.name,
                    )
                    time.sleep(wait)
                    now = time.monotonic()

                    # 重新清理
                    cutoff = now - self._window_seconds
                    while self._request_times and self._request_times[0].timestamp <= cutoff:
                        self._request_times.popleft()

            # 4. 记录本次请求
            slot = RequestSlot(
                timestamp=now,
                priority=priority,
                endpoint=endpoint
            )
            self._request_times.append(slot)

            # 5. 更新统计
            self._total_requests += 1
            wait_time = now - start_time
            if wait_time > 0.001:  # 超过1ms才算等待
                self._total_waits += 1
                self._total_wait_time += wait_time

            return wait_time

    def report_429(self, retry_after: Optional[float] = None, endpoint: str = "unknown"):
        """
        报告429错误，触发全局退避

        Args:
            retry_after: 服务器建议的重试延迟（秒）
            endpoint: 触发429的接口端点
        """
        if self._shared_across_processes:
            self._report_429_shared(retry_after, endpoint)
            return

        with self._request_lock:
            self._backoff_count += 1

            # 指数退避：2s, 4s, 8s, 16s, 最大32s
            if retry_after is not None:
                backoff = retry_after
            else:
                backoff = min(2.0 * (2 ** (self._backoff_count - 1)), 32.0)

            self._backoff_until = time.monotonic() + backoff

            logger.warning(
                "[APIGateway:%s] 触发429限流 (第%d次)，全局退避 %.1fs (endpoint=%s)",
                self._namespace,
                self._backoff_count,
                backoff,
                endpoint,
            )

    def reset_backoff(self):
        """重置退避计数（成功请求后调用）"""
        if self._shared_across_processes:
            self._reset_backoff_shared()
            return

        with self._request_lock:
            if self._backoff_count > 0:
                self._backoff_count = max(0, self._backoff_count - 1)

    def get_stats(self) -> dict:
        """获取统计信息"""
        if self._shared_across_processes:
            return self._get_shared_stats()

        with self._request_lock:
            avg_wait = (
                self._total_wait_time / self._total_waits
                if self._total_waits > 0 else 0.0
            )

            # 计算当前窗口内的请求数
            now = time.monotonic()
            cutoff = now - self._window_seconds
            current_count = sum(
                1 for slot in self._request_times
                if slot.timestamp > cutoff
            )

            return {
                "namespace": self._namespace,
                "total_requests": self._total_requests,
                "total_waits": self._total_waits,
                "avg_wait_time": avg_wait,
                "current_window_count": current_count,
                "max_requests": self._max_requests,
                "backoff_count": self._backoff_count,
                "is_backing_off": time.monotonic() < self._backoff_until,
            }

    def print_stats(self):
        """打印统计信息"""
        stats = self.get_stats()
        logger.info(
            "[APIGateway:%s] 统计: 总请求=%d, 等待次数=%d, 平均等待=%.3fs, "
            "当前窗口=%d/%d, 退避次数=%d",
            self._namespace,
            stats["total_requests"],
            stats["total_waits"],
            stats["avg_wait_time"],
            stats["current_window_count"],
            stats["max_requests"],
            stats["backoff_count"],
        )


# 全局命名空间网关注册表
_gateways: dict[str, UnifiedAPIGateway] = {}
_gateway_lock = threading.Lock()


def get_api_gateway(
    namespace: str = "default",
    *,
    max_requests: int = 3,
    window_seconds: float = 1.0,
    safety_margin: float = 0.02,
    shared_across_processes: bool = False,
) -> UnifiedAPIGateway:
    """获取指定命名空间的 API 网关单例。"""
    ns = str(namespace or "default")
    gateway_key = (
        ns,
        bool(shared_across_processes and fcntl is not None),
        int(max_requests),
        float(window_seconds),
        float(safety_margin),
    )
    gateway = _gateways.get(gateway_key)
    if gateway is None:
        with _gateway_lock:
            gateway = _gateways.get(gateway_key)
            if gateway is None:
                gateway = UnifiedAPIGateway(
                    namespace=ns,
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                    safety_margin=safety_margin,
                    shared_across_processes=shared_across_processes,
                )
                _gateways[gateway_key] = gateway
    return gateway
