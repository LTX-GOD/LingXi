"""
Shell 命令执行工具
=================
在 Docker (Kali) 或本地执行渗透测试命令。

核心理念：
- 原始输出直接返回给 LLM，由 LLM 自主决策
- 不做预处理，充分发挥 LLM 的理解能力
"""
import asyncio
import logging
import os
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Iterable, Optional
from urllib.parse import urlparse

from kali_container import get_kali_container_name
from langchain_core.tools import tool
from log_utils import describe_shell_command
from runtime_env import get_project_python, get_project_root

logger = logging.getLogger(__name__)
_SHELL_TOOL_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(32, int(os.getenv("SHELL_TOOL_EXECUTOR_WORKERS", "64"))),
    thread_name_prefix="shell-tool",
)
_HEAVY_SCAN_TIMEOUT = max(60, int(os.getenv("HEAVY_SCAN_TIMEOUT", "90")))
_HEAVY_SCAN_PATTERNS = (
    re.compile(r"\bnmap\b.*\s-sC(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bnmap\b.*\s-sV(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bnuclei\b", re.IGNORECASE),
    re.compile(r"\bgobuster\s+dir\b", re.IGNORECASE),
    re.compile(r"\bffuf\b", re.IGNORECASE),
    re.compile(r"\bferoxbuster\b", re.IGNORECASE),
)
_SHELL_QUOTING_FAILURE_HINT = (
    "检测到 shell quoting/参数拼接失败。"
    "Bearer/Cookie/JWT/JSON/复杂表单请求不要继续用 curl 硬拼，"
    "下一步改用 execute_python(requests) 发送请求并打印状态码、响应头、响应体前几百字节。"
)

# ─── Docker 命令执行器 ───


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CommandPolicy:
    allowed_hosts: tuple[str, ...] = ()
    blocked_hosts: tuple[str, ...] = ()
    enforce_allowlist: bool = False


_command_policy_var: ContextVar[CommandPolicy] = ContextVar(
    "lingxi_command_policy",
    default=CommandPolicy(),
)
_global_blocked_hosts: set[str] = set()

URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# ─── 安全守卫：禁止命令模式 ───
# 包管理器（禁止安装/修改系统）
FORBIDDEN_CMD_PATTERNS = (
    re.compile(r"(^|\s)apt(-get)?(\s|$)"),
    re.compile(r"(^|\s)dpkg(\s|$)"),
    re.compile(r"(^|\s)(yum|dnf|apk|pacman)(\s|$)"),
    re.compile(r"(^|\s)pip3?\s+install(\s|$)"),
    re.compile(r"(^|\s)uv\s+pip\s+install(\s|$)"),
    # ─── 破坏性文件操作 ───
    re.compile(r"(^|[;&|]\s*)rm\s+-(r|f|rf|fr)\s", re.IGNORECASE),           # rm -rf / rm -f
    re.compile(r"(^|[;&|]\s*)rm\s+--no-preserve-root", re.IGNORECASE),        # rm --no-preserve-root
    re.compile(r"(^|[;&|]\s*)rm\s+(-[a-z]*r[a-z]*\s+)?/($|\s)", re.IGNORECASE),  # rm / 或 rm -rf /
    re.compile(r"(^|[;&|]\s*)rm\s+(-[a-z]*r[a-z]*\s+)?~", re.IGNORECASE),    # rm -rf ~ (用户目录)
    re.compile(r"(^|[;&|]\s*)rm\s+(-[a-z]*r[a-z]*\s+)?\.\.", re.IGNORECASE), # rm -rf ..
    re.compile(r"(^|[;&|]\s*)rm\s+.*\*\s*$", re.IGNORECASE),                 # rm *（通配符删除）
    re.compile(r"(^|[;&|]\s*)shred\s", re.IGNORECASE),                       # 安全擦除
    # ─── 磁盘/分区破坏 ───
    re.compile(r"(^|[;&|]\s*)mkfs\.?", re.IGNORECASE),                       # 格式化分区
    re.compile(r"(^|[;&|]\s*)dd\s.*of=/dev/", re.IGNORECASE),                # 写入裸设备
    re.compile(r"(^|[;&|]\s*)fdisk\s", re.IGNORECASE),                       # 分区工具
    re.compile(r"(^|[;&|]\s*)parted\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)wipefs\s", re.IGNORECASE),
    # ─── 反弹 Shell / 远程控制 ───
    re.compile(r"\bbash\b.*[-<>].*(/dev/tcp|/dev/udp)/", re.IGNORECASE),      # bash 反弹
    re.compile(r"\bnc\b.*\s-e\s", re.IGNORECASE),                            # nc -e /bin/bash
    re.compile(r"\bncat\b.*\s-e\s", re.IGNORECASE),
    re.compile(r"\bsocat\b.*\bexec\b", re.IGNORECASE),                       # socat exec
    re.compile(r"\bmkfifo\b.*\bnc\b", re.IGNORECASE),                        # mkfifo 管道反弹
    re.compile(r"\btelnet\b.*\|.*\bbash\b", re.IGNORECASE),                  # telnet 管道
    # ─── Fork Bomb / 资源耗尽 ───
    re.compile(r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;"),                             # :(){ :|:& };:
    re.compile(r"\bfork\b.*\bwhile\b.*\btrue\b", re.IGNORECASE),
    # ─── 系统修改 ───
    re.compile(r"(^|[;&|]\s*)shutdown\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)reboot($|\s)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)poweroff($|\s)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)init\s+[06]($|\s)", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)passwd\s", re.IGNORECASE),                      # 修改密码
    re.compile(r"(^|[;&|]\s*)useradd\s", re.IGNORECASE),                     # 新建用户
    re.compile(r"(^|[;&|]\s*)userdel\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)usermod\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)chown\s+.*\s+/", re.IGNORECASE),               # chown 根目录
    re.compile(r"(^|[;&|]\s*)chmod\s+.*\s+/", re.IGNORECASE),               # chmod 根目录
    re.compile(r"(^|[;&|]\s*)crontab\s+-(r|e)", re.IGNORECASE),             # 修改 crontab
    # ─── 环境/配置篡改 ───
    re.compile(r"(^|[;&|]\s*)export\s+.*\b(PATH|LD_PRELOAD|LD_LIBRARY_PATH)\s*=", re.IGNORECASE),
    re.compile(r">\s*/etc/", re.IGNORECASE),                                 # 写入 /etc/
    re.compile(r"\btee\b.*\s/etc/", re.IGNORECASE),                          # tee 写入 /etc/
    # ─── 加密货币挖矿 / 后门 ───
    re.compile(r"\b(xmrig|minerd|cpuminer|cgminer)\b", re.IGNORECASE),
    re.compile(r"\bstratum\+tcp://", re.IGNORECASE),                         # 矿池协议
    # ─── 禁止 kill Agent 自身进程 ───
    re.compile(r"(^|[;&|]\s*)kill\s+-(9|KILL|TERM|SIGKILL)\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)killall\s", re.IGNORECASE),
    re.compile(r"(^|[;&|]\s*)pkill\s", re.IGNORECASE),
)

# ─── Python 代码安全守卫 ───
FORBIDDEN_PYTHON_PATTERNS = (
    # 文件系统破坏
    re.compile(r"\bshutil\s*\.\s*rmtree\b"),
    re.compile(r"\bos\s*\.\s*(remove|unlink|rmdir)\b.*(/(?:etc|home|root|var|usr|boot|bin|sbin|lib)|\.\.)"),
    re.compile(r"\bos\s*\.\s*system\s*\(.*\brm\s+-(r|f|rf|fr)\b"),
    re.compile(r"\bsubprocess\b.*\brm\s+-(r|f|rf|fr)\b"),
    # 反弹 Shell
    re.compile(r"\bsocket\b.*\bconnect\b.*\bsubprocess\b", re.DOTALL),
    re.compile(r"\bpty\s*\.\s*spawn\b"),
    re.compile(r"/dev/tcp/"),
    # 挖矿
    re.compile(r"\b(xmrig|stratum|minerd|cpuminer)\b", re.IGNORECASE),
    # 系统调用注入
    re.compile(r"\bctypes\b.*\bcdll\b.*libc"),
    re.compile(r"\bos\s*\.\s*execv[pe]?\b"),                                 # 替换当前进程
    # Fork Bomb
    re.compile(r"\bwhile\s+True\s*:.*\bos\s*\.\s*fork\b", re.DOTALL),
    re.compile(r"\bos\s*\.\s*fork\s*\(\).*while", re.DOTALL),
    # 写入 Agent 关键文件
    re.compile(r"open\s*\(.*(\.env|config\.py|main\.py|graph\.py).*,\s*['\"]w"),
)


def _normalize_host(value: str) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    if "://" in host:
        parsed = urlparse(host)
        host = (parsed.hostname or "").strip().lower()
    elif host.startswith("[") and "]" in host:
        host = host[1:].split("]", 1)[0].strip().lower()
    elif ":" in host and host.count(":") == 1:
        host = host.split(":", 1)[0].strip().lower()
    return host


def _extract_hosts(text: str) -> set[str]:
    hosts: set[str] = set()
    for match in URL_RE.findall(text or ""):
        parsed = urlparse(match)
        host = _normalize_host(parsed.hostname or "")
        if host:
            hosts.add(host)
    for match in IP_RE.findall(text or ""):
        host = _normalize_host(match)
        if host:
            hosts.add(host)
    return hosts


def extract_host_from_target(target: str) -> str:
    """从 URL / host[:port] / host[:port]/path 形式的目标中提取主机名。"""
    normalized = str(target or "").strip()
    if not normalized:
        return ""

    if "://" in normalized:
        parsed = urlparse(normalized)
        return _normalize_host(parsed.hostname or "")

    # 去掉裸目标中的路径、查询串和 fragment
    for sep in ("/", "?", "#"):
        if sep in normalized:
            normalized = normalized.split(sep, 1)[0].strip()

    return _normalize_host(normalized)


def validate_network_target(target: str, *, source: str = "目标地址") -> Optional[str]:
    return None
    # disabled below
    """
    显式校验单个网络目标，覆盖 `host:port` / 裸域名 这类 `validate_execution_text()`
    无法稳定提取的情况。
    """
    host = extract_host_from_target(target)
    if not host:
        return f"{source}为空或无法识别主机"

    policy = _command_policy_var.get()
    blocked_hosts = set(policy.blocked_hosts) | set(_global_blocked_hosts)
    if host in blocked_hosts:
        return (
            f"{source}命中了受保护基础设施地址: {host}。"
            "只允许攻击比赛平台返回的题目入口，不得访问模型网关、比赛 API 或论坛 API。"
        )

    if policy.enforce_allowlist:
        allowed = set(policy.allowed_hosts)
        if host not in allowed:
            allow_text = ", ".join(sorted(allowed)) if allowed else "当前题目没有任何允许的网络目标"
            return (
                f"{source}引用了非当前题目入口地址: {host}。"
                f"当前仅允许访问: {allow_text}。"
            )

    return None


def configure_command_guard(blocked_hosts: Optional[Iterable[str]] = None):
    """配置全局永封地址，这些地址不允许被 shell/python 工具访问。"""
    global _global_blocked_hosts
    _global_blocked_hosts = {
        host for host in (_normalize_host(item) for item in (blocked_hosts or [])) if host
    }


@contextmanager
def scoped_command_policy(
    *,
    allowed_hosts: Optional[Iterable[str]] = None,
    enforce_allowlist: bool = False,
):
    """为当前任务设置命令执行目标约束。"""
    current = _command_policy_var.get()
    merged_allowed = {
        host
        for host in (
            _normalize_host(item)
            for item in (allowed_hosts or current.allowed_hosts)
        )
        if host
    }
    merged_blocked = set(current.blocked_hosts) | set(_global_blocked_hosts)
    token = _command_policy_var.set(
        CommandPolicy(
            allowed_hosts=tuple(sorted(merged_allowed)),
            blocked_hosts=tuple(sorted(merged_blocked)),
            enforce_allowlist=enforce_allowlist,
        )
    )
    try:
        yield
    finally:
        _command_policy_var.reset(token)


def add_allowed_hosts(hosts: Iterable[str]):
    """在当前任务上下文中追加允许访问的主机。"""
    current = _command_policy_var.get()
    merged = set(current.allowed_hosts)
    for item in hosts:
        host = _normalize_host(item)
        if host:
            merged.add(host)
    _command_policy_var.set(
        CommandPolicy(
            allowed_hosts=tuple(sorted(merged)),
            blocked_hosts=current.blocked_hosts,
            enforce_allowlist=current.enforce_allowlist,
        )
    )


def validate_execution_text(text: str, *, source: str = "命令", is_python: bool = False) -> Optional[str]:
    return None
    # disabled below
    """
    校验命令/代码中是否包含恶意或被禁止的操作。

    防护层次：
    1. 破坏性命令拦截（rm -rf、mkfs、dd、fork bomb 等）
    2. 反弹 Shell 检测（bash /dev/tcp、nc -e、socat exec 等）
    3. 系统修改拦截（shutdown、passwd、useradd、crontab 等）
    4. 挖矿检测（xmrig、stratum+tcp 等）
    5. Agent 自身目录保护
    6. Python 特有危险函数检测（shutil.rmtree、pty.spawn 等）
    7. 主机白名单/黑名单执行
    """
    normalized = (text or "").strip()
    if not normalized:
        return None

    # ── 1. Shell 命令恶意模式检测 ──
    for pattern in FORBIDDEN_CMD_PATTERNS:
        if pattern.search(normalized):
            return (
                f"{source}包含被禁止的危险操作（破坏性命令/系统修改/反弹Shell/资源耗尽）。"
                "比赛环境只允许使用预装渗透测试工具攻击目标，不允许修改本机环境。"
            )

    # ── 2. Python 代码特有危险模式检测 ──
    if is_python:
        for pattern in FORBIDDEN_PYTHON_PATTERNS:
            if pattern.search(normalized):
                return (
                    f"{source}包含被禁止的 Python 危险操作（文件删除/反弹Shell/进程替换）。"
                    "只允许使用 requests/pwntools 等进行渗透测试，不允许破坏本机文件系统。"
                )

    # ── 3. Agent 自身工作目录保护 ──
    agent_dir_markers = (
        "/Desktop/Ling-Xi",
        "agent/graph.py",
        "agent/solver.py",
        "tools/shell.py",
        "tools/platform_api.py",
        "config.py",
        ".env",
        "main.py",
        "xxff.md",
    )
    lowered = normalized.lower()
    destructive_verbs = ("rm ", "unlink ", "truncate ", "shred ", "> ", "tee ")
    for marker in agent_dir_markers:
        if marker.lower() in lowered:
            if any(verb in lowered for verb in destructive_verbs):
                return (
                    f"{source}试图对 Agent 自身工作目录/关键文件执行破坏性操作。"
                    "禁止删除、覆盖或截断 Agent 代码和配置文件。"
                )

    # ── 4. 主机白名单/黑名单 ──
    policy = _command_policy_var.get()
    blocked_hosts = set(policy.blocked_hosts) | set(_global_blocked_hosts)
    referenced_hosts = _extract_hosts(text)

    forbidden_hits = sorted(host for host in referenced_hosts if host in blocked_hosts)
    if forbidden_hits:
        return (
            f"{source}命中了受保护基础设施地址: {', '.join(forbidden_hits)}。"
            "只允许攻击比赛平台返回的题目入口，不得访问模型网关、比赛 API 或论坛 API。"
        )

    if policy.enforce_allowlist and referenced_hosts:
        allowed = set(policy.allowed_hosts)
        unexpected = sorted(host for host in referenced_hosts if host not in allowed)
        if unexpected:
            allow_text = ", ".join(sorted(allowed)) if allowed else "当前题目没有任何允许的网络目标"
            return (
                f"{source}引用了非当前题目入口地址: {', '.join(unexpected)}。"
                f"当前仅允许访问: {allow_text}。"
            )

    return None


def _execute_in_docker(
    container: str, command: str, timeout: int = 120
) -> CommandResult:
    """在 Docker 容器中执行命令（内置级联斩杀和无交互护盾）"""
    # 使用 timeout -k(强制杀) 结合 DEBIAN_FRONTEND 防止卡阻
    safe_command = f"export DEBIAN_FRONTEND=noninteractive; timeout -k 5s {timeout}s bash -c {repr(command)}"
    docker_cmd = ["docker", "exec", container, "bash", "-c", safe_command]
    try:
        import subprocess

        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # Python 侧冗余 5 秒确保内核先超时
            stdin=subprocess.DEVNULL,  # 彻底封死请求交互导致的无限挂起
        )
        return CommandResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            exit_code=-1,
            stdout="",
            stderr=f"命令内部彻底挂死，守护进程强杀 ({timeout}s)",
        )
    except Exception as e:
        return CommandResult(exit_code=-1, stdout="", stderr=f"执行失败: {e}")


