"""
结构化知识层
============
统一管理本地结构化经验记录，并提供按桶隔离的检索能力。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)
_ROOT_DIR = Path(__file__).resolve().parent.parent

KNOWLEDGE_MODULE_CTF_WP = "ctf_writeups"
KNOWLEDGE_MODULE_MAIN_MEMORY = "main_battle_memory"
KNOWLEDGE_MODULE_FORUM_MEMORY = "forum_memory"

KNOWLEDGE_BUCKET_MAIN = KNOWLEDGE_MODULE_MAIN_MEMORY
KNOWLEDGE_BUCKET_FORUM = KNOWLEDGE_MODULE_FORUM_MEMORY
KNOWLEDGE_BUCKET_EXTERNAL = KNOWLEDGE_MODULE_CTF_WP
_LEGACY_BUCKET_ALIASES: dict[str, tuple[str, ...]] = {
    KNOWLEDGE_MODULE_MAIN_MEMORY: (
        "main",
        "lingxi_main_experience",
        "main_memory",
    ),
    KNOWLEDGE_MODULE_FORUM_MEMORY: (
        "forum",
        "lingxi_forum_experience",
        "forum_experience",
    ),
    KNOWLEDGE_MODULE_CTF_WP: (
        "external",
        "external_ctf_wp",
        "tou_external_writeups",
        "external_writeups",
        "ctf_wp",
    ),
}
DEFAULT_KNOWLEDGE_DIR = Path(
    os.getenv("LING_XI_KNOWLEDGE_DIR", str(_ROOT_DIR / "data" / "knowledge"))
).resolve()
DEFAULT_MIN_CONFIDENCE = 0.72
DEFAULT_TOP_K = max(1, int(os.getenv("LING_XI_KNOWLEDGE_TOP_K", "3") or 3))
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#-]{2,}|[\u4e00-\u9fff]{2,}")


def normalize_bucket(bucket: str | None) -> str:
    normalized = str(bucket or "").strip()
    for canonical, aliases in _LEGACY_BUCKET_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return KNOWLEDGE_BUCKET_MAIN


def bucket_display_name(bucket: str | None) -> str:
    normalized = normalize_bucket(bucket)
    if normalized == KNOWLEDGE_BUCKET_EXTERNAL:
        return "各大 CTF WP"
    if normalized == KNOWLEDGE_BUCKET_FORUM:
        return "论坛记忆"
    return "主战场记忆"


def bucket_for_challenge(challenge: dict[str, Any] | None) -> str:
    if bool((challenge or {}).get("forum_task", False)):
        return KNOWLEDGE_BUCKET_FORUM
    return KNOWLEDGE_BUCKET_MAIN


def source_type_for_challenge(challenge: dict[str, Any] | None) -> str:
    challenge = challenge or {}
    if bool(challenge.get("forum_task", False)):
        return "forum"
    if bool(challenge.get("manual_task", False)):
        return "manual"
    return "main_battle"


def normalize_category(value: str | None, *, forum: bool = False) -> str:
    raw = str(value or "").strip().lower()
    if forum:
        return "forum"
    if not raw:
        return "unknown"
    aliases = {
        "web安全": "web",
        "web": "web",
        "webs": "web",
        "pwn": "pwn",
        "crypto": "crypto",
        "misc": "misc",
        "reverse": "reverse",
        "rev": "reverse",
        "forensics": "forensics",
        "osint": "osint",
        "ad": "ad",
        "network": "network",
        "cve": "cve",
    }
    for key, target in aliases.items():
        if raw == key or key in raw:
            return target
    return raw[:40]


def challenge_category(challenge: dict[str, Any] | None) -> str:
    challenge = challenge or {}
    if challenge.get("forum_task", False):
        return "forum"
    return normalize_category(
        challenge.get("category")
        or challenge.get("type")
        or challenge.get("zone")
        or "",
        forum=False,
    )


def build_challenge_query(
    challenge: dict[str, Any] | None,
    *,
    recon_info: str = "",
    action_history: list[str] | None = None,
    extra_terms: Iterable[str] | None = None,
) -> str:
    """构建知识库查询字符串，严格限制长度避免HTTP 422错误"""
    challenge = challenge or {}
    parts: list[str] = []

    # 只取最关键的字段，大幅减少长度
    code = str(challenge.get("code", "") or "").strip()[:30]  # 题目代码最多30字符
    if code:
        parts.append(code)

    category = str(challenge.get("category", "") or "").strip()[:20]  # 分类最多20字符
    if category:
        parts.append(category)

    # 完全跳过描述、recon_info和action_history，它们通常包含大量技术细节
    # 这些信息对语义检索帮助不大，反而会导致URL过长

    # extra_terms只取前2个关键词，每个限制20字符
    if extra_terms:
        count = 0
        for term in extra_terms:
            if count >= 2:
                break
            term_str = str(term or "").strip()[:20]
            if term_str:
                parts.append(term_str)
                count += 1

    # 提取token并严格限制数量
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(" ".join(parts).lower()):
        if token in seen or len(token) < 2:  # 跳过单字符token
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 12:  # 从32减少到12个token
            break

    # 最终查询字符串限制在100字符以内
    query = " ".join(tokens).strip()
    if len(query) > 100:
        query = query[:100].rsplit(" ", 1)[0]  # 在单词边界截断
    return query


def _clip_text(text: str | None, limit: int = 260) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _tokenize(text: str) -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_list(raw: Any) -> list[Any]:
    return list(raw) if isinstance(raw, list) else []


@dataclass
class KnowledgeRecord:
    record_id: str
    created_at: str
    bucket: str
    source_type: str
    outcome_type: str
    scope_key: str
    challenge_code: str
    zone: str = ""
    category: str = "unknown"
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    payloads: list[str] = field(default_factory=list)
    action_history_excerpt: list[str] = field(default_factory=list)
    discoveries: list[str] = field(default_factory=list)
    credentials: list[dict[str, str]] = field(default_factory=list)
    verified_flags: list[str] = field(default_factory=list)
    rejected_flags: list[str] = field(default_factory=list)
    strategy_description: str = ""
    final_strategy: str = ""
    quality_score: float = 0.0
    confidence: float = 0.0
    verification_state: str = "unverified"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KnowledgeRecord":
        return cls(
            record_id=str(raw.get("record_id", "") or "").strip(),
            created_at=str(raw.get("created_at", "") or "").strip()
            or datetime.now().isoformat(),
            bucket=normalize_bucket(raw.get("bucket")),
            source_type=str(raw.get("source_type", "") or "").strip() or "main_battle",
            outcome_type=str(raw.get("outcome_type", "") or "").strip() or "success",
            scope_key=str(raw.get("scope_key", "") or "").strip(),
            challenge_code=str(raw.get("challenge_code", "") or "").strip(),
            zone=str(raw.get("zone", "") or "").strip(),
            category=normalize_category(raw.get("category")),
            summary=str(raw.get("summary", "") or "").strip(),
            evidence=[str(item or "").strip() for item in _safe_list(raw.get("evidence")) if str(item or "").strip()],
            payloads=[str(item or "").strip() for item in _safe_list(raw.get("payloads")) if str(item or "").strip()],
            action_history_excerpt=[
                str(item or "").strip()
                for item in _safe_list(raw.get("action_history_excerpt"))
                if str(item or "").strip()
            ],
            discoveries=[str(item or "").strip() for item in _safe_list(raw.get("discoveries")) if str(item or "").strip()],
            credentials=[
                {
                    "host": str(item.get("host", "") or "").strip(),
                    "username": str(item.get("username", "") or "").strip(),
                    "password": str(item.get("password", "") or "").strip(),
                    "service": str(item.get("service", "") or "").strip(),
                }
                for item in _safe_list(raw.get("credentials"))
                if isinstance(item, dict)
            ],
            verified_flags=[
                str(item or "").strip()
                for item in _safe_list(raw.get("verified_flags"))
                if str(item or "").strip()
            ],
            rejected_flags=[
                str(item or "").strip()
                for item in _safe_list(raw.get("rejected_flags"))
                if str(item or "").strip()
            ],
            strategy_description=str(raw.get("strategy_description", "") or "").strip(),
            final_strategy=str(raw.get("final_strategy", "") or "").strip(),
            quality_score=_safe_float(raw.get("quality_score")),
            confidence=_safe_float(raw.get("confidence")),
            verification_state=str(raw.get("verification_state", "") or "").strip() or "unverified",
        )


@dataclass(frozen=True)
class KnowledgeSearchHit:
    record: KnowledgeRecord
    score: float


class KnowledgeStore:
    def __init__(self, root: Path | str = DEFAULT_KNOWLEDGE_DIR):
        self.root = Path(root).resolve()
        self._lock = threading.RLock()

    def _bucket_path(self, bucket: str) -> Path:
        return self.root / f"{normalize_bucket(bucket)}.jsonl"

    def _bucket_candidate_paths(self, bucket: str) -> list[Path]:
        normalized = normalize_bucket(bucket)
        candidates = [self.root / f"{normalized}.jsonl"]
        for alias in _LEGACY_BUCKET_ALIASES.get(normalized, ()):
            candidates.append(self.root / f"{alias}.jsonl")
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            text = str(path)
            if text not in seen:
                seen.add(text)
                deduped.append(path)
        return deduped

    def _load_bucket_unlocked(self, bucket: str) -> list[KnowledgeRecord]:
        records: list[KnowledgeRecord] = []
        seen_ids: set[str] = set()
        for path in self._bucket_candidate_paths(bucket):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            raw = json.loads(stripped)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(raw, dict):
                            continue
                        record = KnowledgeRecord.from_dict(raw)
                        if record.record_id and record.record_id in seen_ids:
                            continue
                        if record.record_id:
                            seen_ids.add(record.record_id)
                        records.append(record)
            except OSError as exc:
                logger.warning("[Knowledge] 读取 bucket 失败: %s | %s", bucket, exc)
        return records

    def load_bucket(self, bucket: str) -> list[KnowledgeRecord]:
        with self._lock:
            return self._load_bucket_unlocked(bucket)

    def ingest(
        self,
        record: KnowledgeRecord | dict[str, Any],
        *,
        mirror_vector: bool = True,
    ) -> KnowledgeRecord:
        normalized = record if isinstance(record, KnowledgeRecord) else KnowledgeRecord.from_dict(record)
        if not normalized.record_id:
            raise ValueError("KnowledgeRecord.record_id 不能为空")

        existed = False
        with self._lock:
            path = self._bucket_path(normalized.bucket)
            self.root.mkdir(parents=True, exist_ok=True)
            existing_ids = {
                item.record_id
                for item in self._load_bucket_unlocked(normalized.bucket)
                if item.record_id
            }
            existed = normalized.record_id in existing_ids
            if not existed:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(normalized.to_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())

        # 生成向量embeddings并写入Milvus
        if mirror_vector:
            try:
                self._ingest_to_vector_store(normalized)
            except Exception as vec_exc:
                logger.warning(
                    "[Knowledge] 向量入库失败（已保留结构化存储）: bucket=%s challenge=%s error=%s",
                    normalized.bucket,
                    normalized.challenge_code,
                    vec_exc,
                )

        logger.info(
            "[Knowledge] %s bucket=%s challenge=%s outcome=%s",
            "已复用记录并尝试镜像" if existed else "已写入",
            normalized.bucket,
            normalized.challenge_code,
            normalized.outcome_type,
        )
        return normalized

    def _ingest_to_vector_store(self, record: KnowledgeRecord) -> None:
        """将知识记录生成向量并写入Milvus"""
        import httpx

        service_url = (
            os.getenv("KNOWLEDGE_SERVICE_HOST")
            or os.getenv("TOU_SERVICE_HOST")
            or "127.0.0.1"
        )
        service_port = (
            os.getenv("KNOWLEDGE_SERVICE_PORT")
            or os.getenv("TOU_SERVICE_PORT")
            or "8791"
        )
        base_url = f"http://{service_url}:{service_port}"

        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    f"{base_url}/experience/vector_ingest",
                    json={
                        "bucket": record.bucket,
                        "record": record.to_dict(),
                    },
                )
                response.raise_for_status()
                result = response.json()
                logger.info(
                    "[Knowledge] 向量入库成功 bucket=%s record_id=%s chunks=%d",
                    record.bucket,
                    record.record_id,
                    result.get("chunks_inserted", 0),
                )
        except Exception as exc:
            logger.warning(
                "[Knowledge] 向量入库失败 bucket=%s record_id=%s: %s",
                record.bucket,
                record.record_id,
                str(exc),
            )

    def replace_bucket(self, bucket: str, records: list[KnowledgeRecord]) -> None:
        normalized_bucket = normalize_bucket(bucket)
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._bucket_path(normalized_bucket)
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{normalized_bucket}_",
                suffix=".jsonl",
                dir=str(self.root),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for record in records:
                        handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_path, path)
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass


def _record_search_text(record: KnowledgeRecord) -> str:
    cred_text = " ".join(
        " ".join(
            value
            for value in (
                cred.get("host", ""),
                cred.get("username", ""),
                cred.get("password", ""),
                cred.get("service", ""),
            )
            if value
        )
        for cred in record.credentials
    )
    parts = [
        record.challenge_code,
        record.scope_key,
        record.zone,
        record.category,
        record.summary,
        record.strategy_description,
        record.final_strategy,
        " ".join(record.evidence),
        " ".join(record.payloads),
        " ".join(record.action_history_excerpt),
        " ".join(record.discoveries),
        cred_text,
        " ".join(record.verified_flags),
        " ".join(record.rejected_flags),
    ]
    return " ".join(part for part in parts if part).lower()


def search_knowledge_records(
    query: str,
    *,
    bucket: str,
    top_k: int = DEFAULT_TOP_K,
    source_type: str | None = None,
    outcome_type: str | None = None,
    category: str | None = None,
    zone: str | None = None,
    challenge_code: str | None = None,
    scope_key: str | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    include_low_confidence: bool = False,
    store: KnowledgeStore | None = None,
) -> list[KnowledgeSearchHit]:
    knowledge_store = store or get_knowledge_store()
    query_tokens = _tokenize(query)
    normalized_bucket = normalize_bucket(bucket)
    normalized_category = normalize_category(category)
    lowered_source_type = str(source_type or "").strip().lower()
    lowered_outcome_type = str(outcome_type or "").strip().lower()
    lowered_zone = str(zone or "").strip().lower()
    lowered_challenge_code = str(challenge_code or "").strip().lower()
    lowered_scope_key = str(scope_key or "").strip().lower()

    scored_hits: list[KnowledgeSearchHit] = []
    for record in knowledge_store.load_bucket(normalized_bucket):
        if lowered_source_type and record.source_type.lower() != lowered_source_type:
            continue
        if lowered_outcome_type and record.outcome_type.lower() != lowered_outcome_type:
            continue
        if category and record.category != normalized_category:
            continue
        if lowered_zone and record.zone.lower() != lowered_zone:
            continue
        if not include_low_confidence and (
            record.verification_state not in {"verified", "high_confidence"}
            and record.confidence < min_confidence
        ):
            continue

        haystack = _record_search_text(record)
        score = (record.quality_score * 0.8) + (record.confidence * 0.6)

        if lowered_challenge_code:
            if lowered_challenge_code == record.challenge_code.lower():
                score += 8.0
            elif lowered_challenge_code in haystack:
                score += 3.0
        if lowered_scope_key:
            if lowered_scope_key == record.scope_key.lower():
                score += 7.0
            elif lowered_scope_key in haystack:
                score += 2.5
        if lowered_zone and lowered_zone == record.zone.lower():
            score += 2.0
        if category and record.category == normalized_category:
            score += 1.8

        if query_tokens:
            summary_text = f"{record.summary} {record.strategy_description} {record.final_strategy}".lower()
            for token in query_tokens:
                if token in summary_text:
                    score += 2.4
                elif token in haystack:
                    score += 1.1

        if score <= 0:
            continue
        scored_hits.append(KnowledgeSearchHit(record=record, score=score))

    scored_hits.sort(
        key=lambda item: (
            item.score,
            item.record.confidence,
            item.record.quality_score,
            item.record.created_at,
        ),
        reverse=True,
    )
    return scored_hits[: max(1, int(top_k or DEFAULT_TOP_K))]


def format_knowledge_hits(
    hits: list[KnowledgeSearchHit],
    *,
    title: str = "结构化经验",
    source_label: str = "历史经验",
    max_items: int = 3,
) -> str:
    if not hits:
        return ""
    lines = [f"## {title}"]
    for index, hit in enumerate(hits[: max(1, max_items)], 1):
        record = hit.record
        lines.append(
            f"[{index}] source={source_label} bucket={record.bucket} outcome={record.outcome_type} "
            f"challenge={record.challenge_code} confidence={record.confidence:.2f}"
        )
        if record.summary:
            lines.append(f"- 摘要: {_clip_text(record.summary, 220)}")
        if record.discoveries:
            lines.append(
                "- 复用发现: "
                + "; ".join(_clip_text(item, 120) for item in record.discoveries[:3])
            )
        if record.credentials:
            cred_lines = [
                _clip_text(
                    f"{cred.get('username', '')}:{cred.get('password', '')}@{cred.get('host', '')}"
                    f" ({cred.get('service', '')})",
                    100,
                )
                for cred in record.credentials[:2]
            ]
            lines.append("- 凭据: " + "; ".join(cred_lines))
        if record.verified_flags:
            lines.append("- 已验证 Flag: " + "; ".join(record.verified_flags[:2]))
        if record.final_strategy or record.strategy_description:
            strategy = record.final_strategy or record.strategy_description
            lines.append(f"- 策略: {_clip_text(strategy, 220)}")
        if record.evidence:
            lines.append(
                "- 证据: "
                + "; ".join(_clip_text(item, 120) for item in record.evidence[:2])
            )
    return "\n".join(lines).strip()


def search_local_knowledge_context(
    challenge: dict[str, Any],
    *,
    zone: str = "",
    scope_key: str = "",
    top_k: int = 2,
) -> str:
    bucket = bucket_for_challenge(challenge)
    query = build_challenge_query(challenge)
    hits = search_knowledge_records(
        query,
        bucket=bucket,
        top_k=top_k,
        category=challenge_category(challenge) if challenge_category(challenge) != "unknown" else None,
        zone=zone,
        challenge_code=str(
            challenge.get("display_code")
            or challenge.get("title")
            or challenge.get("code")
            or ""
        ),
        scope_key=scope_key,
    )
    return format_knowledge_hits(
        hits,
        title=f"{bucket_display_name(bucket)}（可复用）",
        source_label="local_experience",
        max_items=top_k,
    )


_store: Optional[KnowledgeStore] = None


def get_knowledge_store() -> KnowledgeStore:
    global _store
    if _store is None:
        _store = KnowledgeStore()
    return _store
