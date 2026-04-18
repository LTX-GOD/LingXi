"""
多 LLM Provider 管理
====================
支持 DeepSeek / Anthropic / OpenAI / SiliconFlow (MiniMax/Kimi)
自动 Failover + 频率限制
"""

import asyncio
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

_VERSION_SEGMENT_RE = re.compile(r"^v\d+(?:\.\d+)?$")
_COMPETITION_GATEWAY_PATH_RE = re.compile(r"^/85_[A-Za-z0-9]+(?:/v\d+(?:\.\d+)?)?$")
_COMPETITION_GATEWAY_MAX_RETRIES = int(os.getenv("COMPETITION_GATEWAY_LLM_MAX_RETRIES", "0"))
_COMPETITION_GATEWAY_MAX_TOKENS = int(os.getenv("COMPETITION_GATEWAY_LLM_MAX_TOKENS", "4096"))
_COMPETITION_GATEWAY_MAX_CONCURRENCY = int(os.getenv("COMPETITION_GATEWAY_LLM_MAX_CONCURRENCY", "2"))
_COMPETITION_GATEWAY_MIN_INTERVAL = float(os.getenv("COMPETITION_GATEWAY_LLM_MIN_INTERVAL", "0.35"))
_ENDPOINT_GATES_LOCK = threading.Lock()
_ENDPOINT_GATES: dict[str, "_EndpointGate"] = {}
_FAIL_THRESHOLD = 5
_PROBE_INTERVAL = 15.0
_GATE_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(8, _COMPETITION_GATEWAY_MAX_CONCURRENCY * 8),
    thread_name_prefix="llm-gate",
)


class _EndpointGate:
    """针对单个网关的轻量限流器，抑制 524 重试风暴。"""

    def __init__(self, *, concurrency: int, min_interval: float):
        self._semaphore = threading.BoundedSemaphore(max(1, concurrency))
        self._min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_slot_at = 0.0

    def acquire(self):
        self._semaphore.acquire()
        try:
            with self._lock:
                now = time.monotonic()
                wait = max(0.0, self._next_slot_at - now)
                if wait > 0:
                    time.sleep(wait)
                self._next_slot_at = time.monotonic() + self._min_interval
        except Exception:
            self._semaphore.release()
            raise

    def release(self):
        self._semaphore.release()

    async def run_async(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_GATE_EXECUTOR, self.acquire)
        try:
            return await func(*args, **kwargs)
        finally:
            self.release()

    def run_sync(self, func, *args, **kwargs):
        self.acquire()
        try:
            return func(*args, **kwargs)
        finally:
            self.release()