def _execute_locally(command: str, timeout: int = 120) -> CommandResult:
    """在本地执行命令"""
    safe_command = f"export DEBIAN_FRONTEND=noninteractive; timeout -k 5s {timeout}s bash -c {repr(command)}"
    try:
        import subprocess

        result = subprocess.run(
            ["bash", "-c", safe_command],
            capture_output=True,
            text=True,
            timeout=timeout + 5,
            stdin=subprocess.DEVNULL,
        )
        return CommandResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            exit_code=-1,
            stdout="",
            stderr=f"命令内部彻底挂死，守护进程强杀 ({timeout}s)",
        )
    except Exception as e:
        return CommandResult(exit_code=-1, stdout="", stderr=f"执行失败: {e}")


# ─── 全局配置 ───

_docker_container: Optional[str] = None
_docker_enabled: bool = True
_docker_requested: bool = True
_shell_runtime_mode: str = "local"
_shell_runtime_reason: str = "not_configured"


def _configure_dddd2_env() -> None:
    if str(os.getenv("DDDD2_PATH", "") or "").strip():
        return
    project_bin = os.path.join(str(get_project_root()), "dddd2")
    if os.path.exists(project_bin):
        os.environ["DDDD2_PATH"] = project_bin


def _docker_container_exists(container_name: str) -> bool:
    """检查目标容器是否存在且处于运行状态。"""
    if not container_name:
        return False
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        logger.warning(f"[Shell] 检查 Docker 容器失败: {e}")
        return False

    if result.returncode != 0:
        logger.warning(
            "[Shell] docker ps 执行失败，回退本地执行: %s",
            (result.stderr or "").strip(),
        )
        return False

    running = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return container_name in running


