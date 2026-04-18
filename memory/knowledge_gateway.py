"""
统一知识网关
============
检索顺序：
1. 本地结构化经验层（主战场 / 论坛分桶）
2. 受控触发的各大 CTF WP 参考层

目标：
- 本地经验永远高优先级
- forum bucket 与 main bucket 严格隔离
- 外部 WP 只有在门控条件满足且结果与当前题目信号一致时才进入主提示
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from memory.knowledge_service import knowledge_service_enabled, search_knowledge_service
from memory.knowledge_store import (
    KNOWLEDGE_BUCKET_EXTERNAL,
    bucket_display_name,
    bucket_for_challenge,
    build_challenge_query,
    challenge_category,
    format_knowledge_hits,
    search_knowledge_records,
)

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_CTF_WRITEUPS_ROOT = _ROOT / "ctf_writeups_kb"
_LEGACY_WRITEUPS_ROOT = _ROOT / "tou"
_ACTIVE_WRITEUPS_ROOT = (
    _CTF_WRITEUPS_ROOT
    if _CTF_WRITEUPS_ROOT.exists()
    else _LEGACY_WRITEUPS_ROOT
)
_ACTIVE_WRITEUPS_SRC = _ACTIVE_WRITEUPS_ROOT / "src"
_ACTIVE_WRITEUPS_DB = _ACTIVE_WRITEUPS_ROOT / "data" / "milvus.db"
_ACTIVE_WRITEUPS_RAW = _ACTIVE_WRITEUPS_ROOT / "data" / "writeups_raw.jsonl"


def _env(name: str, default: str, *, legacy: str | None = None) -> str:
    for key in (name, legacy):
        if not key:
            continue
        value = os.getenv(key)
        if value is not None:
            stripped = value.strip()
            if stripped:
                return stripped
    return default


def _env_bool(name: str, default: bool, *, legacy: str | None = None) -> bool:
    return _env(name, "true" if default else "false", legacy=legacy).lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _env_int(name: str, default: int, *, legacy: str | None = None) -> int:
    try:
        return int(_env(name, str(default), legacy=legacy))
    except ValueError:
        return default


_CTF_WRITEUPS_TOP_K = max(
    1,
    _env_int("CTF_WRITEUPS_TOP_K", 3, legacy="TOU_TOP_K"),
)
_CTF_WRITEUPS_ENABLED = _env_bool(
    "CTF_WRITEUPS_ENABLED",
    True,
    legacy="TOU_ENABLED",
)
_CTF_WRITEUPS_FORCE_OFFLINE = _env_bool(
    "CTF_WRITEUPS_FORCE_OFFLINE",
    True,
    legacy="TOU_FORCE_OFFLINE",
)
_EXTERNAL_KB_MODE = str(
    os.getenv("LING_XI_EXTERNAL_KB_MODE", "gated") or "gated"
).strip().lower()
_EXTERNAL_KB_TRIGGER_FAILURES = max(
    1,
    int(os.getenv("LING_XI_EXTERNAL_KB_TRIGGER_FAILURES", "2") or 2),
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#-]{2,}|[\u4e00-\u9fff]{2,}")
_SNIPPET_LIMIT = 900
_SEARCHABLE_CONTENT_LIMIT = 6000
_STOPWORDS = {
    "http",
    "https",
    "www",
    "com",
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "then",
    "there",
    "were",
    "have",
    "has",
    "had",
    "been",
    "your",
    "about",
    "after",
    "before",
    "using",
    "used",
    "user",
    "users",
    "when",
    "what",
    "where",
    "which",
    "while",
    "would",
    "could",
    "should",
    "challenge",
    "target",
    "entrypoint",
    "unknown",
    "module",
    "title",
    "code",
}


def _clip_log_text(text: str | None, limit: int = 900) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def _model_cache_roots() -> list[Path]:
    roots: list[Path] = []
    env_pairs = (
        ("SENTENCE_TRANSFORMERS_HOME", None),
        ("TRANSFORMERS_CACHE", None),
        ("HUGGINGFACE_HUB_CACHE", None),
        ("HF_HOME", "hub"),
    )
    for key, suffix in env_pairs:
        raw = str(os.getenv(key, "") or "").strip()
        if not raw:
            continue
        base = Path(raw).expanduser()
        roots.append(base / suffix if suffix else base)

    home = Path.home()
    roots.append(home / ".cache" / "torch" / "sentence_transformers")
    roots.append(home / ".cache" / "huggingface" / "hub")

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        text = str(root)
        if text in seen:
            continue
        seen.add(text)
        deduped.append(root)
    return deduped


def _has_local_embed_model(model_name: str) -> bool:
    cleaned = str(model_name or "").strip()
    if not cleaned:
        return False

    needles = {
        cleaned.lower(),
        cleaned.replace("/", "--").lower(),
        cleaned.replace("/", "_").lower(),
    }
    if "/" not in cleaned:
        needles.add(f"models--sentence-transformers--{cleaned}".lower())
        needles.add(f"sentence-transformers_{cleaned}".lower())

    for root in _model_cache_roots():
        if not root.exists():
            continue
        try:
            for child in root.iterdir():
                name = child.name.lower()
                if any(needle in name for needle in needles):
                    return True
        except OSError:
            continue
    return False


def _looks_like_webish(challenge: dict[str, Any], recon_info: str = "") -> bool:
    if challenge.get("forum_task"):
        return False
    text = " ".join(
        str(challenge.get(key, "") or "")
        for key in ("title", "description", "category", "type", "display_code", "code")
    ).lower()
    text = f"{text}\n{(recon_info or '').lower()}"
    entrypoints = [str(item).lower() for item in (challenge.get("entrypoint") or [])]
    if any("http" in item for item in entrypoints):
        return True
    if any(
        item.endswith((":80", ":443", ":8080", ":8000", ":8888", ":5000", ":5001", ":5003"))
        for item in entrypoints
    ):
        return True
    return any(
        token in text
        for token in (
            "web",
            "http",
            "https",
            "api",
            "login",
            "cookie",
            "jwt",
            "json",
            "upload",
            "xss",
            "ssti",
            "sql",
            "php",
            "flask",
            "django",
            "node",
            "graphql",
            "session",
            "csrf",
            "redirect",
            "template",
            "admin",
        )
    )


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall((text or "").lower()):
        if token in _STOPWORDS:
            continue
        if token.isdigit():
            continue
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _interesting_recon_lines(recon_info: str, limit: int = 6) -> list[str]:
    interesting: list[str] = []
    markers = (
        "login",
        "json",
        "api",
        "jwt",
        "token",
        "cookie",
        "upload",
        "ssti",
        "template",
        "sql",
        "union",
        "deserialize",
        "pickle",
        "xss",
        "csrf",
        "redirect",
        "admin",
        "robots",
        ".git",
        "graphql",
        "set-cookie",
        "content-type",
        "openapi",
        "/docs",
    )
    for line in (recon_info or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if not stripped:
            continue
        if any(marker in lowered for marker in markers):
            interesting.append(stripped[:240])
        if len(interesting) >= limit:
            break
    return interesting


def _build_query(
    challenge: dict[str, Any],
    recon_info: str,
    action_history: list[str] | None,
) -> str:
    extra_terms = _interesting_recon_lines(recon_info)
    if action_history:
        extra_terms.extend(str(line or "")[:180] for line in action_history[-6:])
    return build_challenge_query(
        challenge,
        recon_info="",
        action_history=None,
        extra_terms=extra_terms,
    )


def _target_surfaces(*texts: str) -> set[str]:
    joined = "\n".join(str(text or "") for text in texts).lower()
    surfaces: set[str] = set()
    marker_map = {
        "login": ("login", "signin", "password", "username", "session"),
        "api": ("/api", "openapi", "/docs", "graphql", "json"),
        "upload": ("upload", "multipart", "file=", "filename"),
        "auth": ("jwt", "bearer", "token", "cookie", "set-cookie", "authorization"),
        "sqli": ("sql", "union", "select", "sqlmap"),
        "ssti": ("ssti", "template", "jinja", "twig"),
        "forum": ("帖子", "评论", "私信", "agent", "key", "前4位", "prefix"),
        "ad": ("ldap", "kerberos", "ad", "域控"),
    }
    for surface, markers in marker_map.items():
        if any(marker in joined for marker in markers):
            surfaces.add(surface)
    return surfaces


def _challenge_confidence(
    challenge: dict[str, Any],
    recon_info: str,
    action_history: list[str] | None,
) -> int:
    score = 0
    if _looks_like_webish(challenge, recon_info):
        score += 2
    if challenge_category(challenge) not in {"unknown", "forum"}:
        score += 1
    if _interesting_recon_lines(recon_info, limit=3):
        score += 1
    if action_history:
        recent_text = "\n".join(str(line or "") for line in action_history[-4:])
        if _target_surfaces(recent_text):
            score += 1
    return score


def _should_query_external(
    challenge: dict[str, Any],
    *,
    recon_info: str,
    action_history: list[str] | None,
    consecutive_failures: int,
) -> bool:
    if not _CTF_WRITEUPS_ENABLED or challenge.get("forum_task"):
        return False
    if not _looks_like_webish(challenge, recon_info):
        return False
    if _EXTERNAL_KB_MODE in {"off", "disabled", "false", "0"}:
        return False
    if _EXTERNAL_KB_MODE in {"always", "eager"}:
        return True

    difficulty = str(challenge.get("difficulty", "") or "").strip().lower()
    if difficulty in {"hard", "expert", "insane", "nightmare"}:
        return True
    if difficulty == "easy" and consecutive_failures <= 0:
        return False
    if consecutive_failures >= _EXTERNAL_KB_TRIGGER_FAILURES:
        return True
    return _challenge_confidence(challenge, recon_info, action_history) >= 4


@lru_cache(maxsize=1)
def _load_vector_backend() -> tuple[Any, str]:
    if not _CTF_WRITEUPS_ENABLED:
        return None, "disabled"
    if not _ACTIVE_WRITEUPS_SRC.exists() or not _ACTIVE_WRITEUPS_DB.exists():
        return None, "missing_ctf_writeups_assets"

    try:
        sys_path = str(_ACTIVE_WRITEUPS_SRC)
        if sys_path not in sys.path:
            sys.path.insert(0, sys_path)
        os.environ.setdefault("MILVUS_DB_PATH", str(_ACTIVE_WRITEUPS_DB))
        os.environ.setdefault("COLLECTION_NAME", "ctf_writeups")
        if _CTF_WRITEUPS_FORCE_OFFLINE:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

        from ctf_kb.config import cfg
        from ctf_kb.rag.retriever import retrieve

        embed_model = str(getattr(cfg, "embed_model", "") or "").strip()
        embed_api_base_url = str(getattr(cfg, "embed_api_base_url", "") or "").strip()
        embed_api_key = str(getattr(cfg, "embed_api_key", "") or "").strip()
        using_remote_embed_api = bool(embed_api_base_url and embed_api_key)
        if (
            _CTF_WRITEUPS_FORCE_OFFLINE
            and (not using_remote_embed_api)
            and embed_model
            and not _has_local_embed_model(embed_model)
        ):
            logger.warning(
                "[CTFWriteupsKB] 本地未发现 embedding 模型缓存(%s)，离线模式回退关键词检索",
                embed_model,
            )
            return None, "fallback:no_local_embed_model"

        return retrieve, "vector-inline"
    except Exception as exc:
        logger.warning("[CTFWriteupsKB] 向量检索不可用，自动降级到原始文本检索: %s", exc)
        return None, f"fallback:{exc.__class__.__name__}"


@lru_cache(maxsize=1)
def _load_raw_records() -> tuple[dict[str, Any], ...]:
    if not _ACTIVE_WRITEUPS_RAW.exists():
        return ()

    records: list[dict[str, Any]] = []
    with _ACTIVE_WRITEUPS_RAW.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                continue

            content = str(raw.get("content", "") or "")
            records.append(
                {
                    "task": str(raw.get("task", "") or ""),
                    "title": str(raw.get("title", "") or ""),
                    "event": str(raw.get("event", "") or ""),
                    "url": str(raw.get("url", "") or raw.get("ctftime_url", "") or ""),
                    "category": str(raw.get("category", "unknown") or "unknown"),
                    "difficulty": str(raw.get("difficulty", "unknown") or "unknown"),
                    "content": content,
                    "_title_l": f"{raw.get('task', '')} {raw.get('title', '')}".lower(),
                    "_meta_l": f"{raw.get('event', '')} {' '.join(raw.get('tags', []) or [])}".lower(),
                    "_content_l": content[:_SEARCHABLE_CONTENT_LIMIT].lower(),
                }
            )
    return tuple(records)


def _build_snippet(content: str, tokens: list[str]) -> str:
    normalized = (content or "").strip()
    if not normalized:
        return ""
    lowered = normalized.lower()
    pos = -1
    for token in tokens:
        found = lowered.find(token)
        if found != -1 and (pos == -1 or found < pos):
            pos = found
    if pos == -1:
        pos = 0
    start = max(0, pos - 180)
    snippet = normalized[start : start + _SNIPPET_LIMIT].strip()
    snippet = re.sub(r"\n{3,}", "\n\n", snippet)
    return snippet


def _fallback_search(query: str, top_k: int) -> tuple[str, list[dict[str, Any]]]:
    tokens = _tokenize(query)
    if not tokens:
        return "fallback-keyword", []

    scored: list[tuple[int, dict[str, Any]]] = []
    for record in _load_raw_records():
        score = 0
        title_l = record["_title_l"]
        meta_l = record["_meta_l"]
        content_l = record["_content_l"]
        for token in tokens:
            if token in title_l:
                score += 14
            if token in meta_l:
                score += 6
            hits = content_l.count(token)
            if hits:
                score += min(hits, 5)
        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda item: item[0], reverse=True)
    return "fallback-keyword", [record for _, record in scored[:top_k]]


def _service_results_to_context(
    results: list[dict[str, Any]],
    *,
    title: str,
    source_label: str,
) -> str:
    if not results:
        return ""
    parts = [f"## {title}"]
    for index, item in enumerate(results[:_CTF_WRITEUPS_TOP_K], 1):
        header = (
            f"[{index}] source={source_label} "
            f"challenge={item.get('challenge_code') or item.get('task') or item.get('title') or 'unknown'} "
            f"outcome={item.get('outcome_type') or 'reference'}"
        )
        parts.append(header)
        parts.append(f"- 摘要: {str(item.get('content', '') or '').strip()[:320]}")
        if item.get("category"):
            parts.append(f"- 类别: {item.get('category')}")
        if item.get("confidence") is not None:
            parts.append(f"- confidence={item.get('confidence')}")
    return "\n".join(parts).strip()


def _local_experience_context(
    challenge: dict[str, Any],
    query: str,
    *,
    top_k: int,
) -> str:
    bucket = bucket_for_challenge(challenge)
    category = challenge_category(challenge)
    zone = str(challenge.get("zone", "") or "").strip()
    challenge_code = str(
        challenge.get("display_code")
        or challenge.get("title")
        or challenge.get("code")
        or ""
    )
    scope_key = str(challenge.get("memory_scope_key", "") or "").strip()

    if knowledge_service_enabled():
        try:
            response = search_knowledge_service(
                query,
                top_k=top_k,
                bucket=bucket,
                category=category if category != "unknown" else None,
                allow_startup=False,
            )
            results = list(response.get("results", []) or [])
            context = _service_results_to_context(
                results,
                title=bucket_display_name(bucket),
                source_label="local_experience",
            )
            if context:
                return context
        except Exception as exc:
            logger.warning("[KnowledgeGateway] service 本地经验检索失败，回退文件检索: %s", exc)

    hits = search_knowledge_records(
        query,
        bucket=bucket,
        top_k=top_k,
        category=category if category != "unknown" else None,
        zone=zone,
        challenge_code=challenge_code,
        scope_key=scope_key,
    )
    return format_knowledge_hits(
        hits,
        title=bucket_display_name(bucket),
        source_label="local_experience",
        max_items=top_k,
    )


def _external_candidate_from_service_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "task": str(item.get("task", "") or item.get("title", "") or ""),
        "title": str(item.get("title", "") or item.get("task", "") or ""),
        "event": str(item.get("event", "") or ""),
        "url": str(item.get("url", "") or ""),
        "category": str(item.get("category", "unknown") or "unknown"),
        "difficulty": str(item.get("difficulty", "unknown") or "unknown"),
        "content": str(item.get("content", "") or ""),
        "source": str(item.get("source", KNOWLEDGE_BUCKET_EXTERNAL) or KNOWLEDGE_BUCKET_EXTERNAL),
    }


def _external_candidates_inline(query: str, top_k: int) -> tuple[str, list[dict[str, Any]]]:
    retrieve, backend_label = _load_vector_backend()
    if retrieve is not None:
        try:
            hits = retrieve(query, top_k=top_k)
            results = [
                {
                    "task": getattr(hit, "task", "") or getattr(hit, "title", ""),
                    "title": getattr(hit, "title", "") or getattr(hit, "task", ""),
                    "event": getattr(hit, "event", ""),
                    "url": getattr(hit, "url", ""),
                    "category": getattr(hit, "category", "unknown"),
                    "difficulty": getattr(hit, "difficulty", "unknown"),
                    "content": getattr(hit, "content", ""),
                    "source": KNOWLEDGE_BUCKET_EXTERNAL,
                }
                for hit in list(hits or [])
            ]
            if results:
                return backend_label, results
        except Exception as exc:
            logger.warning("[CTFWriteupsKB] 向量检索执行失败，回退关键词检索: %s", exc)

    fallback_label, fallback_hits = _fallback_search(query, top_k)
    return fallback_label, [
        {
            "task": hit.get("task", "") or hit.get("title", ""),
            "title": hit.get("title", "") or hit.get("task", ""),
            "event": hit.get("event", ""),
            "url": hit.get("url", ""),
            "category": hit.get("category", "unknown"),
            "difficulty": hit.get("difficulty", "unknown"),
            "content": hit.get("content", ""),
            "source": KNOWLEDGE_BUCKET_EXTERNAL,
        }
        for hit in fallback_hits
    ]


def _consistency_score(
    challenge: dict[str, Any],
    *,
    recon_info: str,
    action_history: list[str] | None,
    candidate: dict[str, Any],
) -> float:
    query_tokens = set(_tokenize(_build_query(challenge, recon_info, action_history)))
    candidate_text = " ".join(
        str(candidate.get(key, "") or "")
        for key in ("task", "title", "event", "category", "difficulty", "content")
    )
    candidate_tokens = set(_tokenize(candidate_text))
    overlap = len(query_tokens & candidate_tokens)
    surfaces_challenge = _target_surfaces(
        recon_info,
        " ".join(str(line or "") for line in list(action_history or [])[-6:]),
        str(challenge.get("category", "") or ""),
        str(challenge.get("description", "") or ""),
    )
    surfaces_candidate = _target_surfaces(candidate_text)
    category_score = 0.0
    current_category = challenge_category(challenge)
    candidate_category = str(candidate.get("category", "unknown") or "unknown").strip().lower()
    if current_category not in {"unknown", "forum"}:
        if candidate_category == current_category:
            category_score += 2.0
        elif candidate_category not in {"", "unknown"}:
            category_score -= 1.0
    surface_overlap = len(surfaces_challenge & surfaces_candidate)
    surface_penalty = 0.0
    if surfaces_challenge and not surfaces_candidate:
        surface_penalty -= 0.5
    elif surfaces_challenge and surface_overlap == 0:
        surface_penalty -= 1.0
    return float(overlap) + (surface_overlap * 1.5) + category_score + surface_penalty


def _format_external_hits(
    results: list[dict[str, Any]],
    *,
    title: str,
    source_label: str,
    query: str,
) -> str:
    if not results:
        return ""
    tokens = _tokenize(query)
    parts = [f"## {title}"]
    for index, hit in enumerate(results[:_CTF_WRITEUPS_TOP_K], 1):
        header = (
            f"[{index}] source={source_label} "
            f"{hit.get('task') or hit.get('title') or 'Untitled'} | "
            f"{hit.get('event', '')} | category={hit.get('category', 'unknown')} | "
            f"{hit.get('url', '')}"
        )
        parts.append(header)
        parts.append(_build_snippet(str(hit.get("content", "") or ""), tokens)[:_SNIPPET_LIMIT])
    return "\n".join(parts).strip()


def build_knowledge_advisor_context(
    challenge: dict[str, Any],
    *,
    recon_info: str = "",
    action_history: list[str] | None = None,
    top_k: int | None = None,
    consecutive_failures: int = 0,
) -> str:
    """
    为顾问模型构建统一知识网关上下文。
    """
    query = _build_query(challenge, recon_info, action_history)
    challenge_code = str(
        challenge.get("display_code")
        or challenge.get("title")
        or challenge.get("code")
        or "unknown"
    )
    if not query.strip():
        logger.info("[KnowledgeGateway] 跳过检索: challenge=%s reason=empty_query", challenge_code)
        return ""

    k = max(1, int(top_k or _CTF_WRITEUPS_TOP_K))
    parts: list[str] = []
    logger.info(
        "[KnowledgeGateway] 检索开始: challenge=%s top_k=%s failures=%s query=%s",
        challenge_code,
        k,
        consecutive_failures,
        _clip_log_text(query, 180),
    )

    local_context = _local_experience_context(challenge, query, top_k=k)
    if local_context:
        parts.append(local_context)
        logger.info(
            "[KnowledgeGateway] 本地经验命中: challenge=%s payload=%s",
            challenge_code,
            _clip_log_text(local_context, 900),
        )
    else:
        logger.info("[KnowledgeGateway] 本地经验未命中: challenge=%s", challenge_code)

    should_query_external = _should_query_external(
        challenge,
        recon_info=recon_info,
        action_history=action_history,
        consecutive_failures=consecutive_failures,
    )
    if not should_query_external:
        final_context = "\n\n".join(parts).strip()
        logger.info(
            "[KnowledgeGateway] 外部知识跳过: challenge=%s reason=gate_closed payload=%s",
            challenge_code,
            _clip_log_text(final_context or "—", 900),
        )
        return final_context

    external_candidates: list[dict[str, Any]] = []
    external_label = "service"
    if knowledge_service_enabled():
        try:
            response = search_knowledge_service(
                query,
                top_k=k,
                category=challenge_category(challenge) if challenge_category(challenge) not in {"unknown", "forum"} else None,
                bucket=KNOWLEDGE_BUCKET_EXTERNAL,
            )
            external_candidates = [
                _external_candidate_from_service_result(item)
                for item in list(response.get("results", []) or [])
            ]
        except Exception as exc:
            logger.warning("[KnowledgeGateway] service 外部检索失败，回退 inline: %s", exc)

    if not external_candidates:
        external_label, external_candidates = _external_candidates_inline(query, k)

    consistent_hits: list[dict[str, Any]] = []
    alternate_hits: list[dict[str, Any]] = []
    for candidate in external_candidates:
        score = _consistency_score(
            challenge,
            recon_info=recon_info,
            action_history=action_history,
            candidate=candidate,
        )
        if score >= 2.5:
            consistent_hits.append(candidate)
        elif score >= 1.0:
            alternate_hits.append(candidate)

    if consistent_hits:
        external_context = _format_external_hits(
            consistent_hits,
            title=f"各大 CTF WP 参考（{external_label}）",
            source_label="external_writeup",
            query=query,
        )
        parts.append(external_context)
        logger.info(
            "[KnowledgeGateway] 外部知识命中: challenge=%s source=%s consistent=%s alternate=%s payload=%s",
            challenge_code,
            external_label,
            len(consistent_hits),
            len(alternate_hits),
            _clip_log_text(external_context, 900),
        )
    elif alternate_hits:
        external_context = _format_external_hits(
            alternate_hits,
            title=f"各大 CTF WP 备选假设（{external_label}）",
            source_label="external_hypothesis",
            query=query,
        )
        parts.append(external_context)
        logger.info(
            "[KnowledgeGateway] 外部备选命中: challenge=%s source=%s consistent=%s alternate=%s payload=%s",
            challenge_code,
            external_label,
            len(consistent_hits),
            len(alternate_hits),
            _clip_log_text(external_context, 900),
        )
    else:
        logger.info(
            "[KnowledgeGateway] 外部知识未命中: challenge=%s source=%s candidates=%s",
            challenge_code,
            external_label,
            len(external_candidates),
        )

    final_context = "\n\n".join(part for part in parts if part).strip()
    logger.info(
        "[KnowledgeGateway] 检索完成: challenge=%s payload=%s",
        challenge_code,
        _clip_log_text(final_context or "—", 900),
    )
    return final_context