class _ThrottledRunnable:
    """给 LangChain runnable / chat model 套一层端点级并发闸门。"""

    def __init__(self, runnable: Any, gate: _EndpointGate, *, label: str, fallback: Any = None):
        self._runnable = runnable
        self._gate = gate
        self._label = label
        self._fallback = fallback
        self._failover_lock = threading.Lock()
        self._primary_fail_count = 0
        self._using_fallback = False
        self._probe_thread: threading.Thread | None = None
        self._probe_enabled = True

    async def ainvoke(self, *args, **kwargs):
        if self._using_fallback and self._fallback is not None:
            return await self._invoke_fallback(*args, **kwargs)
        try:
            result = await self._gate.run_async(self._runnable.ainvoke, *args, **kwargs)
            with self._failover_lock:
                self._primary_fail_count = 0
            return result
        except Exception as e:
            classification = self._classify_error(e)
            count, should_failover, enable_probe = self._record_primary_failure(classification)
            logger.warning("[Failover] 主端点失败 %d/%d: %s", count, _FAIL_THRESHOLD, e)
            if should_failover and self._fallback is not None:
                logger.warning("[Failover] 切换到备用端点: %s", self._fallback._label)
                if enable_probe:
                    self._start_probe_thread()
                return await self._invoke_fallback(*args, **kwargs)
            raise

    async def _invoke_fallback(self, *args, **kwargs):
        last_error = None
        attempts = 2
        for attempt in range(attempts):
            try:
                return await self._fallback._gate.run_async(
                    self._fallback._runnable.ainvoke, *args, **kwargs
                )
            except Exception as e:
                last_error = e
                classification = self._classify_error(e)
                if classification != "transport" or attempt == attempts - 1:
                    raise
                logger.warning(
                    "[Failover] 备用端点网关异常，重试 %d/%d: %s",
                    attempt + 1,
                    attempts,
                    e,
                )
        raise last_error

    def invoke(self, *args, **kwargs):
        return self._gate.run_sync(self._runnable.invoke, *args, **kwargs)

    def bind_tools(self, *args, **kwargs):
        bound = self._runnable.bind_tools(*args, **kwargs)
        cloned = _ThrottledRunnable(bound, self._gate, label=self._label, fallback=self._fallback)
        cloned._primary_fail_count = self._primary_fail_count
        cloned._using_fallback = self._using_fallback
        cloned._probe_enabled = self._probe_enabled
        return cloned

    @staticmethod
    def _classify_error(error: Exception) -> str:
        error_text = str(error or "").strip().lower()
        if any(token in error_text for token in ("401", "403", "unauthorized", "forbidden", "invalid api key", "invalid_api_key", "无效的令牌")):
            return "auth"
        if any(
            token in error_text
            for token in (
                "408",
                "429",
                "500",
                "502",
                "503",
                "504",
                "524",
                "timed out",
                "timeout",
                "connection refused",
                "connection reset",
                "bad gateway",
                "non-json",
                "html",
            )
        ):
            return "transport"
        return "other"

    def _record_primary_failure(self, classification: str) -> tuple[int, bool, bool]:
        with self._failover_lock:
            if classification == "auth" and self._fallback is not None:
                self._primary_fail_count = _FAIL_THRESHOLD
                self._using_fallback = True
                self._probe_enabled = False
            else:
                self._primary_fail_count += 1
                if self._primary_fail_count >= _FAIL_THRESHOLD and self._fallback is not None:
                    self._using_fallback = True
                    self._probe_enabled = classification == "transport"
            return self._primary_fail_count, self._using_fallback, self._probe_enabled

    def _start_probe_thread(self) -> None:
        with self._failover_lock:
            if not self._probe_enabled:
                return
            if self._probe_thread is not None and self._probe_thread.is_alive():
                return

        def _probe():
            from langchain_core.messages import HumanMessage
            while True:
                time.sleep(_PROBE_INTERVAL)
                with self._failover_lock:
                    if not self._using_fallback or not self._probe_enabled:
                        break
                try:
                    self._runnable.invoke([HumanMessage(content="ping")], max_tokens=1)
                    with self._failover_lock:
                        self._primary_fail_count = 0
                        self._using_fallback = False
                        self._probe_enabled = True
                        self._probe_thread = None
                    logger.info("[Failover] 主端点已恢复，切回主端点")
                    break
                except Exception as e:
                    if self._classify_error(e) == "auth":
                        with self._failover_lock:
                            self._probe_enabled = False
                            self._probe_thread = None
                        logger.warning("[Failover] 主端点认证失败，停止恢复探测")
                        break
                    logger.debug("[Failover] 主端点探测失败，继续使用备用")

        t = threading.Thread(target=_probe, daemon=True, name="llm-failover-probe")
        with self._failover_lock:
            self._probe_thread = t
        t.start()

    def __getattr__(self, item):
        return getattr(self._runnable, item)


def _get_endpoint_gate(base_url: str) -> _EndpointGate:
    root = _provider_root(base_url) or (base_url or "").rstrip("/")
    with _ENDPOINT_GATES_LOCK:
        gate = _ENDPOINT_GATES.get(root)
        if gate is None:
            gate = _EndpointGate(
                concurrency=_COMPETITION_GATEWAY_MAX_CONCURRENCY,
                min_interval=_COMPETITION_GATEWAY_MIN_INTERVAL,
            )
            _ENDPOINT_GATES[root] = gate
        return gate


def _apply_competition_gateway_overrides(
    base_url: str,
    *,
    max_tokens: int,
    max_retries: int,
) -> tuple[int, int]:
    if not _looks_like_competition_gateway(base_url):
        return max_tokens, max_retries

    tuned_tokens = min(max_tokens, _COMPETITION_GATEWAY_MAX_TOKENS)
    tuned_retries = min(max_retries, _COMPETITION_GATEWAY_MAX_RETRIES)
    logger.info(
        "Competition gateway tuning applied: base_url=%s max_tokens=%s->%s max_retries=%s->%s",
        base_url,
        max_tokens,
        tuned_tokens,
        max_retries,
        tuned_retries,
    )
    return tuned_tokens, tuned_retries


