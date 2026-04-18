from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
from logging.handlers import RotatingFileHandler
from typing import Any
from urllib.parse import urlparse

_FLAG_RE = re.compile(r"\b(?:flag|ctf)\{[^{}\r\n]{1,256}\}", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(Agent-Token\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)\b(Authorization\s*[:=]\s*Bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)\b(Bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)\b((?:api[_-]?key|session(?:id)?|cookie|password|passwd|jwt|token|secret)\s*[:=]\s*)([^\s,;]+)"),
)
_LOGGING_FILTER_INSTALLED = False
_DEFAULT_LOG_FILE = "lingxi.log"
_DEFAULT_CONSOLE_LOG_LEVEL = "INFO"
_DEFAULT_FILE_LOG_LEVEL = "INFO"
_DEFAULT_LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_LOG_FILE_BACKUP_COUNT = 5


def unsafe_raw_logs_enabled() -> bool:
    return str(os.getenv("LING_XI_UNSAFE_RAW_LOGS", "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_log_file(log_file: str | None = None) -> str:
    normalized = str(log_file or "").strip()
    if normalized:
        return normalized
    env_value = str(os.getenv("LINGXI_LOG_FILE", "") or "").strip()
    if env_value:
        return env_value
    return _DEFAULT_LOG_FILE


def _resolve_log_level(env_name: str, default: str) -> int:
    raw_value = str(os.getenv(env_name, "") or "").strip().upper()
    if not raw_value:
        raw_value = default.upper()
    level = logging.getLevelName(raw_value)
    return level if isinstance(level, int) else getattr(logging, default.upper(), logging.INFO)


def _resolve_int_env(env_name: str, default: int, *, minimum: int = 0) -> int:
    raw_value = str(os.getenv(env_name, "") or "").strip()
    if not raw_value:
        return default
    try:
        return max(minimum, int(raw_value))
    except (TypeError, ValueError):
        return default


def _hash_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def flag_fingerprint(flag: str | None) -> str:
    normalized = str(flag or "").strip()
    if not normalized:
        return "<flag:empty>"
    return f"<flag:{_hash_text(normalized)}>"


def safe_endpoint_label(url: str | None) -> str:
    normalized = str(url or "").strip()
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return normalized
    host = parsed.hostname or parsed.netloc
    port = f":{parsed.port}" if parsed.port else ""
    if parsed.path and parsed.path not in {"", "/"}:
        return f"{parsed.scheme}://{host}{port}/<redacted>"
    return f"{parsed.scheme}://{host}{port}"


def extract_target_hosts(text: str | None) -> list[str]:
    normalized = str(text or "")
    hosts: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.findall(normalized):
        parsed = urlparse(match)
        host = (parsed.hostname or "").strip()
        if host and host not in seen:
            seen.add(host)
            hosts.append(host)
    for match in _IP_RE.findall(normalized):
        if match not in seen:
            seen.add(match)
            hosts.append(match)
    return hosts


def redact_sensitive_text(text: str | None) -> str:
    normalized = str(text or "")
    if not normalized or unsafe_raw_logs_enabled():
        return normalized

    redacted = normalized
    redacted = _FLAG_RE.sub(lambda match: flag_fingerprint(match.group(0)), redacted)
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}<redacted-token>", redacted)
    redacted = _URL_RE.sub(lambda match: safe_endpoint_label(match.group(0)), redacted)
    return redacted


def redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {key: redact_log_arg(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_log_arg(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_log_arg(item) for item in value)
    if isinstance(value, set):
        return {redact_log_arg(item) for item in value}
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if unsafe_raw_logs_enabled():
            return True
        try:
            record.msg = redact_log_arg(record.msg)
            if isinstance(record.args, tuple):
                record.args = tuple(redact_log_arg(arg) for arg in record.args)
            elif isinstance(record.args, dict):
                record.args = {key: redact_log_arg(value) for key, value in record.args.items()}
        except Exception:
            return True
        return True


_FILTER = RedactingFilter()

_LEVEL_COLORS = {
    "DEBUG": "\033[90m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import sys
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        color = _LEVEL_COLORS.get(record.levelname, "") if use_color else ""
        reset = _RESET if use_color else ""
        short_name = record.name.rsplit(".", 1)[-1]
        time_str = self.formatTime(record, "%H:%M:%S")
        level = f"{color}{record.levelname:<5}{reset}"
        msg = record.getMessage()
        return f"{time_str} {level} {short_name:<12} {msg}"


def setup_logging(log_file: str | None = None) -> None:
    resolved_log_file = resolve_log_file(log_file)
    console_level = _resolve_log_level("LINGXI_CONSOLE_LOG_LEVEL", _DEFAULT_CONSOLE_LOG_LEVEL)
    file_level = _resolve_log_level("LINGXI_FILE_LOG_LEVEL", _DEFAULT_FILE_LOG_LEVEL)
    max_bytes = _resolve_int_env(
        "LINGXI_LOG_FILE_MAX_BYTES",
        _DEFAULT_LOG_FILE_MAX_BYTES,
        minimum=1,
    )
    backup_count = _resolve_int_env(
        "LINGXI_LOG_FILE_BACKUP_COUNT",
        _DEFAULT_LOG_FILE_BACKUP_COUNT,
        minimum=0,
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        try:
            handler.close()
        except Exception:
            pass
    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setLevel(console_level)
    stream.setFormatter(ColorFormatter())
    root.addHandler(stream)

    fh = RotatingFileHandler(
        resolved_log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setLevel(file_level)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(name)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(fh)

    for handler in root.handlers:
        handler.addFilter(_FILTER)

    global _LOGGING_FILTER_INSTALLED
    _LOGGING_FILTER_INSTALLED = True


def install_logging_redaction() -> None:
    global _LOGGING_FILTER_INSTALLED
    if _LOGGING_FILTER_INSTALLED:
        return
    root = logging.getLogger()
    for handler in root.handlers:
        handler.addFilter(_FILTER)
    _LOGGING_FILTER_INSTALLED = True


def describe_shell_command(command: str | None, *, timeout: int | None = None) -> str:
    normalized = str(command or "").strip()
    if not normalized:
        return "tool=unknown"
    try:
        tool_name = shlex.split(normalized, posix=False)[0]
    except ValueError:
        tool_name = normalized.split(" ", 1)[0]
    hosts = extract_target_hosts(normalized)
    target = ",".join(hosts[:3]) if hosts else "n/a"
    parts = [f"tool={tool_name}", f"target={target}"]
    if timeout is not None:
        parts.append(f"timeout={int(timeout)}s")
    if any(token in normalized.lower() for token in ("sqlmap", "nikto", "gobuster", "ffuf", "nmap", "hydra")):
        parts.append("category=automation")
    elif tool_name.lower() in {"curl", "wget", "httpx"}:
        parts.append("category=http")
    else:
        parts.append("category=command")
    return " ".join(parts)


def describe_python_script(code: str | None, *, purpose: str = "python_exec") -> str:
    normalized = str(code or "")
    hosts = extract_target_hosts(normalized)
    target = ",".join(hosts[:3]) if hosts else "n/a"
    line_count = len([line for line in normalized.splitlines() if line.strip()])
    return (
        f"purpose={purpose} chars={len(normalized)} "
        f"lines={line_count} target={target} sha1={_hash_text(normalized or purpose)}"
    )