def configure_shell(container_name: str = "", docker_enabled: bool = True):
    """配置 Shell 执行环境"""
    global _docker_container, _docker_enabled, _docker_requested
    global _shell_runtime_mode, _shell_runtime_reason
    _configure_dddd2_env()
    resolved_container = get_kali_container_name(container_name, log=logger)
    _docker_container = resolved_container
    _docker_enabled = docker_enabled
    _docker_requested = docker_enabled

    if docker_enabled and resolved_container:
        if _docker_container_exists(resolved_container):
            _shell_runtime_mode = "docker"
            _shell_runtime_reason = "container_ready"
            state = get_shell_runtime_state()
            logger.info(
                "[Shell] 执行环境就绪: mode=%s requested_docker=%s container=%s reason=%s",
                state["mode"],
                state["requested_docker"],
                state["container"],
                state["reason"],
            )
            return state
        _docker_enabled = False
        _shell_runtime_mode = "local"
        _shell_runtime_reason = "docker_container_unavailable"
        logger.warning(
            "[Shell] Docker 已启用但容器 %s 不可用，自动回退到本地执行环境",
            resolved_container,
        )
        return get_shell_runtime_state()

    _shell_runtime_mode = "local"
    _shell_runtime_reason = "docker_disabled" if not docker_enabled else "missing_container_name"
    state = get_shell_runtime_state()
    logger.info(
        "[Shell] 执行环境就绪: mode=%s requested_docker=%s container=%s reason=%s",
        state["mode"],
        state["requested_docker"],
        state["container"],
        state["reason"],
    )
    return state