def _wrap_if_competition_gateway(llm: BaseChatModel, *, base_url: str, role: str, provider: str, model: str):
    if not _looks_like_competition_gateway(base_url):
        return llm
    logger.info(
        "Enable LLM gateway throttle role=%s provider=%s model=%s base_url=%s concurrency=%s min_interval=%.2fs",
        role,
        provider,
        model,
        _provider_root(base_url),
        _COMPETITION_GATEWAY_MAX_CONCURRENCY,
        _COMPETITION_GATEWAY_MIN_INTERVAL,
    )
    return _ThrottledRunnable(
        llm,
        _get_endpoint_gate(base_url),
        label=f"{role}:{provider}:{model}",
    )


def _strip_path_suffixes(path: str, suffixes: tuple[str, ...]) -> str:
    """移除已知 API 后缀，统一得到 provider 根路径。"""
    normalized = path.rstrip("/")
    changed = True
    while changed and normalized:
        changed = False
        for suffix in suffixes:
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)].rstrip("/")
                changed = True
                break
    return normalized


def _normalize_openai_compatible_base_url(base_url: str) -> str:
    """
    规范 OpenAI 兼容 base_url。

    比赛使用的 New API 网关根地址形如 `http://host/85_xxx`，真正可用的
    OpenAI 兼容 API 在 `/v1/chat/completions`。LangChain 只会自动补
    `/chat/completions`，因此这里需要先把根地址矫正为 `/v1`。
    """
    normalized = (base_url or "").strip()
    if not normalized:
        return normalized

    parsed = urlparse(normalized)
    path = _strip_path_suffixes(
        parsed.path,
        (
            "/v2/chat/completions",
            "/v1/chat/completions",
            "/chat/completions",
            "/completions",
            "/responses",
        ),
    )
    last_segment = path.rsplit("/", 1)[-1] if path else ""

    needs_v1 = False
    if not path:
        needs_v1 = True
    elif _VERSION_SEGMENT_RE.fullmatch(last_segment):
        needs_v1 = False
    elif _COMPETITION_GATEWAY_PATH_RE.fullmatch(path):
        needs_v1 = True
    elif path.endswith("/openai"):
        needs_v1 = True

    if needs_v1:
        path = f"{path}/v1" if path else "/v1"

    sanitized = parsed._replace(path=path or "", params="", query="", fragment="")
    return urlunparse(sanitized).rstrip("/")


def _provider_root(base_url: str) -> str:
    """提取 provider 根地址，用于识别不同协议是否误指向同一网关。"""
    normalized = (base_url or "").strip()
    if not normalized:
        return normalized

    parsed = urlparse(normalized)
    path = _strip_path_suffixes(
        parsed.path,
        (
            "/v1/messages",
            "/messages",
            "/v2/chat/completions",
            "/v1/chat/completions",
            "/chat/completions",
            "/v1",
        ),
    )
    last_segment = path.rsplit("/", 1)[-1] if path else ""
    if _VERSION_SEGMENT_RE.fullmatch(last_segment):
        path = path.rsplit("/", 1)[0]
    sanitized = parsed._replace(path=path or "", params="", query="", fragment="")
    return urlunparse(sanitized).rstrip("/")


def _looks_like_competition_gateway(base_url: str) -> bool:
    normalized = (base_url or "").strip()
    if not normalized:
        return False
    parsed = urlparse(normalized)
    path = _strip_path_suffixes(
        parsed.path,
        (
            "/v1/messages",
            "/messages",
            "/v2/chat/completions",
            "/v1/chat/completions",
            "/chat/completions",
            "/v1",
        ),
    )
    return bool(_COMPETITION_GATEWAY_PATH_RE.fullmatch(path))


def _build_chat_openai(
    base_url: str,
    api_key: str,
    model: str,
    *,
    temperature: float,
    max_tokens: int,
    timeout: int,
    max_retries: int,
) -> BaseChatModel:
    normalized_base_url = _normalize_openai_compatible_base_url(base_url)
    max_tokens, max_retries = _apply_competition_gateway_overrides(
        normalized_base_url,
        max_tokens=max_tokens,
        max_retries=max_retries,
    )
    if normalized_base_url != (base_url or "").rstrip("/"):
        logger.info(
            "Normalize OpenAI-compatible base_url from %s to %s",
            base_url,
            normalized_base_url,
        )
    return ChatOpenAI(
        base_url=normalized_base_url,
        api_key=api_key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=max_retries,
    )


