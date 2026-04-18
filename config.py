"""
配置管理
========
从环境变量 / .env 文件加载所有配置项。
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from host_failover import normalize_host_url
from kali_container import get_kali_container_name

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*args, **kwargs):
        return False


# Shell 环境应优先于 .env，便于运行时临时调参和回滚。
load_dotenv(override=False)


def _env_or_default(name: str, default: str) -> str:
    """
    读取环境变量；当变量不存在或为空字符串时，回退到默认值。
    """
    value = os.getenv(name)
    if value is None:
        return default
    stripped = value.strip()
    if stripped == "":
        return default
    return stripped


@dataclass
class PlatformConfig:
    """主赛场配置（与零界论坛 SERVER_HOST 解耦）"""

    base_url: str = ""
    api_base_url: str = ""
    server_host: str = ""
    server_host_fallback: str = ""
    api_token: str = ""

    def __post_init__(self):
        base_url = _env_or_default("COMPETITION_BASE_URL", self.base_url)
        api_base_url = _env_or_default("COMPETITION_API_BASE_URL", base_url)
        server_host = _env_or_default("COMPETITION_SERVER_HOST", api_base_url or base_url)
        fallback_host = _env_or_default(
            "COMPETITION_SERVER_HOST_FALLBACK",
            self.server_host_fallback,
        )
        self.base_url = normalize_host_url(base_url, default_scheme="http")
        self.api_base_url = normalize_host_url(api_base_url, default_scheme="http")
        self.server_host = normalize_host_url(server_host, default_scheme="http")
        normalized_fallback = normalize_host_url(fallback_host, default_scheme="http")
        self.server_host_fallback = (
            normalized_fallback if normalized_fallback != self.server_host else ""
        )
        # 官方文档: AGENT_TOKEN / 兼容旧配置: COMPETITION_API_TOKEN
        self.api_token = _env_or_default(
            "AGENT_TOKEN",
            _env_or_default("COMPETITION_API_TOKEN", self.api_token),
        )


@dataclass
class ForumConfig:
    """零界论坛赛道配置（严格对齐官方论坛工具包入口语义）"""

    server_host: str = ""
    server_host_fallback: str = ""
    agent_bearer_token: str = ""
    enabled: bool = False

    def __post_init__(self):
        raw_host = _env_or_default("SERVER_HOST", self.server_host)
        self.server_host = normalize_host_url(raw_host, default_scheme="http")
        raw_fallback_host = _env_or_default(
            "SERVER_HOST_FALLBACK",
            self.server_host_fallback,
        )
        fallback_host = normalize_host_url(raw_fallback_host, default_scheme="http")
        self.server_host_fallback = (
            fallback_host if fallback_host != self.server_host else ""
        )
        # 官方论坛工具包: Authorization: Bearer <AGENT_TOKEN>
        self.agent_bearer_token = _env_or_default(
            "AGENT_TOKEN",
            _env_or_default("AGENT_BEARER_TOKEN", self.agent_bearer_token),
        )
        self.enabled = _env_or_default("FORUM_ENABLED", "false").lower() == "true"


@dataclass
class LLMProviderConfig:
    """单个 LLM Provider 配置"""

    name: str = ""
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout: int = 300


@dataclass
class LLMConfig:
    """LLM 配置 (多 Provider)"""

    # 主攻手
    main_provider: str = "deepseek"
    # 顾问
    advisor_provider: str = "siliconflow"

    # DeepSeek
    deepseek_base_url: str = ""
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"

    # Anthropic
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"
    advisor_anthropic_base_url: str = ""
    advisor_anthropic_api_key: str = ""
    advisor_anthropic_model: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o"

    # SiliconFlow (MiniMax / Kimi 等)
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_api_key: str = ""
    siliconflow_model: str = "MiniMaxAI/MiniMax-M2"

    # Forum / 灵境零界专用 OpenAI-compatible LLM（不影响主战场）
    forum_llm_provider: str = "openai"
    forum_llm_base_url: str = ""
    forum_llm_api_key: str = ""
    forum_llm_model: str = ""

    # 主备切换备用端点（全局兜底）
    openai_fallback_base_url: str = ""
    openai_fallback_api_key: str = ""
    openai_fallback_model: str = ""

    # 角色专属备用端点（优先级高于全局兜底，留空则回退到全局）
    main_fallback_base_url: str = ""
    main_fallback_api_key: str = ""
    main_fallback_model: str = ""
    advisor_fallback_base_url: str = ""
    advisor_fallback_api_key: str = ""
    advisor_fallback_model: str = ""
    forum_fallback_base_url: str = ""
    forum_fallback_api_key: str = ""
    forum_fallback_model: str = ""

    def __post_init__(self):
        self.deepseek_base_url = _env_or_default(
            "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
        )
        self.deepseek_api_key = _env_or_default(
            "DEEPSEEK_API_KEY", self.deepseek_api_key
        )
        self.deepseek_model = _env_or_default("DEEPSEEK_MODEL", self.deepseek_model)
        self.anthropic_base_url = _env_or_default(
            "ANTHROPIC_BASE_URL", self.anthropic_base_url
        )
        self.anthropic_api_key = _env_or_default(
            "ANTHROPIC_API_KEY", self.anthropic_api_key
        )
        self.anthropic_model = _env_or_default("ANTHROPIC_MODEL", self.anthropic_model)
        self.advisor_anthropic_base_url = _env_or_default(
            "ADVISOR_ANTHROPIC_BASE_URL", self.anthropic_base_url
        )
        self.advisor_anthropic_api_key = _env_or_default(
            "ADVISOR_ANTHROPIC_API_KEY", self.anthropic_api_key
        )
        self.advisor_anthropic_model = _env_or_default(
            "ADVISOR_ANTHROPIC_MODEL", self.anthropic_model
        )
        self.openai_api_key = _env_or_default("OPENAI_API_KEY", self.openai_api_key)
        self.openai_base_url = _env_or_default("OPENAI_BASE_URL", self.openai_base_url)
        self.openai_model = _env_or_default("OPENAI_MODEL", self.openai_model)
        self.siliconflow_base_url = _env_or_default(
            "SILICONFLOW_BASE_URL", self.siliconflow_base_url
        )
        self.siliconflow_api_key = _env_or_default(
            "SILICONFLOW_API_KEY", self.siliconflow_api_key
        )
        self.siliconflow_model = _env_or_default(
            "SILICONFLOW_MODEL", self.siliconflow_model
        )
        self.forum_llm_provider = _env_or_default(
            "FORUM_LLM_PROVIDER", self.forum_llm_provider
        )
        self.forum_llm_base_url = _env_or_default(
            "FORUM_LLM_BASE_URL", self.forum_llm_base_url
        )
        self.forum_llm_api_key = _env_or_default(
            "FORUM_LLM_API_KEY", self.forum_llm_api_key
        )
        self.forum_llm_model = _env_or_default(
            "FORUM_LLM_MODEL", self.forum_llm_model
        )
        self.openai_fallback_base_url = _env_or_default(
            "OPENAI_FALLBACK_BASE_URL", self.openai_fallback_base_url
        )
        self.openai_fallback_api_key = _env_or_default(
            "OPENAI_FALLBACK_API_KEY", self.openai_fallback_api_key
        )
        self.openai_fallback_model = _env_or_default(
            "OPENAI_FALLBACK_MODEL", self.openai_fallback_model
        )
        self.main_fallback_base_url = _env_or_default(
            "MAIN_FALLBACK_BASE_URL", self.main_fallback_base_url
        )
        self.main_fallback_api_key = _env_or_default(
            "MAIN_FALLBACK_API_KEY", self.main_fallback_api_key
        )
        self.main_fallback_model = _env_or_default(
            "MAIN_FALLBACK_MODEL", self.main_fallback_model
        )
        self.advisor_fallback_base_url = _env_or_default(
            "ADVISOR_FALLBACK_BASE_URL", self.advisor_fallback_base_url
        )
        self.advisor_fallback_api_key = _env_or_default(
            "ADVISOR_FALLBACK_API_KEY", self.advisor_fallback_api_key
        )
        self.advisor_fallback_model = _env_or_default(
            "ADVISOR_FALLBACK_MODEL", self.advisor_fallback_model
        )
        self.forum_fallback_base_url = _env_or_default(
            "FORUM_FALLBACK_BASE_URL", self.forum_fallback_base_url
        )
        self.forum_fallback_api_key = _env_or_default(
            "FORUM_FALLBACK_API_KEY", self.forum_fallback_api_key
        )
        self.forum_fallback_model = _env_or_default(
            "FORUM_FALLBACK_MODEL", self.forum_fallback_model
        )
        self.main_provider = _env_or_default("MAIN_LLM_PROVIDER", self.main_provider)
        self.advisor_provider = _env_or_default(
            "ADVISOR_LLM_PROVIDER", self.advisor_provider
        )


@dataclass
class DockerConfig:
    """Docker 执行环境配置"""

    container_name: str = ""
    enabled: bool = True

    def __post_init__(self):
        self.container_name = get_kali_container_name(self.container_name)
        self.enabled = _env_or_default("DOCKER_ENABLED", "true").lower() == "true"


@dataclass
class SliverConfig:
    """Sliver MCP 配置"""

    enabled: bool = False
    client_path: str = "./bin/sliver-client"
    client_config_path: str = "./sliver-config"
    client_root_dir: str = "./sliver-workdir"

    def __post_init__(self):
        self.client_path = _env_or_default("SLIVER_CLIENT_PATH", self.client_path)
        self.client_config_path = _env_or_default(
            "SLIVER_CLIENT_CONFIG", self.client_config_path
        )
        self.client_root_dir = _env_or_default(
            "SLIVER_CLIENT_ROOT_DIR", self.client_root_dir
        )

        enabled_raw = os.getenv("SLIVER_ENABLED")
        if enabled_raw is None or not enabled_raw.strip():
            auto_enable = (
                _env_or_default("SLIVER_AUTO_ENABLE_IF_PRESENT", "false").lower()
                == "true"
            )
            self.enabled = (
                auto_enable
                and os.path.exists(self.client_path)
                and os.path.exists(self.client_config_path)
            )
        else:
            self.enabled = enabled_raw.strip().lower() == "true"


@dataclass
class AgentConfig:
    """Agent 运行配置"""

    max_attempts: int = 70
    initial_attempt_budget: int = 50
    retry_attempt_budget_step: int = 10
    max_concurrent_tasks: int = 8
    max_forum_concurrent_tasks: int = 4
    single_task_timeout: int = 3600
    max_retries: int = 4
    retry_backoff_seconds: int = 60
    enable_role_swap_retry: bool = True
    attempt_history_limit: int = 3
    consecutive_failures_threshold: int = 3
    advisor_consultation_interval: int = 0
    fetch_interval_seconds: int = 600
    schedule_tick_seconds: int = 5
    tool_loop_break_threshold: int = 20
    advisor_no_tool_rounds_threshold: int = 2
    # SDK 专用
    sdk_model: str = ""
    sdk_advisor_model: str = ""
    sdk_permission_mode: str = "bypassPermissions"

    def __post_init__(self):
        self.max_attempts = int(_env_or_default("MAX_ATTEMPTS", str(self.max_attempts)))
        self.initial_attempt_budget = int(
            _env_or_default("INITIAL_ATTEMPT_BUDGET", str(self.initial_attempt_budget))
        )
        self.retry_attempt_budget_step = int(
            _env_or_default(
                "RETRY_ATTEMPT_BUDGET_STEP", str(self.retry_attempt_budget_step)
            )
        )
        self.max_concurrent_tasks = int(
            _env_or_default("MAX_CONCURRENT_TASKS", str(self.max_concurrent_tasks))
        )
        self.max_forum_concurrent_tasks = int(
            _env_or_default(
                "MAX_FORUM_CONCURRENT_TASKS",
                str(self.max_forum_concurrent_tasks),
            )
        )
        self.single_task_timeout = int(
            _env_or_default("SINGLE_TASK_TIMEOUT", str(self.single_task_timeout))
        )
        self.max_retries = int(_env_or_default("MAX_RETRIES", str(self.max_retries)))
        self.retry_backoff_seconds = int(
            _env_or_default("RETRY_BACKOFF_SECONDS", str(self.retry_backoff_seconds))
        )
        self.enable_role_swap_retry = (
            _env_or_default("ENABLE_ROLE_SWAP_RETRY", "true").lower() == "true"
        )
        self.attempt_history_limit = int(
            _env_or_default("ATTEMPT_HISTORY_LIMIT", str(self.attempt_history_limit))
        )
        self.consecutive_failures_threshold = int(
            _env_or_default(
                "CONSECUTIVE_FAILURES_THRESHOLD",
                str(self.consecutive_failures_threshold),
            )
        )
        self.advisor_consultation_interval = int(
            _env_or_default(
                "ADVISOR_CONSULTATION_INTERVAL", str(self.advisor_consultation_interval)
            )
        )
        self.fetch_interval_seconds = int(
            _env_or_default("FETCH_INTERVAL_SECONDS", str(self.fetch_interval_seconds))
        )
        self.schedule_tick_seconds = int(
            _env_or_default("SCHEDULE_TICK_SECONDS", str(self.schedule_tick_seconds))
        )
        self.tool_loop_break_threshold = int(
            _env_or_default(
                "TOOL_LOOP_BREAK_THRESHOLD", str(self.tool_loop_break_threshold)
            )
        )
        self.advisor_no_tool_rounds_threshold = int(
            _env_or_default(
                "ADVISOR_NO_TOOL_ROUNDS_THRESHOLD",
                str(self.advisor_no_tool_rounds_threshold),
            )
        )
        self.sdk_model = _env_or_default("SDK_MODEL", self.sdk_model)
        self.sdk_advisor_model = _env_or_default("SDK_ADVISOR_MODEL", self.sdk_advisor_model)
        self.sdk_permission_mode = _env_or_default("SDK_PERMISSION_MODE", self.sdk_permission_mode)


@dataclass
class AppConfig:
    """全局配置"""

    platform: PlatformConfig = field(default_factory=PlatformConfig)
    forum: ForumConfig = field(default_factory=ForumConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    docker: DockerConfig = field(default_factory=DockerConfig)
    sliver: SliverConfig = field(default_factory=SliverConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)


def load_config() -> AppConfig:
    """加载全局配置"""
    return AppConfig()


def resolve_advisor_model_name(llm_config: LLMConfig) -> str:
    """按顾问 provider 解析默认模型名，避免顾问始终误用 advisor_anthropic_model。"""
    provider = str(getattr(llm_config, "advisor_provider", "") or "").strip().lower()
    if provider == "deepseek":
        return str(getattr(llm_config, "deepseek_model", "") or "").strip()
    if provider == "openai":
        return str(getattr(llm_config, "openai_model", "") or "").strip()
    if provider == "siliconflow":
        return str(getattr(llm_config, "siliconflow_model", "") or "").strip()
    return str(
        getattr(llm_config, "advisor_anthropic_model", "")
        or getattr(llm_config, "anthropic_model", "")
        or ""
    ).strip()