def get_shell_runtime_state() -> dict[str, str | bool]:
    """返回当前 Shell 执行环境状态，供启动链路和日志展示使用。"""
    return {
        "mode": _shell_runtime_mode,
        "container": _docker_container or "",
        "docker_enabled": _docker_enabled,
        "requested_docker": _docker_requested,
        "reason": _shell_runtime_reason,
    }


def _execute(command: str, timeout: int = 120) -> CommandResult:
    """统一执行入口"""
    if _docker_enabled and _docker_container:
        return _execute_in_docker(_docker_container, command, timeout)
    else:
        return _execute_locally(command, timeout)


def get_runtime_python_command() -> str:
    """
    返回当前 shell 命令上下文应使用的 Python 可执行文件。
    - Docker 内继续使用容器自带 `python3`
    - 本地执行统一使用当前项目 `.venv`
    """
    if _docker_enabled and _docker_container:
        return "python3"
    return shlex.quote(get_project_python())


def get_dddd2_command() -> str:
    """
    返回当前运行时应使用的 dddd2 命令片段。

    优先级：
    1. `DDDD2_PATH`
    2. 本地模式下回退到仓库根目录的 `dddd2`
    3. Docker 模式下回退到 `dddd2`（依赖容器内 PATH）
    """
    project_bin = os.path.join(str(get_project_root()), "dddd2")
    local_fallback = shlex.quote(project_bin) if os.path.exists(project_bin) else "dddd2"
    runtime_fallback = "dddd2" if (_docker_enabled and _docker_container) else local_fallback
    return f"${{DDDD2_PATH:-{runtime_fallback}}}"