def _is_provider_available(config, provider: str, role: str = "main") -> bool:
    if provider == "deepseek":
        return bool(config.deepseek_api_key)
    if provider == "anthropic":
        if role == "advisor":
            return bool(
                getattr(config, "advisor_anthropic_api_key", "")
                or config.anthropic_api_key
            )
        return bool(config.anthropic_api_key)
    if provider == "openai":
        return bool(config.openai_api_key)
    if provider == "siliconflow":
        return bool(config.siliconflow_api_key)
    return False


def _resolve_provider(config, preferred: str, role: str = "main") -> str:
    """优先使用指定 provider；若不可用则自动回退到首个可用 provider。"""
    if _is_provider_available(config, preferred, role=role):
        return preferred

    fallback_order = ["deepseek", "anthropic", "openai", "siliconflow"]
    for p in fallback_order:
        if _is_provider_available(config, p, role=role):
            logger.warning(
                "Preferred provider '%s' unavailable, fallback to '%s'",
                preferred,
                p,
            )
            return p
    raise RuntimeError("No LLM providers available (all API keys missing)")


def create_deepseek(
    base_url: str, api_key: str, model: str = "deepseek-chat", **kwargs
) -> BaseChatModel:
    """创建 DeepSeek LLM"""
    return _build_chat_openai(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=kwargs.get("temperature", 0.0),
        max_tokens=kwargs.get("max_tokens", 8192),
        timeout=kwargs.get("timeout", 300),
        max_retries=kwargs.get("max_retries", 3),
    )


def create_anthropic(
    api_key: str,
    model: str = "claude-sonnet-4-20250514",
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseChatModel:
    """创建 Anthropic Claude LLM"""
    tuned_max_tokens, tuned_max_retries = _apply_competition_gateway_overrides(
        base_url or "",
        max_tokens=kwargs.get("max_tokens", 8192),
        max_retries=kwargs.get("max_retries", 3),
    )
    normalized = (base_url.rstrip("/") if base_url else "https://api.anthropic.com")

    # 比赛网关 85_xxx 默认只暴露 OpenAI 兼容接口，原生 Anthropic `/v1/messages`
    # 会直接 404。因此这里对比赛网关强制走 OpenAI 兼容路径。
    if _looks_like_competition_gateway(normalized):
        logger.info(
            "Anthropic-compatible competition gateway detected; route via OpenAI-compatible API "
            "base_url=%s model=%s",
            normalized,
            model,
        )
        return _build_chat_openai(
            base_url=normalized,
            api_key=api_key,
            model=model,
            temperature=kwargs.get("temperature", 0.0),
            max_tokens=tuned_max_tokens,
            timeout=kwargs.get("timeout", 300),
            max_retries=tuned_max_retries,
        )

    try:
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=kwargs.get("temperature", 0.0),
            max_tokens=tuned_max_tokens,
            timeout=kwargs.get("timeout", 300),
            max_retries=tuned_max_retries,
        )
    except ImportError:
        logger.warning(
            "langchain-anthropic not installed, falling back to OpenAI-compatible"
        )
        # MiniMax 的 Anthropic 兼容网关已包含 /anthropic 前缀，OpenAI 兼容端点应走 /v1/chat/completions。
        # 若直接追加 /v1 会变成 /anthropic/v1/chat/completions，导致 404。
        if normalized.endswith("/anthropic"):
            normalized = normalized[: -len("/anthropic")]
        return _build_chat_openai(
            base_url=normalized,
            api_key=api_key,
            model=model,
            temperature=kwargs.get("temperature", 0.0),
            max_tokens=tuned_max_tokens,
            timeout=kwargs.get("timeout", 300),
            max_retries=tuned_max_retries,
        )


def create_openai(
    base_url: str, api_key: str, model: str = "gpt-4o", **kwargs
) -> BaseChatModel:
    """创建 OpenAI LLM"""
    return _build_chat_openai(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=kwargs.get("temperature", 0.0),
        max_tokens=kwargs.get("max_tokens", 8192),
        timeout=kwargs.get("timeout", 300),
        max_retries=kwargs.get("max_retries", 3),
    )


