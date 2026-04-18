"""
Level 2 CVE PoC 工具
====================
将本地可选 PoC 扩展封装成 Agent 可调用工具。
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from tools.shell import (
    _execute,
    _truncate_output,
    get_runtime_python_command,
    run_shell_io,
    validate_execution_text,
    validate_network_target,
)


POC_ROOT = Path(__file__).resolve().parent.parent / "extensions" / "level2-pocs"

_POC_SPECS: dict[str, dict[str, Any]] = {
    "1panel": {
        "script": "cve-2024-39907.py",
        "aliases": ("1panel", "panel", "cve-2024-39907"),
        "default_target_hint": "http(s)://host:port",
    },
    "1panel-postauth": {
        "script": "1panel-postauth-rce.py",
        "aliases": ("1panel-postauth", "1panel-api", "1panel-cronjob", "1panel-auth"),
        "default_target_hint": "http(s)://host:port",
    },
    "comfyui-manager": {
        "script": "cve-2025-67303.py",
        "aliases": ("comfyui-manager", "comfyui", "manager", "cve-2025-67303"),
        "default_target_hint": "http(s)://host:port, 默认 8188",
    },
    "gradio": {
        "script": "CVE-2024-1561",
        "aliases": ("gradio", "cve-2024-1561", "cve-2024-1728", "cve-2024-4325"),
        "default_target_hint": "http(s)://host:port, 默认 7860",
    },
}
_FLAG_PATTERN = re.compile(r"flag\{[^}\n]{1,512}\}", flags=re.IGNORECASE)
_GRADIO_DEFAULT_HUNT_PATHS = (
    "/flag",
    "/flag.txt",
    "/app/flag",
    "/app/flag.txt",
    "/workspace/flag",
    "/workspace/flag.txt",
    "/home/ctf/flag",
    "/home/ctf/flag.txt",
    "/root/flag",
    "/root/flag.txt",
    "/tmp/flag",
    "/tmp/flag.txt",
)
_1PANEL_HINT_PREFIXES = (
    "path=",
    "paths=",
    "shell_path=",
    "shell_paths=",
    "url=",
    "urls=",
    "shell_url=",
    "shell_urls=",
    "webroot=",
    "webroots=",
    "root=",
    "roots=",
    "base=",
    "bases=",
    "origin=",
    "origins=",
)


def level2_poc_extension_available() -> bool:
    return POC_ROOT.exists()


def _ensure_level2_poc_available() -> None:
    if not level2_poc_extension_available():
        raise FileNotFoundError(
            "公开仓库未附带 Level2 私有 PoC 扩展；如需启用，请在 extensions/level2-pocs/ 下自行挂接。"
        )


def _script_path_for(canonical: str) -> Path:
    return POC_ROOT / str(_POC_SPECS[canonical]["script"])


def _resolve_poc_name(name: str) -> str:
    normalized = str(name or "").strip().lower()
    if not normalized:
        raise ValueError("poc_name 不能为空")
    for canonical, spec in _POC_SPECS.items():
        if normalized == canonical or normalized in spec["aliases"]:
            return canonical
    supported = ", ".join(sorted(_POC_SPECS))
    raise ValueError(f"未知 poc_name: {name}。支持: {supported}")


def _safe_quote_parts(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts if str(part or "").strip())


def _mask_sensitive_command(command: str) -> str:
    masked = str(command or "")
    markers = (
        "psession=",
        "LEVEL2_1PANEL_PSESSION=",
    )
    for marker in markers:
        if marker not in masked:
            continue
        prefix, suffix = masked.split(marker, 1)
        token_end = len(suffix)
        for idx, ch in enumerate(suffix):
            if ch.isspace() or ch in "\"'":
                token_end = idx
                break
        token = suffix[:token_end]
        replacement = f"{marker}{token[:4]}***" if token else f"{marker}***"
        masked = prefix + replacement + suffix[token_end:]
    return masked


def _extract_flag_candidates(text: str) -> list[str]:
    return _FLAG_PATTERN.findall(str(text or ""))


def _parse_gradio_hunt_paths(extra: str) -> list[str]:
    raw = str(extra or "").strip()
    candidates = re.split(r"[\n,]+", raw) if raw else list(_GRADIO_DEFAULT_HUNT_PATHS)
    normalized: list[str] = []
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if not value.startswith("/"):
            value = "/" + value.lstrip("/")
        if value not in normalized:
            normalized.append(value)
    return normalized or list(_GRADIO_DEFAULT_HUNT_PATHS)


def _parse_1panel_extra(extra: str) -> tuple[str, str, str]:
    raw = str(extra or "").strip()
    psession = ""
    hint_parts: list[str] = []
    command_parts: list[str] = []
    if not raw:
        return "id", psession, ""

    for part in raw.split(";"):
        fragment = str(part or "").strip()
        if not fragment:
            continue
        normalized = fragment.lower()
        if normalized.startswith("psession="):
            psession = fragment.split("=", 1)[1].strip()
            continue
        if any(normalized.startswith(prefix) for prefix in _1PANEL_HINT_PREFIXES):
            hint_parts.append(fragment)
            continue
        command_parts.append(fragment)

    command = "id"
    if command_parts:
        first = command_parts[0]
        if first.startswith("cmd="):
            first_value = first.split("=", 1)[1].strip()
            command = ";".join([first_value] + command_parts[1:]).strip() or command
        else:
            command = ";".join(command_parts).strip() or command

    return command, psession, ";".join(hint_parts)


def _format_poc_result(
    canonical: str,
    target: str,
    mode: str,
    command: str,
    result: Any,
    *,
    attempt: str = "",
    note: str = "",
) -> str:
    output = (
        f"PoC: {canonical}\n"
        f"Target: {target}\n"
        f"Mode: {mode}\n"
    )
    if attempt:
        output += f"Attempt: {attempt}\n"
    if note:
        output += f"{note}\n"
    output += (
        f"Command: {_mask_sensitive_command(command)}\n"
        f"Exit Code: {result.exit_code}\n"
    )
    if result.stdout:
        output += f"\n--- STDOUT ---\n{_truncate_output(result.stdout)}\n"
    if result.stderr:
        output += f"\n--- STDERR ---\n{_truncate_output(result.stderr)}\n"
    return output


def _build_level2_poc_command(
    poc_name: str,
    target: str,
    mode: str,
    extra: str = "",
) -> tuple[str, int]:
    canonical = _resolve_poc_name(poc_name)
    _ensure_level2_poc_available()
    spec = _POC_SPECS[canonical]
    script_path = _script_path_for(canonical)
    if not script_path.exists():
        raise FileNotFoundError(f"PoC 扩展脚本不存在: {script_path}")

    normalized_target = str(target or "").strip()
    if not normalized_target:
        raise ValueError(
            f"target 不能为空。{canonical} 期望目标格式: {spec['default_target_hint']}"
        )

    normalized_mode = str(mode or "").strip().lower()
    timeout = 90

    if canonical == "gradio":
        base_parts = [str(script_path), "--url", normalized_target]
        if normalized_mode == "check":
            parts = base_parts + ["--file", "/etc/hostname"]
        elif normalized_mode == "hunt_flag":
            path_or_url = str(extra or "").strip() or "/flag"
            parts = base_parts + ["--file", path_or_url]
            timeout = 120
        elif normalized_mode == "exec":
            path_or_url = str(extra or "").strip() or "/flag"
            parts = base_parts + ["--file", path_or_url]
        else:
            raise ValueError("gradio 仅支持 mode=check|hunt_flag|exec")
        return _safe_quote_parts(parts), timeout

    python_cmd = get_runtime_python_command()
    base_parts = [python_cmd, str(script_path), normalized_target]

    if canonical == "comfyui-manager":
        if normalized_mode == "check":
            parts = base_parts + ["--check"]
        elif normalized_mode == "hunt_flag":
            parts = base_parts + ["--all"]
            timeout = 120
        elif normalized_mode == "exec":
            cmd = str(extra or "").strip() or "id"
            parts = base_parts + ["--rce", cmd]
            timeout = 120
        else:
            raise ValueError("comfyui-manager 仅支持 mode=check|hunt_flag|exec")
        return _safe_quote_parts(parts), timeout

    if canonical in {"1panel", "1panel-postauth"}:
        psession = os.getenv("LEVEL2_1PANEL_PSESSION", "").strip()
        command, psession_from_extra, hint = _parse_1panel_extra(extra)
        if psession_from_extra:
            psession = psession_from_extra
        if normalized_mode == "check":
            parts = base_parts + ["--check"]
            if canonical == "1panel" and hint:
                parts += ["--hint", hint]
        elif normalized_mode == "hunt_flag":
            parts = base_parts + ["--hunt-flag"]
            if canonical == "1panel" and hint:
                parts += ["--hint", hint]
            timeout = 180 if canonical == "1panel-postauth" else 120
        elif normalized_mode == "exec":
            parts = base_parts + ["--exec", command]
            if canonical == "1panel" and hint:
                parts += ["--hint", hint]
            timeout = 180 if canonical == "1panel-postauth" else 120
        else:
            raise ValueError(f"{canonical} 仅支持 mode=check|hunt_flag|exec")
        if psession:
            parts += ["--psession", psession]
        return _safe_quote_parts(parts), timeout

    raise ValueError(f"未实现的 poc_name: {canonical}")


def _run_level2_poc_impl(
    poc_name: str,
    target: str,
    mode: str = "check",
    extra: str = "",
    timeout: int = 0,
) -> str:
    target_error = validate_network_target(target, source="Level2 PoC 目标")
    if target_error:
        return f"错误：{target_error}"
    try:
        _ensure_level2_poc_available()
    except FileNotFoundError as exc:
        return f"错误：{exc}"

    canonical = _resolve_poc_name(poc_name)
    normalized_mode = str(mode or "").strip().lower()

    if canonical == "gradio" and normalized_mode == "hunt_flag":
        candidate_paths = _parse_gradio_hunt_paths(extra)
        _, default_timeout = _build_level2_poc_command(canonical, target, normalized_mode, candidate_paths[0])
        total_timeout = max(1, int(timeout or default_timeout))
        per_attempt_timeout = max(5, min(20, total_timeout // max(1, len(candidate_paths))))
        attempts: list[str] = []
        for index, file_path in enumerate(candidate_paths, start=1):
            command, _ = _build_level2_poc_command(canonical, target, normalized_mode, file_path)
            policy_error = validate_execution_text(command, source="Level2 PoC 命令")
            if policy_error:
                return f"错误：{policy_error}"
            result = _execute(command, per_attempt_timeout)
            attempts.append(
                _format_poc_result(
                    canonical,
                    target,
                    normalized_mode,
                    command,
                    result,
                    attempt=f"{index}/{len(candidate_paths)}",
                    note=f"File Candidate: {file_path}",
                )
            )
            if _extract_flag_candidates(result.stdout) or _extract_flag_candidates(result.stderr):
                break
        return "\n\n".join(attempts)

    command, default_timeout = _build_level2_poc_command(canonical, target, normalized_mode, extra)
    policy_error = validate_execution_text(command, source="Level2 PoC 命令")
    if policy_error:
        return f"错误：{policy_error}"

    effective_timeout = max(1, int(timeout or default_timeout))
    result = _execute(command, effective_timeout)
    return _format_poc_result(canonical, target, normalized_mode, command, result)


@tool
async def run_level2_cve_poc(
    poc_name: str,
    target: str,
    mode: str = "check",
    extra: str = "",
    timeout: int = 0,
) -> str:
    """
    运行本地 Level 2 CVE PoC。

    参数说明:
    - poc_name: 组件名，如 `1panel`、`1panel-postauth`、`comfyui-manager`、`gradio`
    - target: 目标地址，例如 `http://ip:10086`
    - mode: `check` / `hunt_flag` / `exec`
    - extra: `exec` 模式的命令或附加参数；对 `gradio`，`exec` 的 `extra` 是文件路径，`hunt_flag` 可传逗号/换行分隔的候选路径；对 `1panel`，可传 `psession=...;path=...;url=...;base=...`；对 `1panel-postauth`，可传 `cmd=...;psession=...`
    - timeout: 可选，自定义超时秒数
    """
    return await run_shell_io(_run_level2_poc_impl, poc_name, target, mode, extra, timeout)
