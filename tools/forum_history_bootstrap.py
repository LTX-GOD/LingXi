"""
零界历史私信启动扫描器
=====================
启动 LJ 赛场前，扫描所有历史私信，提取 KeyA/KeyB/KeyC，
尝试组合 challenge-2 的 flag，并把结果写入持久化记忆。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.flag_utils import has_recorded_forum_flag, record_forum_flag_attempt
from tools.forum_api import ForumAPIError, ForumRateLimitError, get_forum_client

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_WP_DIR = _ROOT / "wp"
_BOOTSTRAP_JSON = _WP_DIR / "forum_history_bootstrap.json"
_BOOTSTRAP_MD = _WP_DIR / "forum_history_bootstrap.md"
_CONVERSATION_PAGE_SIZE = max(20, int(os.getenv("FORUM_HISTORY_CONVERSATION_PAGE_SIZE", "100") or 100))
_MESSAGE_PAGE_SIZE = max(20, int(os.getenv("FORUM_HISTORY_MESSAGE_PAGE_SIZE", "100") or 100))
_MAX_KEY_VALUES_PER_TYPE = max(1, int(os.getenv("FORUM_HISTORY_KEY_LIMIT_PER_TYPE", "6") or 6))
_MAX_FLAG_COMBINATIONS = max(1, int(os.getenv("FORUM_HISTORY_FLAG_COMBO_LIMIT", "6") or 6))
_CONTEXT_LIMIT = 1600

_KEY_FULL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\bkey\s*([abc])\s*[:=：]\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{3,63})"),
    re.compile(r"(?i)\bkey([abc])\s*[:=：]\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{3,63})"),
)
_KEY_PREFIX_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\bkey\s*([abc])\b[^\n]{0,30}?(?:前4位|前四位|前缀)\s*(?:是|为|[:=：])\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{1,15})"
    ),
    re.compile(
        r"(?i)\bkey([abc])\b[^\n]{0,30}?(?:前4位|前四位|前缀)\s*(?:是|为|[:=：])\s*[`\"']?([A-Za-z0-9][A-Za-z0-9_-]{1,15})"
    ),
)
_NOISE_TOKENS = {
    "example",
    "sample",
    "placeholder",
    "xxxx",
    "yyyy",
    "zzzz",
    "keya",
    "keyb",
    "keyc",
    "key_a",
    "key_b",
    "key_c",
}


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "list", "records", "rows", "messages", "conversations", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if payload:
            return [payload]
    return []


def _get_int(obj: Any, *keys: str) -> int | None:
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _get_text(obj: Any, *keys: str) -> str:
    if not isinstance(obj, dict):
        return ""
    for key in keys:
        value = obj.get(key)
        if value is not None:
            text = str(value).strip()
            if text:
                return text
    return ""


def _normalize_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:32]


def _normalize_key_value(value: str) -> str:
    normalized = str(value or "").strip().strip("`'\".,:;[](){}")
    if len(normalized) < 4 or len(normalized) > 64:
        return ""
    lowered = normalized.lower()
    if lowered in _NOISE_TOKENS:
        return ""
    if lowered.startswith("flag{") or lowered.startswith("ctf{"):
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{3,63}", normalized):
        return ""
    return normalized


def _extract_key_mentions(text: str) -> list[dict[str, str]]:
    content = str(text or "")
    if not content.strip():
        return []
    found: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for pattern in _KEY_FULL_PATTERNS:
        for match in pattern.finditer(content):
            key_type = str(match.group(1) or "").upper()
            value = _normalize_key_value(match.group(2) or "")
            if key_type not in {"A", "B", "C"} or not value:
                continue
            sig = (key_type, value, "full")
            if sig in seen:
                continue
            seen.add(sig)
            found.append({"type": key_type, "value": value, "kind": "full"})

    for pattern in _KEY_PREFIX_PATTERNS:
        for match in pattern.finditer(content):
            key_type = str(match.group(1) or "").upper()
            value = _normalize_key_value(match.group(2) or "")
            if key_type not in {"A", "B", "C"} or not value:
                continue
            sig = (key_type, value, "prefix")
            if sig in seen:
                continue
            seen.add(sig)
            found.append({"type": key_type, "value": value, "kind": "prefix"})

    return found


def _fetch_all_conversations(client: Any) -> list[dict[str, Any]]:
    conversations: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _extract_items(client.get_conversations(page=page, size=_CONVERSATION_PAGE_SIZE))
        if not batch:
            break
        conversations.extend(batch)
        if len(batch) < _CONVERSATION_PAGE_SIZE:
            break
        page += 1
    return conversations


def _fetch_all_messages(client: Any, conv_id: int) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _extract_items(
            client.get_conversation_messages(conv_id=conv_id, page=page, size=_MESSAGE_PAGE_SIZE)
        )
        if not batch:
            break
        messages.extend(batch)
        if len(batch) < _MESSAGE_PAGE_SIZE:
            break
        page += 1
    return messages


def _record_key_value(
    bucket: dict[str, dict[str, Any]],
    *,
    value: str,
    conv_id: int | None,
    message_id: int | None,
    timestamp: str,
    direction: str,
    excerpt: str,
) -> None:
    current = bucket.get(value)
    if current is None:
        bucket[value] = {
            "value": value,
            "count": 1,
            "last_seen": timestamp,
            "conv_id": conv_id,
            "message_id": message_id,
            "direction": direction,
            "excerpt": excerpt,
        }
        return
    current["count"] = int(current.get("count", 0) or 0) + 1
    if timestamp and timestamp >= str(current.get("last_seen", "") or ""):
        current["last_seen"] = timestamp
        current["conv_id"] = conv_id
        current["message_id"] = message_id
        current["direction"] = direction
        current["excerpt"] = excerpt


def _sorted_key_values(bucket: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    values = list(bucket.values())
    values.sort(
        key=lambda item: (
            -int(item.get("count", 0) or 0),
            str(item.get("last_seen", "") or ""),
            str(item.get("value", "") or ""),
        ),
        reverse=False,
    )
    values.sort(
        key=lambda item: (
            -int(item.get("count", 0) or 0),
            str(item.get("last_seen", "") or ""),
        ),
        reverse=True,
    )
    return values


def _build_flag_candidates(full_keys: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    key_a = _sorted_key_values(full_keys.get("A", {}))[:_MAX_KEY_VALUES_PER_TYPE]
    key_b = _sorted_key_values(full_keys.get("B", {}))[:_MAX_KEY_VALUES_PER_TYPE]
    key_c = _sorted_key_values(full_keys.get("C", {}))[:_MAX_KEY_VALUES_PER_TYPE]
    candidates: list[dict[str, Any]] = []
    seen_flags: set[str] = set()

    for ia, a in enumerate(key_a):
        for ib, b in enumerate(key_b):
            for ic, c in enumerate(key_c):
                raw = f"{a['value']}{b['value']}{c['value']}"
                digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
                flag = f"flag{{{digest}}}"
                if flag in seen_flags:
                    continue
                seen_flags.add(flag)
                candidates.append(
                    {
                        "flag": flag,
                        "key_a": a["value"],
                        "key_b": b["value"],
                        "key_c": c["value"],
                        "rank": ia + ib + ic,
                    }
                )
    candidates.sort(key=lambda item: (int(item.get("rank", 99) or 99), item["flag"]))
    return candidates[:_MAX_FLAG_COMBINATIONS]


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Forum History Bootstrap",
        "",
        f"- 更新时间: {summary.get('updated_at', '')}",
        f"- 会话数: {summary.get('conversation_count', 0)}",
        f"- 消息数: {summary.get('message_count', 0)}",
        f"- 我方 Agent ID: {summary.get('my_agent_id', '')}",
    ]

    for key_type in ("A", "B", "C"):
        full_values = summary.get("full_keys", {}).get(key_type, [])
        prefix_values = summary.get("prefix_keys", {}).get(key_type, [])
        lines.extend(["", f"## Key {key_type}", ""])
        if full_values:
            lines.append("### Full Values")
            lines.extend(
                [
                    f"- `{item.get('value', '')}` | count={item.get('count', 0)} | last_seen={item.get('last_seen', '')}"
                    for item in full_values
                ]
            )
        else:
            lines.append("- Full Values: none")
        if prefix_values:
            lines.append("")
            lines.append("### Prefix Values")
            lines.extend(
                [
                    f"- `{item.get('value', '')}` | count={item.get('count', 0)} | last_seen={item.get('last_seen', '')}"
                    for item in prefix_values
                ]
            )

    attempted = summary.get("attempted_flags", [])
    lines.extend(["", "## Attempted Flags", ""])
    if attempted:
        for item in attempted:
            lines.append(
                f"- `{item.get('flag', '')}` | scored={item.get('scored')} | "
                f"verified={item.get('verified')} | message={item.get('message', '')}"
            )
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".forum_hist_", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".forum_hist_", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def run_forum_history_bootstrap(submit_flags: bool = True) -> dict[str, Any]:
    """
    启动时扫描所有历史私信，提取 Key 记忆并尝试拼 challenge-2 的 flag。
    """
    client = get_forum_client()
    my_info = client.get_my_agent_info() or {}
    my_agent_id = _get_int(my_info, "id", "agent_id", "user_id")

    conversations = _fetch_all_conversations(client)
    full_keys: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in ("A", "B", "C")}
    prefix_keys: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in ("A", "B", "C")}
    message_count = 0

    for conversation in conversations:
        conv_id = _get_int(conversation, "id", "conv_id", "conversation_id")
        if conv_id is None:
            continue
        messages = _fetch_all_messages(client, conv_id)
        for message in messages:
            message_count += 1
            sender_id = _get_int(message, "sender_id", "senderId", "from_id", "fromId")
            receiver_id = _get_int(message, "receiver_id", "receiverId", "to_id", "toId")
            content = _get_text(message, "content", "text", "message")
            if not content:
                continue
            direction = "unknown"
            if my_agent_id is not None:
                if sender_id == my_agent_id:
                    direction = "outbound"
                elif receiver_id == my_agent_id:
                    direction = "inbound"
            timestamp = _normalize_timestamp(
                _get_text(message, "created_at", "createdAt", "timestamp", "sent_at", "sentAt")
            )
            excerpt = re.sub(r"\s+", " ", content).strip()[:180]
            mentions = _extract_key_mentions(content)
            for mention in mentions:
                record = prefix_keys if mention["kind"] == "prefix" else full_keys
                _record_key_value(
                    record[mention["type"]],
                    value=mention["value"],
                    conv_id=conv_id,
                    message_id=_get_int(message, "id", "message_id", "messageId"),
                    timestamp=timestamp,
                    direction=direction,
                    excerpt=excerpt,
                )

    attempted_flags: list[dict[str, Any]] = []
    if submit_flags:
        for candidate in _build_flag_candidates(full_keys):
            flag = candidate["flag"]
            if has_recorded_forum_flag(flag):
                continue
            try:
                result = client.submit_ctf_flag(2, flag)
            except ForumRateLimitError as exc:
                logger.warning("[ForumHistory] 启动扫描提交赛题二 Flag 限流，停止本轮尝试: %s", exc)
                break
            except ForumAPIError as exc:
                logger.warning("[ForumHistory] 启动扫描提交赛题二 Flag 失败: %s | %s", flag, exc)
                continue

            record_forum_flag_attempt(
                flag,
                2,
                scored=bool(result.get("scored")),
                verified=result.get("verified"),
                message=str(result.get("message", "") or result.get("verification_error", "") or ""),
            )
            attempted_flags.append(
                {
                    **candidate,
                    "scored": bool(result.get("scored")),
                    "verified": result.get("verified"),
                    "message": str(result.get("message", "") or result.get("verification_error", "") or ""),
                }
            )
            if result.get("scored"):
                logger.info(
                    "[ForumHistory] ✅ 启动扫描命中赛题二 Flag: %s | A=%s B=%s C=%s",
                    flag,
                    candidate["key_a"],
                    candidate["key_b"],
                    candidate["key_c"],
                )

    summary = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "my_agent_id": my_agent_id,
        "conversation_count": len(conversations),
        "message_count": message_count,
        "full_keys": {
            key: _sorted_key_values(values)[:_MAX_KEY_VALUES_PER_TYPE]
            for key, values in full_keys.items()
        },
        "prefix_keys": {
            key: _sorted_key_values(values)[:_MAX_KEY_VALUES_PER_TYPE]
            for key, values in prefix_keys.items()
        },
        "attempted_flags": attempted_flags,
    }
    _atomic_write_json(_BOOTSTRAP_JSON, summary)
    _atomic_write_text(_BOOTSTRAP_MD, _render_markdown(summary))
    logger.info(
        "[ForumHistory] 启动扫描完成: conversations=%s messages=%s keys(A/B/C)=%s/%s/%s attempts=%s",
        summary["conversation_count"],
        summary["message_count"],
        len(summary["full_keys"].get("A", [])),
        len(summary["full_keys"].get("B", [])),
        len(summary["full_keys"].get("C", [])),
        len(attempted_flags),
    )
    return summary


def get_forum_history_bootstrap_context(limit: int = _CONTEXT_LIMIT) -> str:
    """
    读取启动扫描的持久化摘要，作为 LJ 题目的附加记忆。
    """
    try:
        payload = json.loads(_BOOTSTRAP_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError) as exc:
        logger.warning("[ForumHistory] 读取持久化摘要失败: %s", exc)
        return ""

    lines = [
        "📨 历史私信持久化记忆:",
        f"- 更新时间: {payload.get('updated_at', '')}",
        f"- 会话数: {payload.get('conversation_count', 0)} | 消息数: {payload.get('message_count', 0)}",
    ]
    for key_type in ("A", "B", "C"):
        values = payload.get("full_keys", {}).get(key_type, []) or []
        if values:
            lines.append(
                f"- Key{key_type}: " + ", ".join(
                    f"{item.get('value', '')}(count={item.get('count', 0)})"
                    for item in values[:3]
                )
            )
    attempted_flags = payload.get("attempted_flags", []) or []
    if attempted_flags:
        lines.append(
            "- 启动阶段已尝试赛题二候选 Flag: " + ", ".join(
                f"{item.get('flag', '')}[scored={item.get('scored')}]"
                for item in attempted_flags[:4]
            )
        )
    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ..."


if __name__ == "__main__":
    run_forum_history_bootstrap(submit_flags=True)