def create_siliconflow(
    base_url: str, api_key: str, model: str = "MiniMaxAI/MiniMax-M2", **kwargs
) -> BaseChatModel:
    """创建 SiliconFlow (MiniMax/Kimi 等)"""
    return _build_chat_openai(
        base_url=base_url,
        api_key=api_key,
        model=model,
        temperature=kwargs.get("temperature", 0.7),
        max_tokens=kwargs.get("max_tokens", 8192),
        timeout=kwargs.get("timeout", 600),
        max_retries=kwargs.get("max_retries", 10),
    )


def create_llm_from_config(config, role: str = "main") -> BaseChatModel:
    """
    根据配置创建 LLM 实例

    Args:
        config: LLMConfig 实例
        role: "main" (主攻手) 或 "advisor" (顾问)
    """
    preferred_provider = (
        config.main_provider if role == "main" else config.advisor_provider
    )
    provider = _resolve_provider(config, preferred_provider, role=role)
    logger.info(
        "Create LLM role=%s preferred=%s resolved=%s",
        role,
        preferred_provider,
        provider,
    )

    anthropic_base_url = (
        getattr(config, "advisor_anthropic_base_url", "")
        if role == "advisor"
        else getattr(config, "anthropic_base_url", "")
    ) or getattr(config, "anthropic_base_url", "")
    anthropic_api_key = (
        getattr(config, "advisor_anthropic_api_key", "")
        if role == "advisor"
        else getattr(config, "anthropic_api_key", "")
    ) or getattr(config, "anthropic_api_key", "")
    anthropic_model = (
        getattr(config, "advisor_anthropic_model", "")
        if role == "advisor"
        else getattr(config, "anthropic_model", "")
    ) or getattr(config, "anthropic_model", "")

    if (
        provider == "anthropic"
        and config.openai_api_key
        and _looks_like_competition_gateway(anthropic_base_url)
        and _provider_root(anthropic_base_url)
        == _provider_root(getattr(config, "openai_base_url", ""))
    ):
        logger.warning(
            "Anthropic endpoint for role=%s points to the same competition gateway as OpenAI; "
            "routing through OpenAI-compatible API with model=%s base_url=%s",
            role,
            config.openai_model,
            _normalize_openai_compatible_base_url(config.openai_base_url),
        )
        llm = create_openai(
            config.openai_base_url,
            config.openai_api_key,
            config.openai_model,
        )
        llm_result = _wrap_if_competition_gateway(
            llm,
            base_url=config.openai_base_url,
            role=role,
            provider="openai",
            model=config.openai_model,
        )
    elif provider == "deepseek":
        logger.info(
            "LLM endpoint role=%s provider=deepseek model=%s base_url=%s",
            role,
            config.deepseek_model,
            config.deepseek_base_url,
        )
        llm = create_deepseek(
            config.deepseek_base_url, config.deepseek_api_key, config.deepseek_model
        )
        llm_result = _wrap_if_competition_gateway(
            llm,
            base_url=config.deepseek_base_url,
            role=role,
            provider="deepseek",
            model=config.deepseek_model,
        )
    elif provider == "anthropic":
        logger.info(
            "LLM endpoint role=%s provider=anthropic model=%s base_url=%s",
            role,
            anthropic_model,
            anthropic_base_url,
        )
        llm = create_anthropic(
            anthropic_api_key,
            anthropic_model,
            base_url=anthropic_base_url,
        )
        llm_result = _wrap_if_competition_gateway(
            llm,
            base_url=anthropic_base_url,
            role=role,
            provider="anthropic",
            model=anthropic_model,
        )
    elif provider == "openai":
        logger.info(
            "LLM endpoint role=%s provider=openai model=%s base_url=%s",
            role,
            config.openai_model,
            config.openai_base_url,
        )
        llm = create_openai(
            config.openai_base_url, config.openai_api_key, config.openai_model
        )
        llm_result = _wrap_if_competition_gateway(
            llm,
            base_url=config.openai_base_url,
            role=role,
            provider="openai",
            model=config.openai_model,
        )
    elif provider == "siliconflow":
        logger.info(
            "LLM endpoint role=%s provider=siliconflow model=%s base_url=%s",
            role,
            config.siliconflow_model,
            config.siliconflow_base_url,
        )
        llm = create_siliconflow(
            config.siliconflow_base_url,
            config.siliconflow_api_key,
            config.siliconflow_model,
        )
        llm_result = _wrap_if_competition_gateway(
            llm,
            base_url=config.siliconflow_base_url,
            role=role,
            provider="siliconflow",
            model=config.siliconflow_model,
        )
    else:
        raise ValueError(f"Unknown LLM provider: {provider}")

    # 注入备用端点（角色专属优先，全局兜底次之）
    _role_prefix = {"main": "main", "advisor": "advisor", "forum": "forum"}.get(role, "")
    fallback_url = (
        getattr(config, f"{_role_prefix}_fallback_base_url", "") if _role_prefix else ""
    ) or getattr(config, "openai_fallback_base_url", "")
    fallback_key = (
        getattr(config, f"{_role_prefix}_fallback_api_key", "") if _role_prefix else ""
    ) or getattr(config, "openai_fallback_api_key", "")
    if fallback_url and fallback_key and isinstance(llm_result, _ThrottledRunnable):
        fallback_model = (
            (getattr(config, f"{_role_prefix}_fallback_model", "") if _role_prefix else "")
            or getattr(config, "openai_fallback_model", "")
            or (
                anthropic_model
                if provider == "anthropic"
                else config.openai_model
            )
        )
        if provider == "anthropic":
            fb_llm = create_anthropic(
                fallback_key,
                fallback_model,
                base_url=fallback_url,
            )
        else:
            fb_llm = create_openai(fallback_url, fallback_key, fallback_model)
        fb_wrapped = _wrap_if_competition_gateway(
            fb_llm,
            base_url=fallback_url,
            role=role,
            provider=f"{provider}-fallback",
            model=fallback_model,
        )
        llm_result._fallback = fb_wrapped
        logger.info(
            "[Failover] 备用端点已配置: role=%s provider=%s model=%s",
            role,
            provider,
            fallback_model,
        )

    return llm_result


