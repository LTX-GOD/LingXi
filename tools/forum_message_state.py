"""
零界论坛私信状态机
==================
以后台增量轮询方式维护私信状态，避免论坛任务是否读取私信完全依赖 LLM 当轮决策。

目标：
1. 启动后即建立会话基线，后续对方新发私信时能被状态机捕获。
2. 用持久化状态标记 pending/idle/tracked，会话和消息状态可迭代更新。
3. 把最新待处理私信摘要暴露给论坛题提示词，减少“有新消息但没去查”的漏检。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.flag_utils import has_recorded_forum_flag, record_forum_flag_attempt
from tools.forum_api import ForumAPIError, ForumRateLimitError, get_forum_client
from tools.forum_history_bootstrap import (
    _BOOTSTRAP_JSON,
    _MAX_KEY_VALUES_PER_TYPE,
    _build_flag_candidates,
    _extract_items,
    _extract_key_mentions,
    _fetch_all_conversations,
    _fetch_all_messages,
    _get_int,
    _get_text,
    _normalize_timestamp,
    _record_key_value,
    _sorted_key_values,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_WP_DIR = _ROOT / "wp"
_STATE_JSON = _WP_DIR / "forum_message_state.json"
_STATE_MD = _WP_DIR / "forum_message_state.md"
_RECENT_EVENT_LIMIT = max(8, int(os.getenv("FORUM_MESSAGE_STATE_EVENT_LIMIT", "24") or 24))
_PENDING_REF_LIMIT = max(4, int(os.getenv("FORUM_MESSAGE_STATE_PENDING_REF_LIMIT", "12") or 12))
_CONTEXT_LIMIT = max(400, int(os.getenv("FORUM_MESSAGE_STATE_CONTEXT_LIMIT", "1400") or 1400))
_FLAG_ATTEMPTS_PER_SYNC = max(1, int(os.getenv("FORUM_MESSAGE_STATE_FLAG_ATTEMPTS_PER_SYNC", "2") or 2))
_FULL_CONVERSATION_REFRESH_SECONDS = max(
    30.0,
    float(os.getenv("FORUM_MESSAGE_STATE_FULL_REFRESH_SECONDS", "90") or 90),
)
_SYNC_INTERVAL_SECONDS = max(
    8.0,
    float(os.getenv("FORUM_MESSAGE_STATE_SYNC_INTERVAL_SECONDS", "18") or 18),
)


def _read_state() -> dict[str, Any]:
    try:
        return json.loads(_STATE_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("[ForumState] 读取状态失败: %s", exc)
        return {}


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".forum_state_", suffix=".json", dir=str(path.parent))
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
    fd, tmp_path = tempfile.mkstemp(prefix=".forum_state_", suffix=".md", dir=str(path.parent))
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


def _message_ref(conv_id: int, message: dict[str, Any]) -> str:
    message_id = _get_int(message, "id", "message_id", "messageId")
    if message_id is not None:
        return str(message_id)
    basis = "|".join(
        [
            str(conv_id),
            _get_text(message, "created_at", "createdAt", "timestamp", "sent_at", "sentAt"),
            str(_get_int(message, "sender_id", "senderId", "from_id", "fromId") or ""),
            _get_text(message, "content", "text", "message")[:160],
        ]
    )
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    return f"sig:{digest}"


def _message_sort_key(message: dict[str, Any]) -> tuple[str, int]:
    timestamp = _normalize_timestamp(
        _get_text(message, "created_at", "createdAt", "timestamp", "sent_at", "sentAt")
    )
    message_id = _get_int(message, "id", "message_id", "messageId") or 0
    return (timestamp, message_id)


def _hydrate_key_buckets(payload: Any) -> dict[str, dict[str, dict[str, Any]]]:
    buckets: dict[str, dict[str, dict[str, Any]]] = {key: {} for key in ("A", "B", "C")}
    if not isinstance(payload, dict):
        return buckets
    for key_type in ("A", "B", "C"):
        values = payload.get(key_type, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "") or "").strip()
            if not value:
                continue
            buckets[key_type][value] = {
                "value": value,
                "count": int(item.get("count", 0) or 0),
                "last_seen": str(item.get("last_seen", "") or ""),
                "conv_id": item.get("conv_id"),
                "message_id": item.get("message_id"),
                "direction": str(item.get("direction", "") or ""),
                "excerpt": str(item.get("excerpt", "") or ""),
            }
    return buckets


def _serialize_key_buckets(buckets: dict[str, dict[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    return {
        key_type: _sorted_key_values(values)[:_MAX_KEY_VALUES_PER_TYPE]
        for key_type, values in buckets.items()
    }


def _seed_key_buckets_from_bootstrap(
    full_keys: dict[str, dict[str, dict[str, Any]]],
    prefix_keys: dict[str, dict[str, dict[str, Any]]],
) -> None:
    if any(full_keys[key] or prefix_keys[key] for key in ("A", "B", "C")):
        return
    try:
        payload = json.loads(_BOOTSTRAP_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, ValueError) as exc:
        logger.warning("[ForumState] 读取历史私信摘要失败，无法做 key 种子初始化: %s", exc)
        return

    for key_type in ("A", "B", "C"):
        for item in payload.get("full_keys", {}).get(key_type, []) or []:
            value = str(item.get("value", "") or "").strip()
            if not value:
                continue
            full_keys[key_type][value] = {
                "value": value,
                "count": int(item.get("count", 0) or 0),
                "last_seen": str(item.get("last_seen", "") or ""),
                "conv_id": item.get("conv_id"),
                "message_id": item.get("message_id"),
                "direction": str(item.get("direction", "") or ""),
                "excerpt": str(item.get("excerpt", "") or ""),
            }
        for item in payload.get("prefix_keys", {}).get(key_type, []) or []:
            value = str(item.get("value", "") or "").strip()
            if not value:
                continue
            prefix_keys[key_type][value] = {
                "value": value,
                "count": int(item.get("count", 0) or 0),
                "last_seen": str(item.get("last_seen", "") or ""),
                "conv_id": item.get("conv_id"),
                "message_id": item.get("message_id"),
                "direction": str(item.get("direction", "") or ""),
                "excerpt": str(item.get("excerpt", "") or ""),
            }


def _coerce_conversation_state(payload: Any, conv_id: int) -> dict[str, Any]:
    if isinstance(payload, dict):
        state = dict(payload)
    else:
        state = {}
    state["conversation_id"] = conv_id
    state.setdefault("peer_agent_id", None)
    state.setdefault("last_message_id", 0)
    state.setdefault("last_message_ref", "")
    state.setdefault("last_message_at", "")
    state.setdefault("last_excerpt", "")
    state.setdefault("last_inbound_message_id", 0)
    state.setdefault("last_inbound_message_ref", "")
    state.setdefault("last_outbound_message_id", 0)
    state.setdefault("pending_inbound_refs", [])
    state.setdefault("unread_hint", False)
    state.setdefault("status", "idle")
    state.setdefault("last_refreshed_at", "")
    return state


def _extract_unread_state(unread_items: list[dict[str, Any]]) -> tuple[set[int], list[dict[str, Any]]]:
    unread_conv_ids: set[int] = set()
    unread_events: list[dict[str, Any]] = []
    for item in unread_items:
        conv_id = _get_int(item, "conversation_id", "conversationId", "conv_id", "convId")
        if conv_id is not None:
            unread_conv_ids.add(conv_id)
        unread_events.append(
            {
                "conversation_id": conv_id,
                "message_ref": str(_get_int(item, "id", "message_id", "messageId") or ""),
                "sender_id": _get_int(item, "sender_id", "senderId", "from_id", "fromId"),
                "created_at": _normalize_timestamp(
                    _get_text(item, "created_at", "createdAt", "timestamp", "sent_at", "sentAt")
                ),
                "excerpt": _get_text(item, "content", "text", "message")[:180],
            }
        )
    return unread_conv_ids, unread_events


def _render_state_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Forum Message State",
        "",
        f"- 更新时间: {payload.get('updated_at', '')}",
        f"- 未读数: {payload.get('unread_count', 0)}",
        f"- 待处理会话数: {payload.get('pending_conversation_count', 0)}",
        f"- 待处理消息数: {payload.get('pending_message_count', 0)}",
    ]

    pending_events = [
        item for item in (payload.get("recent_events", []) or [])
        if str(item.get("status", "") or "") == "pending_review"
    ]
    lines.extend(["", "## Pending Inbound", ""])
    if not pending_events:
        lines.append("- none")
    else:
        for item in pending_events[-8:]:
            key_summary = ""
            mentions = item.get("key_mentions", []) or []
            if mentions:
                key_summary = " | " + ", ".join(
                    f"Key{m.get('type', '')}:{m.get('value', '')}({m.get('kind', '')})"
                    for m in mentions[:3]
                )
            lines.append(
                f"- conv={item.get('conversation_id', '')} sender={item.get('sender_id', '')} "
                f"message={item.get('message_ref', '')} at={item.get('created_at', '')} "
                f"| {item.get('excerpt', '')}{key_summary}"
            )

    for key_type in ("A", "B", "C"):
        values = payload.get("full_keys", {}).get(key_type, []) or []
        if not values:
            continue
        lines.extend(["", f"## Key {key_type}", ""])
        for item in values[:4]:
            lines.append(
                f"- `{item.get('value', '')}` | count={item.get('count', 0)} | last_seen={item.get('last_seen', '')}"
            )

    return "\n".join(lines).strip() + "\n"


def _trim_recent_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(events) <= _RECENT_EVENT_LIMIT:
        return events
    return events[-_RECENT_EVENT_LIMIT:]


def _maybe_submit_forum2_flags(
    client: Any,
    *,
    full_keys: dict[str, dict[str, dict[str, Any]]],
    attempted_flags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    submitted = 0
    for candidate in _build_flag_candidates(full_keys):
        if submitted >= _FLAG_ATTEMPTS_PER_SYNC:
            break
        flag = str(candidate.get("flag", "") or "").strip()
        if not flag or has_recorded_forum_flag(flag):
            continue
        try:
            result = client.submit_ctf_flag(2, flag)
        except ForumRateLimitError as exc:
            logger.warning("[ForumState] 后台私信状态机提交赛题二 Flag 限流: %s", exc)
            break
        except ForumAPIError as exc:
            logger.warning("[ForumState] 后台私信状态机提交赛题二 Flag 失败: %s | %s", flag, exc)
            continue

        submitted += 1
        attempt = {
            **candidate,
            "scored": bool(result.get("scored")),
            "verified": result.get("verified"),
            "message": str(result.get("message", "") or result.get("verification_error", "") or ""),
        }
        attempted_flags.append(attempt)
        record_forum_flag_attempt(
            flag,
            2,
            scored=bool(result.get("scored")),
            verified=result.get("verified"),
            message=attempt["message"],
        )
        if attempt["scored"]:
            logger.info(
                "[ForumState] ✅ 后台私信状态机命中赛题二 Flag: %s | A=%s B=%s C=%s",
                flag,
                candidate.get("key_a", ""),
                candidate.get("key_b", ""),
                candidate.get("key_c", ""),
            )
    return attempted_flags[-12:]


def sync_forum_message_state(
    *,
    submit_flags: bool = True,
) -> dict[str, Any]:
    """
    增量同步论坛私信状态。

    行为：
    - 每轮都查未读，保证启动后新私信能进入状态机。
    - 建立会话基线，检测 last_message_id 变化。
    - 对变化会话补拉消息，把新入站消息标记为 pending_review。
    """
    client = get_forum_client()
    state = _read_state()
    conversations_state = {
        str(key): value
        for key, value in (state.get("conversations", {}) or {}).items()
        if isinstance(value, dict)
    }
    recent_events = [
        dict(item)
        for item in (state.get("recent_events", []) or [])
        if isinstance(item, dict)
    ]
    full_keys = _hydrate_key_buckets(state.get("full_keys"))
    prefix_keys = _hydrate_key_buckets(state.get("prefix_keys"))
    _seed_key_buckets_from_bootstrap(full_keys, prefix_keys)
    attempted_flags = [
        dict(item)
        for item in (state.get("attempted_flags", []) or [])
        if isinstance(item, dict)
    ][-12:]

    my_agent_id = _get_int(state, "my_agent_id")
    if my_agent_id is None:
        my_info = client.get_my_agent_info() or {}
        my_agent_id = _get_int(my_info, "id", "agent_id", "user_id")
    unread_items = _extract_items(client.get_unread_messages())
    unread_conv_ids, unread_events = _extract_unread_state(unread_items)
    last_conversation_sync_at_ts = float(state.get("last_conversation_sync_at_ts", 0.0) or 0.0)
    should_refresh_conversations = (
        not conversations_state
        or bool(unread_items)
        or (time.time() - last_conversation_sync_at_ts) >= _FULL_CONVERSATION_REFRESH_SECONDS
    )
    conversations = _fetch_all_conversations(client) if should_refresh_conversations else []

    changed_conv_ids: set[int] = set(unread_conv_ids)
    for conv in conversations:
        conv_id = _get_int(conv, "id", "conv_id", "conversation_id")
        if conv_id is None:
            continue
        conv_key = str(conv_id)
        conv_state = _coerce_conversation_state(conversations_state.get(conv_key), conv_id)
        prev_last_message_id = int(conv_state.get("last_message_id", 0) or 0)
        current_last_message_id = _get_int(
            conv,
            "last_message_id",
            "lastMessageId",
            "latest_message_id",
            "latestMessageId",
            "message_id",
            "messageId",
        )
        current_last_message_at = _normalize_timestamp(
            _get_text(conv, "updated_at", "updatedAt", "last_message_at", "lastMessageAt", "timestamp")
        )
        current_excerpt = _get_text(conv, "last_message", "lastMessage", "preview", "content", "message")[:180]
        conv_state["unread_hint"] = conv_id in unread_conv_ids
        if current_last_message_id is not None:
            conv_state["last_message_id"] = max(prev_last_message_id, int(current_last_message_id))
            if current_last_message_id > prev_last_message_id:
                changed_conv_ids.add(conv_id)
        if current_last_message_at:
            conv_state["last_message_at"] = current_last_message_at
        if current_excerpt:
            conv_state["last_excerpt"] = current_excerpt
        if conv_id in unread_conv_ids:
            changed_conv_ids.add(conv_id)
        conversations_state[conv_key] = conv_state

    new_inbound_count = 0
    refreshed_conversation_count = 0
    for conv_id in sorted(changed_conv_ids):
        conv_key = str(conv_id)
        conv_state = _coerce_conversation_state(conversations_state.get(conv_key), conv_id)
        prev_last_inbound_id = int(conv_state.get("last_inbound_message_id", 0) or 0)
        prev_last_inbound_ref = str(conv_state.get("last_inbound_message_ref", "") or "")
        pending_refs = {
            str(value)
            for value in (conv_state.get("pending_inbound_refs", []) or [])
            if str(value).strip()
        }
        messages = _fetch_all_messages(client, conv_id)
        if not messages:
            conversations_state[conv_key] = conv_state
            continue

        refreshed_conversation_count += 1
        peer_agent_id = _get_int(conv_state, "peer_agent_id")
        last_message_id = int(conv_state.get("last_message_id", 0) or 0)
        last_message_ref = str(conv_state.get("last_message_ref", "") or "")
        last_message_at = str(conv_state.get("last_message_at", "") or "")
        last_excerpt = str(conv_state.get("last_excerpt", "") or "")
        last_inbound_message_id = prev_last_inbound_id
        last_inbound_message_ref = prev_last_inbound_ref
        last_outbound_message_id = int(conv_state.get("last_outbound_message_id", 0) or 0)

        for message in sorted(messages, key=_message_sort_key):
            message_id = _get_int(message, "id", "message_id", "messageId")
            sender_id = _get_int(message, "sender_id", "senderId", "from_id", "fromId")
            receiver_id = _get_int(message, "receiver_id", "receiverId", "to_id", "toId")
            content = _get_text(message, "content", "text", "message")
            timestamp = _normalize_timestamp(
                _get_text(message, "created_at", "createdAt", "timestamp", "sent_at", "sentAt")
            )
            excerpt = " ".join(content.split())[:180]
            message_ref = _message_ref(conv_id, message)

            direction = "unknown"
            if my_agent_id is not None:
                if sender_id == my_agent_id:
                    direction = "outbound"
                elif receiver_id == my_agent_id:
                    direction = "inbound"

            if direction == "inbound" and peer_agent_id is None and sender_id is not None and sender_id != my_agent_id:
                peer_agent_id = sender_id
            if direction == "outbound" and peer_agent_id is None and receiver_id is not None and receiver_id != my_agent_id:
                peer_agent_id = receiver_id

            if message_id is not None and message_id >= last_message_id:
                last_message_id = int(message_id)
                last_message_ref = message_ref
                if timestamp:
                    last_message_at = timestamp
                if excerpt:
                    last_excerpt = excerpt
            elif timestamp and timestamp >= last_message_at:
                last_message_ref = message_ref
                last_message_at = timestamp
                if excerpt:
                    last_excerpt = excerpt

            if direction == "inbound":
                is_new_inbound = False
                if message_id is not None:
                    if int(message_id) > prev_last_inbound_id and message_ref not in pending_refs:
                        is_new_inbound = True
                    last_inbound_message_id = max(last_inbound_message_id, int(message_id))
                elif message_ref and message_ref != prev_last_inbound_ref and message_ref not in pending_refs:
                    is_new_inbound = True
                last_inbound_message_ref = message_ref
                if is_new_inbound:
                    pending_refs.add(message_ref)
                    recent_events.append(
                        {
                            "conversation_id": conv_id,
                            "peer_agent_id": peer_agent_id,
                            "message_ref": message_ref,
                            "sender_id": sender_id,
                            "receiver_id": receiver_id,
                            "created_at": timestamp,
                            "excerpt": excerpt,
                            "key_mentions": _extract_key_mentions(content),
                            "status": "pending_review",
                        }
                    )
                    new_inbound_count += 1
            elif direction == "outbound" and message_id is not None:
                last_outbound_message_id = max(last_outbound_message_id, int(message_id))

            for mention in _extract_key_mentions(content):
                target = prefix_keys if mention["kind"] == "prefix" else full_keys
                _record_key_value(
                    target[mention["type"]],
                    value=mention["value"],
                    conv_id=conv_id,
                    message_id=message_id,
                    timestamp=timestamp,
                    direction=direction,
                    excerpt=excerpt,
                )

        conv_state.update(
            {
                "peer_agent_id": peer_agent_id,
                "last_message_id": last_message_id,
                "last_message_ref": last_message_ref,
                "last_message_at": last_message_at,
                "last_excerpt": last_excerpt,
                "last_inbound_message_id": last_inbound_message_id,
                "last_inbound_message_ref": last_inbound_message_ref,
                "last_outbound_message_id": last_outbound_message_id,
                "pending_inbound_refs": sorted(pending_refs)[-_PENDING_REF_LIMIT:],
                "unread_hint": conv_id in unread_conv_ids,
                "status": "pending_review" if pending_refs else ("tracked" if last_message_ref else "idle"),
                "last_refreshed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        conversations_state[conv_key] = conv_state

    if submit_flags:
        attempted_flags = _maybe_submit_forum2_flags(
            client,
            full_keys=full_keys,
            attempted_flags=attempted_flags,
        )

    recent_events = _trim_recent_events(recent_events)
    pending_conversation_ids = [
        int(conv_id)
        for conv_id, item in conversations_state.items()
        if (item.get("pending_inbound_refs") or [])
    ]
    pending_message_count = sum(
        len(item.get("pending_inbound_refs", []) or [])
        for item in conversations_state.values()
    )
    payload = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "my_agent_id": my_agent_id,
        "unread_count": len(unread_items),
        "known_conversation_count": len(conversations_state),
        "pending_conversation_count": len(pending_conversation_ids),
        "pending_message_count": pending_message_count,
        "unread_conversation_ids": sorted(unread_conv_ids),
        "pending_conversation_ids": pending_conversation_ids[:24],
        "recent_unread": unread_events[-8:],
        "recent_events": recent_events,
        "conversations": conversations_state,
        "last_conversation_sync_at_ts": time.time() if should_refresh_conversations else last_conversation_sync_at_ts,
        "full_keys": _serialize_key_buckets(full_keys),
        "prefix_keys": _serialize_key_buckets(prefix_keys),
        "attempted_flags": attempted_flags,
        "stats": {
            "conversation_refresh": bool(should_refresh_conversations),
            "refreshed_conversation_count": refreshed_conversation_count,
            "new_inbound_count": new_inbound_count,
        },
    }
    _atomic_write_json(_STATE_JSON, payload)
    _atomic_write_text(_STATE_MD, _render_state_markdown(payload))
    logger.info(
        "[ForumState] 私信状态同步完成: unread=%s pending_conv=%s pending_msg=%s refreshed=%s new_inbound=%s",
        payload["unread_count"],
        payload["pending_conversation_count"],
        payload["pending_message_count"],
        refreshed_conversation_count,
        new_inbound_count,
    )
    return payload


def get_forum_message_state_context(limit: int = _CONTEXT_LIMIT) -> str:
    payload = _read_state()
    if not payload:
        return ""

    lines = [
        "📬 后台私信状态机:",
        f"- 更新时间: {payload.get('updated_at', '')}",
        f"- 未读数: {payload.get('unread_count', 0)} | 待处理会话: {payload.get('pending_conversation_count', 0)} | 待处理消息: {payload.get('pending_message_count', 0)}",
    ]

    pending_events = [
        item for item in (payload.get("recent_events", []) or [])
        if str(item.get("status", "") or "") == "pending_review"
    ]
    if pending_events:
        lines.append("- 状态机已捕获新的入站私信；下一步优先 `forum_get_unread_messages` -> `forum_get_conversations` -> `forum_get_conversation_messages` 回源。")
        for item in pending_events[-5:]:
            key_summary = ""
            mentions = item.get("key_mentions", []) or []
            if mentions:
                key_summary = " | " + ", ".join(
                    f"Key{m.get('type', '')}:{m.get('value', '')}({m.get('kind', '')})"
                    for m in mentions[:3]
                )
            lines.append(
                f"- conv={item.get('conversation_id', '')} sender={item.get('sender_id', '')} at={item.get('created_at', '')} "
                f"| {item.get('excerpt', '')}{key_summary}"
            )

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
            "- 状态机已尝试赛题二候选 Flag: " + ", ".join(
                f"{item.get('flag', '')}[scored={item.get('scored')}]"
                for item in attempted_flags[:4]
            )
        )

    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + " ..."


def mark_forum_message_state_reviewed(*, review_all: bool = False) -> dict[str, Any]:
    payload = _read_state()
    if not payload:
        return {}

    conversations = payload.get("conversations", {}) or {}
    recent_events = payload.get("recent_events", []) or []
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    changed = False

    for item in conversations.values():
        if not isinstance(item, dict):
            continue
        pending_refs = item.get("pending_inbound_refs", []) or []
        if not pending_refs and not review_all:
            continue
        if pending_refs:
            item["pending_inbound_refs"] = []
            changed = True
        item["unread_hint"] = False
        item["status"] = "tracked" if item.get("last_message_ref") or item.get("last_message_at") else "idle"
        item["last_reviewed_at"] = now_iso

    for event in recent_events:
        if not isinstance(event, dict):
            continue
        if str(event.get("status", "") or "") != "pending_review":
            continue
        event["status"] = "tracked"
        event["reviewed_at"] = now_iso
        changed = True

    if not changed:
        return payload

    pending_conversation_ids = [
        int(conv_id)
        for conv_id, item in conversations.items()
        if isinstance(item, dict) and (item.get("pending_inbound_refs") or [])
    ]
    pending_message_count = sum(
        len(item.get("pending_inbound_refs", []) or [])
        for item in conversations.values()
        if isinstance(item, dict)
    )
    payload["updated_at"] = now_iso
    payload["pending_conversation_count"] = len(pending_conversation_ids)
    payload["pending_message_count"] = pending_message_count
    payload["pending_conversation_ids"] = pending_conversation_ids[:24]
    _atomic_write_json(_STATE_JSON, payload)
    _atomic_write_text(_STATE_MD, _render_state_markdown(payload))
    return payload


async def run_forum_message_state_worker(
    *,
    submit_flags: bool = False,
    poll_interval: float = _SYNC_INTERVAL_SECONDS,
) -> None:
    logger.info(
        "[ForumState] 后台私信状态机启动: submit_flags=%s interval=%.1fs",
        submit_flags,
        poll_interval,
    )
    while True:
        try:
            await asyncio.to_thread(
                sync_forum_message_state,
                submit_flags=submit_flags,
            )
            await asyncio.sleep(max(8.0, poll_interval))
        except asyncio.CancelledError:
            logger.info("[ForumState] 后台私信状态机已停止")
            raise
        except ForumRateLimitError as exc:
            logger.warning("[ForumState] 后台同步触发限流，延迟重试: %s", exc)
            await asyncio.sleep(max(20.0, poll_interval * 2))
        except Exception as exc:
            logger.warning("[ForumState] 后台同步失败: %s", exc)
            await asyncio.sleep(max(12.0, poll_interval))
