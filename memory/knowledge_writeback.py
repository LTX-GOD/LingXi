"""
知识写回
========
从解题结果中抽取结构化经验，先写入 durable queue，再由后台 worker 入库。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from memory.knowledge_store import (
    DEFAULT_KNOWLEDGE_DIR,
    KnowledgeRecord,
    bucket_for_challenge,
    challenge_category,
    source_type_for_challenge,
    get_knowledge_store,
)
from tools.flag_utils import is_low_confidence_flag

logger = logging.getLogger(__name__)

_QUEUE_FILE = DEFAULT_KNOWLEDGE_DIR / "writeback_queue.jsonl"
_QUEUE_STATE_FILE = DEFAULT_KNOWLEDGE_DIR / "writeback_queue_state.json"
_QUEUE_LOCK = threading.RLock()
_DISCOVERY_MARKERS = (
    "openapi",
    "/docs",
    "robots",
    ".git",
    "admin",
    "upload",
    "token",
    "jwt",
    "cookie",
    "set-cookie",
    "authorization",
    "default credential",
    "弱口令",
    "私信",
    "帖子",
    "评论",
    "前4位",
    "prefix",
    "key",
    "ssti",
    "sql",
    "graphql",
    "api",
    "回显",
    "状态码",
)
_EVIDENCE_MARKERS = _DISCOVERY_MARKERS + (
    "200",
    "302",
    "401",
    "403",
    "500",
    "scored",
    "verified",
    "成功",
    "失败",
    "redirect",
)
_TRANSIENT_ERROR_MARKERS = (
    "timeout",
    "cancelled",
    "connection reset",
    "connection aborted",
    "network",
    "llm",
    "model",
    "rate limit",
    "context window",
    "empty action history",
)


def _emit_knowledge_updated(record: KnowledgeRecord) -> None:
    try:
        from web.server import push_event

        push_event(
            "knowledge_updated",
            {
                "bucket": record.bucket,
                "challenge_code": record.challenge_code,
                "record_id": record.record_id,
                "outcome_type": record.outcome_type,
                "created_at": record.created_at,
            },
        )
    except Exception:
        return


def knowledge_writeback_enabled() -> bool:
    return str(os.getenv("LING_XI_KNOWLEDGE_WRITEBACK", "1") or "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def knowledge_failure_writeback_enabled() -> bool:
    return str(os.getenv("LING_XI_KNOWLEDGE_INCLUDE_FAILURES", "1") or "1").lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _clip_text(text: str | None, limit: int = 260) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _unique_preserve(items: Iterable[str], *, limit: int = 8) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = _clip_text(item, 320).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _extract_credentials(
    challenge: dict[str, Any],
    result: dict[str, Any],
    memory_context: str,
) -> list[dict[str, str]]:
    # 默认禁止从 action_history/payloads/recon_info/memory_context 推断并持久化凭据，
    # 避免把误识别或短暂暴露的秘密扩散到 durable queue / knowledge store / memory store。
    raw_credentials = list(result.get("credentials", []) or [])
    credentials: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for item in raw_credentials:
        if not isinstance(item, dict):
            continue
        normalized = {
            "host": str(item.get("host", "") or "").strip(),
            "username": str(item.get("username", "") or "").strip(),
            "password": str(item.get("password", "") or "").strip(),
            "service": str(item.get("service", "") or "").strip(),
        }
        key = (
            normalized["host"],
            normalized["username"],
            normalized["password"],
            normalized["service"],
        )
        if not normalized["password"] or key in seen:
            continue
        seen.add(key)
        credentials.append(normalized)
        if len(credentials) >= 4:
            break
    return credentials


def _extract_semantic_insight(line: str) -> str | None:
    """从工具调用记录中提取语义化的解题思路"""
    lowered = line.lower()

    # 跳过纯技术细节的工具调用记录
    if "工具:" in line and "参数:" in line:
        # 提取关键语义信息
        if "robots.txt" in lowered:
            return "检查robots.txt发现隐藏路径或敏感信息"
        elif ".git" in lowered and ("head" in lowered or "config" in lowered):
            return "发现.git目录泄露，可能获取源码"
        elif "openapi.json" in lowered or "/docs" in lowered:
            return "发现API文档端点，获取接口信息"
        elif "sqlmap" in lowered:
            return "使用sqlmap自动化检测SQL注入漏洞"
        elif "sql" in lowered and ("injection" in lowered or "注入" in lowered or "union" in lowered):
            return "发现SQL注入点，尝试数据提取"
        elif "graphql" in lowered and "introspection" in lowered:
            return "通过GraphQL introspection发现隐藏字段"
        elif ("login" in lowered or "登录" in lowered) and ("admin" in lowered or "demo" in lowered or "user" in lowered):
            return "尝试弱口令或默认凭据登录"
        elif "jwt" in lowered or "token" in lowered:
            return "分析JWT/Token认证机制寻找绕过"
        elif "cookie" in lowered and ("session" in lowered or "auth" in lowered):
            return "分析Cookie/Session机制寻找权限提升"
        elif "ssti" in lowered or "template" in lowered:
            return "检测服务端模板注入(SSTI)漏洞"
        elif "ssrf" in lowered:
            return "检测服务端请求伪造(SSRF)漏洞"
        elif "upload" in lowered or "文件上传" in lowered:
            return "测试文件上传绕过获取webshell"
        elif "nmap" in lowered or "端口扫描" in lowered:
            return "端口扫描识别开放服务"
        elif "gobuster" in lowered or "目录爆破" in lowered or "dirbuster" in lowered:
            return "目录爆破发现隐藏路径"
        elif "hydra" in lowered or "暴力破解" in lowered:
            return "暴力破解获取有效凭据"
        # 如果包含关键发现标记但无法提取语义，返回原文
        if any(marker in lowered for marker in _DISCOVERY_MARKERS):
            return line
        return None

    # 保留非工具调用的重要信息（如熔断提示、策略描述）
    if any(marker in lowered for marker in ("熔断", "策略", "成功", "发现", "获取", "绕过", "注入", "泄露")):
        return line

    return None


def _extract_discoveries(
    challenge: dict[str, Any],
    result: dict[str, Any],
) -> list[str]:
    lines = []
    for line in list(result.get("action_history", []) or []) + [str(result.get("recon_info_excerpt", "") or "")]:
        normalized = str(line or "").strip()
        if not normalized:
            continue

        # 提取语义化的解题思路
        insight = _extract_semantic_insight(normalized)
        if insight:
            lines.append(insight)

    if challenge.get("forum_task"):
        forum_code = int(challenge.get("forum_challenge_id", 0) or 0)
        if forum_code:
            lines.insert(0, f"forum-{forum_code} 线索桶：与主战场隔离存储")

    return _unique_preserve(lines, limit=5)


def _extract_evidence(
    challenge: dict[str, Any],
    result: dict[str, Any],
) -> list[str]:
    evidence: list[str] = []
    for line in list(result.get("action_history", []) or []):
        normalized = str(line or "").strip()
        if not normalized:
            continue

        # 提取语义化的证据，而不是技术细节
        insight = _extract_semantic_insight(normalized)
        if insight:
            evidence.append(insight)

    if result.get("error"):
        error_msg = str(result.get("error"))
        # 只保留有意义的错误信息，跳过临时性错误
        if not any(marker in error_msg.lower() for marker in _TRANSIENT_ERROR_MARKERS):
            evidence.append(f"遇到错误: {_clip_text(error_msg, 120)}")

    return _unique_preserve(evidence, limit=6)


def _extract_verified_flags(result: dict[str, Any]) -> list[str]:
    verified: list[str] = []
    for flag in list(result.get("scored_flags", []) or []):
        normalized = str(flag or "").strip()
        if normalized and not is_low_confidence_flag(normalized):
            verified.append(normalized)
    fallback_flag = str(result.get("flag", "") or "").strip()
    if result.get("success") and fallback_flag and not is_low_confidence_flag(fallback_flag):
        verified.append(fallback_flag)
    return _unique_preserve(verified, limit=4)


def _extract_rejected_flags(result: dict[str, Any]) -> list[str]:
    rejected: list[str] = []
    for flag in list(result.get("rejected_flags", []) or []):
        normalized = str(flag or "").strip()
        if normalized:
            rejected.append(normalized)
    fallback_flag = str(result.get("flag", "") or "").strip()
    if (
        fallback_flag
        and not result.get("success")
        and (is_low_confidence_flag(fallback_flag) or fallback_flag not in rejected)
    ):
        rejected.append(fallback_flag)
    return _unique_preserve(rejected, limit=8)


def _is_transient_failure(result: dict[str, Any]) -> bool:
    error_text = str(result.get("error", "") or "").lower()
    if not error_text:
        return False
    return any(marker in error_text for marker in _TRANSIENT_ERROR_MARKERS)


def _has_forum_verified_clue(challenge: dict[str, Any], evidence: list[str]) -> bool:
    if not challenge.get("forum_task"):
        return False
    lowered = " ".join(item.lower() for item in evidence)
    return any(
        marker in lowered
        for marker in ("帖子", "评论", "私信", "前4位", "prefix", "agent", "key", "官方")
    )


def _looks_like_high_value_failure(
    challenge: dict[str, Any],
    result: dict[str, Any],
    *,
    discoveries: list[str],
    credentials: list[dict[str, str]],
    evidence: list[str],
    verified_flags: list[str],
) -> bool:
    if result.get("success"):
        return False
    if not knowledge_failure_writeback_enabled():
        return False
    if _is_transient_failure(result):
        return False
    if verified_flags:
        return False
    action_history = list(result.get("action_history", []) or [])
    if not action_history:
        return False
    if any(is_low_confidence_flag(str(flag or "")) for flag in [result.get("flag", "")] if str(flag or "").strip()):
        return False
    strong_signals = bool(discoveries or credentials or _has_forum_verified_clue(challenge, evidence))
    return strong_signals and bool(evidence)


def _build_summary(
    challenge: dict[str, Any],
    result: dict[str, Any],
    *,
    outcome_type: str,
    discoveries: list[str],
    evidence: list[str],
) -> str:
    display_code = str(
        challenge.get("display_code")
        or challenge.get("title")
        or challenge.get("code")
        or "unknown"
    )
    source_type = source_type_for_challenge(challenge)

    if outcome_type == "success":
        # 优先使用语义化的发现作为摘要
        head = (
            (discoveries[0] if discoveries else "")
            or (evidence[0] if evidence else "")
            or str(result.get("final_strategy", "") or "").strip()
            or "成功获取Flag"
        )
        # 移除技术细节前缀
        head = head.replace("[#", "").replace("] 工具:", "").replace("| 参数:", "")
        return _clip_text(f"{display_code} [{source_type}] 解题思路: {head}", 260)

    head = (
        discoveries[0] if discoveries else ""
    ) or (evidence[0] if evidence else "") or str(result.get("error", "") or "").strip()
    head = head.replace("[#", "").replace("] 工具:", "").replace("| 参数:", "")
    return _clip_text(f"{display_code} [{source_type}] 尝试路径: {head}", 260)


def _compute_quality_score(
    *,
    outcome_type: str,
    discoveries: list[str],
    credentials: list[dict[str, str]],
    evidence: list[str],
    verified_flags: list[str],
    rejected_flags: list[str],
) -> tuple[float, float, str]:
    quality = 0.82 if outcome_type == "success" else 0.58
    confidence = 0.90 if outcome_type == "success" else 0.74
    if discoveries:
        quality += 0.08
        confidence += 0.05
    if credentials:
        quality += 0.10
        confidence += 0.07
    if evidence:
        quality += min(0.10, 0.03 * len(evidence))
        confidence += min(0.08, 0.02 * len(evidence))
    if verified_flags:
        quality += 0.08
        confidence += 0.05
    if rejected_flags and outcome_type != "success":
        quality += 0.03
    verification_state = "verified" if outcome_type == "success" else "high_confidence"
    return min(1.0, quality), min(1.0, confidence), verification_state


def build_knowledge_candidate(
    challenge: dict[str, Any],
    result: dict[str, Any],
    *,
    zone: str = "",
    scope_key: str = "",
    memory_context: str = "",
    strategy_description: str = "",
    reflection_summary: str = "",
) -> KnowledgeRecord | None:
    if not knowledge_writeback_enabled():
        return None

    discoveries = _extract_discoveries(challenge, result)
    credentials = _extract_credentials(challenge, result, memory_context)
    evidence = _extract_evidence(challenge, result)
    verified_flags = _extract_verified_flags(result)
    rejected_flags = _extract_rejected_flags(result)

    if result.get("success"):
        outcome_type = "success"
    elif _looks_like_high_value_failure(
        challenge,
        result,
        discoveries=discoveries,
        credentials=credentials,
        evidence=evidence,
        verified_flags=verified_flags,
    ):
        outcome_type = "high_value_failure"
    else:
        return None

    # 如果有顾问反思总结，优先使用反思内容作为evidence和summary
    if reflection_summary and result.get("success"):
        # 将反思总结拆分为多条evidence
        reflection_lines = [line.strip() for line in reflection_summary.split("\n") if line.strip() and not line.strip().startswith("#")]
        evidence = _unique_preserve(reflection_lines[:6], limit=6)

        # 使用反思总结的第一句作为summary
        summary_head = reflection_lines[0] if reflection_lines else "成功获取Flag"
    else:
        # 原有逻辑：从action_history提取
        evidence = _extract_evidence(challenge, result)
        summary_head = (
            (discoveries[0] if discoveries else "")
            or (evidence[0] if evidence else "")
            or str(result.get("final_strategy", "") or "").strip()
            or "成功获取Flag"
        )

    quality_score, confidence, verification_state = _compute_quality_score(
        outcome_type=outcome_type,
        discoveries=discoveries,
        credentials=credentials,
        evidence=evidence,
        verified_flags=verified_flags,
        rejected_flags=rejected_flags,
    )
    created_at = datetime.now().isoformat()
    display_code = str(
        challenge.get("display_code")
        or challenge.get("title")
        or challenge.get("code")
        or "unknown"
    )

    # 构建summary
    source_type = source_type_for_challenge(challenge)
    if outcome_type == "success":
        summary = _clip_text(f"{display_code} [{source_type}] 解题思路: {summary_head}", 260)
    else:
        summary = _clip_text(f"{display_code} [{source_type}] 尝试路径: {summary_head}", 260)

    seed = json.dumps(
        {
            "challenge_code": display_code,
            "scope_key": scope_key or display_code,
            "created_at": created_at,
            "summary": summary,
            "outcome_type": outcome_type,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    record_id = f"{int(time.time())}-{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"
    return KnowledgeRecord(
        record_id=record_id,
        created_at=created_at,
        bucket=bucket_for_challenge(challenge),
        source_type=source_type_for_challenge(challenge),
        outcome_type=outcome_type,
        scope_key=str(scope_key or display_code),
        challenge_code=display_code,
        zone=str(zone or challenge.get("zone") or "").strip(),
        category=challenge_category(challenge),
        summary=summary,
        evidence=evidence,
        payloads=_unique_preserve((str(item or "") for item in list(result.get("payloads", []) or [])), limit=8),
        action_history_excerpt=_unique_preserve(
            (str(item or "") for item in list(result.get("action_history", []) or [])),
            limit=12,
        ),
        discoveries=discoveries,
        credentials=credentials,
        verified_flags=verified_flags,
        rejected_flags=rejected_flags,
        strategy_description=str(strategy_description or "").strip(),
        final_strategy=str(result.get("final_strategy", "") or "").strip(),
        quality_score=quality_score,
        confidence=confidence,
        verification_state=verification_state,
    )


def _read_queue_state() -> dict[str, Any]:
    try:
        return json.loads(_QUEUE_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("[Knowledge] 读取 queue state 失败: %s", exc)
        return {}


def _write_queue_state(payload: dict[str, Any]) -> None:
    _QUEUE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _QUEUE_STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def enqueue_knowledge_writeback(
    challenge: dict[str, Any],
    result: dict[str, Any],
    *,
    zone: str = "",
    scope_key: str = "",
    memory_context: str = "",
    strategy_description: str = "",
    reflection_summary: str = "",
) -> KnowledgeRecord | None:
    candidate = build_knowledge_candidate(
        challenge,
        result,
        zone=zone,
        scope_key=scope_key,
        memory_context=memory_context,
        strategy_description=strategy_description,
        reflection_summary=reflection_summary,
    )
    if candidate is None:
        return None

    envelope = {
        "record": candidate.to_dict(),
        "queued_at": datetime.now().isoformat(),
    }
    with _QUEUE_LOCK:
        _QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _QUEUE_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    logger.info(
        "[Knowledge] 已加入写回队列: challenge=%s outcome=%s bucket=%s",
        candidate.challenge_code,
        candidate.outcome_type,
        candidate.bucket,
    )
    return candidate


def process_pending_knowledge_queue(*, memory_store: Any | None = None) -> int:
    with _QUEUE_LOCK:
        state = _read_queue_state()
        last_processed_line = int(state.get("last_processed_line", 0) or 0)
        if not _QUEUE_FILE.exists():
            return 0
        processed = 0
        with _QUEUE_FILE.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                if line_no <= last_processed_line:
                    continue
                stripped = line.strip()
                if not stripped:
                    last_processed_line = line_no
                    continue
                try:
                    envelope = json.loads(stripped)
                    raw_record = envelope.get("record", {})
                    record = KnowledgeRecord.from_dict(raw_record)
                    if not record.record_id:
                        raise ValueError("missing record_id")
                except Exception as exc:
                    logger.warning("[Knowledge] 解析队列项失败，跳过: line=%s err=%s", line_no, exc)
                    last_processed_line = line_no
                    continue

                service_enabled = False
                ingest_knowledge_record = None
                try:
                    from memory.knowledge_service import (
                        ingest_knowledge_record,
                        knowledge_service_enabled,
                    )
                    service_enabled = knowledge_service_enabled()
                except Exception:
                    ingest_knowledge_record = None

                try:
                    get_knowledge_store().ingest(record, mirror_vector=False)
                except Exception as exc:
                    logger.warning(
                        "[Knowledge] 本地写入失败，保留队列等待重试: line=%s err=%s",
                        line_no,
                        exc,
                    )
                    break

                if service_enabled and ingest_knowledge_record is not None:
                    try:
                        ingest_knowledge_record(record.to_dict(), bucket=record.bucket)
                    except Exception as exc:
                        logger.warning("[Knowledge] 服务镜像失败，但本地写回已成功: %s", exc)

                active_memory_store = memory_store
                if active_memory_store is None:
                    from memory.store import get_memory_store

                    active_memory_store = get_memory_store()

                if active_memory_store is not None:
                    for discovery in record.discoveries:
                        if record.zone:
                            active_memory_store.add_discovery(
                                record.zone,
                                discovery,
                                bucket=record.bucket,
                            )
                    for cred in record.credentials:
                        active_memory_store.add_credential(
                            cred.get("host", ""),
                            cred.get("username", ""),
                            cred.get("password", ""),
                            service=cred.get("service", ""),
                            bucket=record.bucket,
                            zone=record.zone,
                        )

                _emit_knowledge_updated(record)

                processed += 1
                last_processed_line = line_no
                _write_queue_state(
                    {
                        "last_processed_line": last_processed_line,
                        "last_processed_at": datetime.now().isoformat(),
                    }
                )
        return processed


async def run_knowledge_writeback_worker(
    *,
    memory_store: Any | None = None,
    poll_interval: float = 1.0,
) -> None:
    if not knowledge_writeback_enabled():
        return
    logger.info("[Knowledge] 后台写回 worker 已启动")
    while True:
        try:
            processed = await asyncio.to_thread(
                process_pending_knowledge_queue,
                memory_store=memory_store,
            )
            await asyncio.sleep(0.2 if processed else max(0.5, poll_interval))
        except asyncio.CancelledError:
            logger.info("[Knowledge] 后台写回 worker 已停止")
            raise
        except Exception as exc:
            logger.warning("[Knowledge] 后台写回异常: %s", exc)
            await asyncio.sleep(max(1.0, poll_interval))
