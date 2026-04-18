"""
自动 Flag 提取 + 智能提交
==========================
从命令输出中自动检测 flag{...} 并提交，作为兜底机制。
"""

from __future__ import annotations

import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Flag 正则模式
FLAG_PATTERNS = [
    re.compile(r"\bflag\{[a-zA-Z0-9:_\-]{3,160}\}"),
    re.compile(r"\bFLAG\{[a-zA-Z0-9:_\-]{3,160}\}", re.IGNORECASE),
    re.compile(r"\bctf\{[a-zA-Z0-9:_\-]{3,160}\}", re.IGNORECASE),
]

_LOW_CONFIDENCE_SUBSTRINGS = (
    "xxxx",
    "yyyy",
    "zzzz",
    "xxx",
    "yyy",
    "keya_keyb_keyc",
    "keya:",
    "keyb:",
    "keyc:",
    "placeholder",
    "example",
    "sample",
)
_RECORDED_FLAG_PATTERN = re.compile(r"\b(?:flag|ctf)\{[A-Za-z0-9:_\-]{1,200}\}", re.IGNORECASE)
_FORUM_FLAG_LOG_LOCK = threading.Lock()


def _get_forum_flag_log_path(path: Optional[str | Path] = None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    override = str(os.getenv("FORUM_FLAG_LOG_PATH", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "xxff.md"


def load_recorded_forum_flags(path: Optional[str | Path] = None) -> set[str]:
    """读取 xxff.md 中已经记录过的论坛 flag。"""
    log_path = _get_forum_flag_log_path(path)
    try:
        text = log_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    except OSError as exc:
        logger.warning("[Flag] 读取论坛 Flag 记录失败: %s", exc)
        return set()
    return {match.group(0).strip() for match in _RECORDED_FLAG_PATTERN.finditer(text)}


def has_recorded_forum_flag(flag: str, path: Optional[str | Path] = None) -> bool:
    """判断某个论坛 flag 是否已经写入 xxff.md。"""
    normalized = (flag or "").strip()
    if not normalized:
        return False
    return normalized in load_recorded_forum_flags(path)


def record_forum_flag_attempt(
    flag: str,
    challenge_id: int,
    *,
    scored: Optional[bool] = None,
    verified: Optional[bool] = None,
    message: str = "",
    path: Optional[str | Path] = None,
) -> bool:
    """
    把已实际提交过的论坛 flag 追加写入 xxff.md。

    返回:
        True  表示本次新写入
        False 表示此前已存在或写入失败
    """
    normalized = (flag or "").strip()
    if not normalized:
        return False

    log_path = _get_forum_flag_log_path(path)
    status = "submitted"
    if scored is True:
        status = "scored"
    elif verified is True:
        status = "verified_but_not_scored"
    elif verified is False:
        status = "submitted_unverified"

    note = " ".join(str(message or "").split())
    if len(note) > 240:
        note = note[:240] + "..."
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

    with _FORUM_FLAG_LOG_LOCK:
        existing = load_recorded_forum_flags(log_path)
        if normalized in existing:
            return False
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if not log_path.exists():
                log_path.write_text(
                    "# Submitted Forum Flags\n\n"
                    "自动记录已经实际提交过的论坛 flag，避免重复提交。\n\n",
                    encoding="utf-8",
                )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"- {timestamp} | challenge={int(challenge_id)} | "
                    f"status={status} | flag=`{normalized}`"
                )
                if note:
                    handle.write(f" | message={note}")
                handle.write("\n")
            return True
        except OSError as exc:
            logger.warning("[Flag] 写入论坛 Flag 记录失败: %s", exc)
            return False


def extract_flags(text: str) -> List[str]:
    """
    从文本中提取所有可能的 flag。

    Args:
        text: 命令输出或任何文本

    Returns:
        去重的 flag 列表
    """
    flags = set()
    for pattern in FLAG_PATTERNS:
        matches = pattern.findall(text)
        for m in matches:
            candidate = m.strip()
            inner = candidate[candidate.find("{") + 1:-1]
            if any(ch.isspace() for ch in inner):
                continue
            if any(ch in inner for ch in ['"', "'", "，", "。", "：", "；", "\n", "\r", "\t"]):
                continue
            if is_low_confidence_flag(candidate):
                continue
            flags.add(candidate)
    return list(flags)


def is_low_confidence_flag(flag: str) -> bool:
    normalized = (flag or "").strip()
    if not normalized:
        return True
    lower = normalized.lower()
    if not lower.startswith(("flag{", "ctf{")) or not lower.endswith("}"):
        return True
    body = lower[lower.find("{") + 1:-1]
    if not body:
        return True
    if any(token in body for token in _LOW_CONFIDENCE_SUBSTRINGS):
        return True
    if re.fullmatch(r"(.)\1{5,}", body):
        return True
    if body.count("_") >= 2 and all(part.startswith("key") for part in body.split("_") if part):
        return True
    return False


def validate_flag_format(flag: str) -> tuple[bool, str]:
    """
    验证 flag 格式是否正确。

    Returns:
        (是否有效, 错误消息)
    """
    flag = flag.strip()
    if not flag:
        return False, "FLAG 不能为空"
    if not flag.startswith("flag{"):
        return False, f"FLAG 必须以 flag{{ 开头，当前: {flag[:20]}"
    if not flag.endswith("}"):
        return False, f"FLAG 必须以 }} 结尾，当前: ...{flag[-20:]}"
    inner = flag[5:-1]
    if not inner:
        return False, "FLAG 内容不能为空"
    if len(inner) > 200:
        return False, f"FLAG 内容过长 ({len(inner)} 字符)"
    return True, ""


def suggest_flag_fix(flag: str) -> str:
    """为格式错误的 flag 提供修复建议"""
    flag = flag.strip()

    # 检查常见问题
    if "flag{" in flag and "}" in flag:
        # 提取内部的 flag{...}
        match = re.search(r"flag\{[^\}]+\}", flag)
        if match:
            return f"💡 建议: 检测到正确的 FLAG 格式: {match.group()}"

    if flag.startswith("flag") and "{" not in flag:
        return f"💡 建议: 缺少花括号，可能是 flag{{{flag[4:]}}}"

    return "💡 建议: FLAG 格式应为 flag{...}，请确认完整内容"