class FailoverLLM:
    """
    Failover LLM — 自动切换 Provider

    主模型调用失败时自动尝试下一个 Provider。
    """

    def __init__(self, config):
        self.config = config
        self._providers = []
        self._build_chain()

    def _build_chain(self):
        """构建 failover chain"""
        providers = [
            (
                "deepseek",
                lambda: create_deepseek(
                    self.config.deepseek_base_url,
                    self.config.deepseek_api_key,
                    self.config.deepseek_model,
                ),
            ),
            (
                "anthropic",
                lambda: create_anthropic(
                    self.config.anthropic_api_key,
                    self.config.anthropic_model,
                    base_url=getattr(self.config, "anthropic_base_url", None),
                ),
            ),
            (
                "openai",
                lambda: create_openai(
                    self.config.openai_base_url,
                    self.config.openai_api_key,
                    self.config.openai_model,
                ),
            ),
            (
                "siliconflow",
                lambda: create_siliconflow(
                    self.config.siliconflow_base_url,
                    self.config.siliconflow_api_key,
                    self.config.siliconflow_model,
                ),
            ),
        ]

        for name, factory in providers:
            try:
                # Check if the provider has credentials
                if name == "deepseek" and self.config.deepseek_api_key:
                    self._providers.append((name, factory))
                elif name == "anthropic" and self.config.anthropic_api_key:
                    self._providers.append((name, factory))
                elif name == "openai" and self.config.openai_api_key:
                    self._providers.append((name, factory))
                elif name == "siliconflow" and self.config.siliconflow_api_key:
                    self._providers.append((name, factory))
            except Exception as e:
                logger.warning(f"Failed to init {name}: {e}")

        logger.info(f"Failover chain: {[p[0] for p in self._providers]}")

    def get_primary(self) -> BaseChatModel:
        """获取主 Provider"""
        if not self._providers:
            raise RuntimeError("No LLM providers available")
        name, factory = self._providers[0]
        logger.debug(f"Using primary LLM: {name}")
        return factory()

    def get_fallback(self, skip: str = "") -> Optional[BaseChatModel]:
        """获取备用 Provider"""
        for name, factory in self._providers:
            if name != skip:
                logger.debug(f"Falling back to: {name}")
                return factory()
        return None