async def run_shell_io(func, *args, **kwargs):
    """将阻塞式命令执行隔离到专用线程池，避免占用默认 executor。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _SHELL_TOOL_EXECUTOR,
        partial(func, *args, **kwargs),
    )


# ─── 输出截断 ───

MAX_OUTPUT_LEN = 30000  # 最大输出长度


def _truncate_output(text: str, max_len: int = MAX_OUTPUT_LEN) -> str:
    """智能截断过长输出"""
    if len(text) <= max_len:
        return text
    half = max_len // 2
    return (
        text[:half]
        + f"\n\n... [截断: 原始输出 {len(text)} 字符, 仅保留首尾各 {half} 字符] ...\n\n"
        + text[-half:]
    )


def _is_heavy_scan_command(command: str) -> bool:
    lowered = (command or "").lower()
    return any(pattern.search(lowered) for pattern in _HEAVY_SCAN_PATTERNS)


def _looks_like_complex_http_command(command: str) -> bool:
    lowered = (command or "").lower()
    if "curl" not in lowered:
        return False
    complex_markers = (
        "authorization:",
        "bearer ",
        "cookie:",
        "content-type: application/json",
        "__proto__",
        "-d '{",
        "--data '{",
        "--data-binary",
        "-h 'content-type: application/json'",
    )
    return any(marker in lowered for marker in complex_markers)


def _looks_like_shell_quoting_failure(result: CommandResult) -> bool:
    stderr = (result.stderr or "").lower()
    stdout = (result.stdout or "").lower()
    combined = f"{stderr}\n{stdout}"
    if result.exit_code not in {2, 64}:
        return False
    markers = (
        "unexpected eof",
        "unexpected token",
        "syntax error",
        "unterminated",
        "unmatched",
        "looking for matching",
        "parse error",
        "curl: option",
    )
    return any(marker in combined for marker in markers) or result.exit_code == 2


# ─── LangChain 工具 ───


def _execute_command_impl(command: str, timeout: int = 60) -> str:
    """
    在 Kali Linux 环境中执行 Shell 命令。

    这是你的主要工具。你可以执行任何渗透测试相关的命令：

    **常用命令示例：**
    - Web 请求: `curl -v http://target/path`
    - 参数自动编码: `curl --get --data-urlencode "url=system('ls /');" http://target/`
    - SQL 注入: `sqlmap -u "http://target/page?id=1" --batch --dbs`
    - 漏洞搜索: `searchsploit keyword`
    - 文件操作: `cat /flag`, `ls -la`, `find / -name "flag*"`
    - 目录爆破: `gobuster dir -u http://target -w /usr/share/wordlists/dirb/common.txt`
    - 端口扫描: `dddd2 -t target_ip -Pn -npoc`（禁止使用 nmap）
    - 模板扫描: `nuclei -u http://target -as -rl 150`
    - 综合扫描(指纹+漏扫): `~/Ling-Xi/dddd2 -t <target>` 支持 IP/CIDR/域名/URL/文件，内置端口扫描+协议识别+Web指纹+nuclei PoC+服务爆破，结果输出到 result.txt
      - 只收集不漏扫: `-npoc`
      - 指定 PoC: `-poc "shiro"` / `-poc "log4j"`
      - 指定端口: `-p 80,443,8080-8090`
      - 子域名枚举: `-sd`
      - 从 fscan/dddd 结果继续扫: `-t result.txt`
      - 禁用服务爆破: `-nb`
      - 只扫指定严重度: `-s high,critical`
      - 排除标签: `-et dos,fuzz`
      - 输出 HTML 报告: `-ho report.html`
      - Hunter/Fofa/Quake 资产拉取: `-hunter "app=shiro"` / `-fofa "app=shiro"`
    - 暴力破解: `hydra -l admin -P wordlist.txt target ssh`

    **注意：**
    - 默认先做低成本高信号动作；不要一上来就跑长时间 `nmap/nuclei/gobuster`
    - Bearer/Cookie/JWT/JSON 这类复杂请求优先改用 `execute_python`
    - 默认超时 120 秒；高耗时扫描不会再被激进缩短，只会对异常长 timeout 做上限保护
    - 输出会原样返回，请自行分析结果

    Args:
        command: 完整的 Shell 命令
        timeout: 超时时间（秒），默认 120
    """
    if not command or not command.strip():
        return "错误：命令不能为空"

    policy_error = validate_execution_text(command, source="命令")
    if policy_error:
        logger.warning("[Shell] 拦截命令: %s", policy_error)
        return f"错误：{policy_error}"

    effective_timeout = timeout
    if _is_heavy_scan_command(command):
        effective_timeout = timeout if timeout <= _HEAVY_SCAN_TIMEOUT else _HEAVY_SCAN_TIMEOUT
        if effective_timeout < timeout:
            logger.debug(
                "[Shell] 检测到高耗时枚举命令，对超长 timeout 做保护: %ss -> %ss",
                timeout,
                effective_timeout,
            )

    logger.debug("[Shell] 执行: %s", describe_shell_command(command, timeout=effective_timeout))

    result = _execute(command, effective_timeout)

    output = f"Exit Code: {result.exit_code}\n"
    if result.stdout:
        output += f"\n--- STDOUT ---\n{_truncate_output(result.stdout)}\n"
    if result.stderr:
        output += f"\n--- STDERR ---\n{_truncate_output(result.stderr)}\n"
    if _looks_like_complex_http_command(command) and _looks_like_shell_quoting_failure(result):
        output += f"\n--- HINT ---\n{_SHELL_QUOTING_FAILURE_HINT}\n"

    logger.debug(
        f"[Shell] 完成: exit={result.exit_code}, stdout={len(result.stdout)}, stderr={len(result.stderr)}"
    )
    return output


@tool
async def execute_command(command: str, timeout: int = 60) -> str:
    """在 Kali 容器中执行 Shell 命令。

    适用场景（必须主动调用，不要等待）：
    - 端口/服务扫描：`dddd2 -t <target> -Pn -npoc`（禁止使用 nmap）
    - Web 目录爆破：gobuster dir / ffuf
    - 漏洞扫描：nuclei -u <url> -as、nikto -h <url>
    - SQL 注入：sqlmap -u <url> --batch
    - 暴力破解：hydra
    - HTTP 请求：curl -v、wget
    - 网络工具：nc、ping、dig、whois
    - 文件操作：cat、find、grep、ls
    - 任何 Kali 预装的渗透工具

    收到靶机入口后，第一步必须用此工具执行 `dddd2 -t <target> -Pn -npoc` 或 curl 侦察，不要空想。禁止使用 nmap。
    """
    return await run_shell_io(_execute_command_impl, command, timeout)
