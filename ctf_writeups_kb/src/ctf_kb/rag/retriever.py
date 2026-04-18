"""
RAG 检索层：统一走向量后端；离线或异常时回退到本地轻量索引。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ctf_kb.config import cfg
from ctf_kb.models import SearchHit
from ctf_kb.vector.factory import get_vector_store


@dataclass(frozen=True)
class SearchFilters:
    event: str | None = None
    task: str | None = None
    category: str | None = None
    difficulty: str | None = None
    year: int | None = None
    top_k: int | None = None

    def limit(self) -> int:
        return self.top_k or cfg.top_k


@dataclass(frozen=True)
class RerankContext:
    query: str
    filters: SearchFilters
    top_k: int


@dataclass(frozen=True)
class RerankSignals:
    needles: list[str]
    event_needles: list[str]
    task_needles: list[str]


def _score_text(haystack: str, needles: list[str]) -> int:
    text = (haystack or "").lower()
    score = 0
    for needle in needles:
        if not needle:
            continue
        hits = text.count(needle)
        if hits:
            score += min(hits, 6)
    return score


@lru_cache(maxsize=1)
def _load_raw_records() -> tuple[dict, ...]:
    candidates = [Path(getattr(cfg, "index_jsonl", "")), Path(cfg.raw_jsonl)]
    for src in candidates:
        if not str(src):
            continue
        if not src.exists():
            continue
        rows: list[dict] = []
        for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
        if rows:
            return tuple(rows)
    return ()


def _query_terms(query: str) -> list[str]:
    lowered = (query or "").strip().lower()
    if not lowered:
        return []

    tokens = [token for token in re.findall(r"[a-z0-9_+#\-\u4e00-\u9fff]{2,}", lowered) if token]
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        cleaned = term.strip()
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        terms.append(cleaned)

    if len(lowered) <= 120:
        add(lowered)
    for token in tokens:
        add(token)
    for width in (2, 3):
        for idx in range(0, max(0, len(tokens) - width + 1)):
            add(" ".join(tokens[idx : idx + width]))
    return terms[:24]


def _fetch_k(top_k: int) -> int:
    return max(top_k, min(24, top_k * 3))


def _unique_hits(hits: list[SearchHit]) -> list[SearchHit]:
    unique: list[SearchHit] = []
    seen_chunk_ids: set[str] = set()
    for hit in hits:
        chunk_key = hit.chunk_id or f"{hit.writeup_id}:{hit.chunk_index}"
        if chunk_key in seen_chunk_ids:
            continue
        seen_chunk_ids.add(chunk_key)
        unique.append(hit)
    return unique


def _diversify_hits(hits: list[SearchHit], top_k: int) -> list[SearchHit]:
    primary: list[SearchHit] = []
    secondary: list[SearchHit] = []
    seen_writeups: set[str] = set()
    for hit in hits:
        writeup_key = hit.writeup_id or hit.chunk_id
        target = secondary
        if writeup_key and writeup_key not in seen_writeups:
            seen_writeups.add(writeup_key)
            target = primary
        target.append(hit)
        if len(primary) >= top_k:
            return primary[:top_k]
    return (primary + secondary)[:top_k]


def _matches_substring_filter(expected: str | None, actual: str) -> bool:
    return not expected or expected.lower() in (actual or "").lower()


def _matches_exact_filter(expected: str | None, actual: str) -> bool:
    return not expected or expected.lower() == (actual or "").lower()


def _matches_year_filter(expected: int | None, actual: int | str | None) -> bool:
    if expected is None:
        return True
    try:
        return int(actual or 0) == expected
    except (TypeError, ValueError):
        return False


def _row_matches_filters(row: dict, filters: SearchFilters) -> bool:
    row_event = str(row.get("event", "") or "")
    row_task = str(row.get("task", "") or row.get("title", "") or "")
    row_category = str(row.get("category", "unknown") or "unknown")
    row_difficulty = str(row.get("difficulty", "unknown") or "unknown")
    row_year = row.get("year", 0)
    return all(
        (
            _matches_substring_filter(filters.event, row_event),
            _matches_substring_filter(filters.task, row_task),
            _matches_exact_filter(filters.category, row_category),
            _matches_exact_filter(filters.difficulty, row_difficulty),
            _matches_year_filter(filters.year, row_year),
        )
    )


def _hit_matches_filters(hit: SearchHit, filters: SearchFilters) -> bool:
    return all(
        (
            _matches_exact_filter(filters.category, hit.category),
            _matches_exact_filter(filters.difficulty, hit.difficulty),
            _matches_year_filter(filters.year, hit.year),
        )
    )


def _hit_rank_text(hit: SearchHit) -> str:
    return " ".join(
        [
            hit.task or "",
            hit.title or "",
            hit.event or "",
            hit.category or "",
            hit.difficulty or "",
            str(hit.year or ""),
            " ".join(hit.techniques or []),
            " ".join(hit.tools or []),
        ]
    )


def _metadata_bonus(hit: SearchHit, filters: SearchFilters) -> float:
    bonus = 0.0
    if _matches_exact_filter(filters.category, hit.category) and filters.category:
        bonus += 2.5
    if _matches_exact_filter(filters.difficulty, hit.difficulty) and filters.difficulty:
        bonus += 1.5
    if _matches_year_filter(filters.year, hit.year) and filters.year is not None:
        bonus += 1.25
    if hit.year:
        bonus += 0.05
    return bonus


def _rank_hit(hit: SearchHit, *, idx: int, signals: RerankSignals, filters: SearchFilters) -> float | None:
    if not _hit_matches_filters(hit, filters):
        return None

    lexical_score = _score_text(_hit_rank_text(hit), signals.needles) * 3
    lexical_score += _score_text(hit.content[: max(getattr(cfg, "fallback_scan_chars", 2400), 800)], signals.needles)
    filter_score = _score_text(hit.event, signals.event_needles) * 2
    filter_score += _score_text(hit.task or hit.title, signals.task_needles) * 3
    return (
        float(hit.score) * 20.0
        + lexical_score
        + filter_score
        + _metadata_bonus(hit, filters)
        - (idx * 0.001)
    )


def _rerank_hits(ctx: RerankContext, hits: list[SearchHit]) -> list[SearchHit]:
    unique_hits = _unique_hits(hits)
    if not unique_hits:
        return []

    signals = RerankSignals(
        needles=_query_terms(ctx.query),
        event_needles=_query_terms(ctx.filters.event or ""),
        task_needles=_query_terms(ctx.filters.task or ""),
    )
    ranked: list[tuple[float, int, SearchHit]] = []
    for idx, hit in enumerate(unique_hits):
        score = _rank_hit(hit, idx=idx, signals=signals, filters=ctx.filters)
        if score is None:
            continue
        ranked.append((score, idx, hit))

    ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    reranked_hits = [hit for _, _, hit in ranked]
    return _diversify_hits(reranked_hits, ctx.top_k)


def _row_value(row: dict, key: str, default: str = "") -> str:
    return str(row.get(key, default) or default)


def _row_int(row: dict, key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _row_list(row: dict, key: str) -> list[str]:
    value = row.get(key, [])
    return list(value if isinstance(value, list) else [])


def _row_index_text(row: dict) -> str:
    text = str(row.get("index_content", "") or row.get("content", "") or "")
    return text[: max(getattr(cfg, "fallback_scan_chars", 2400), 600)]


def _row_to_hit(row: dict, *, score: float) -> SearchHit:
    return SearchHit(
        chunk_id=_row_value(row, "writeup_id"),
        writeup_id=_row_value(row, "writeup_id"),
        event=_row_value(row, "event"),
        task=_row_value(row, "task"),
        title=_row_value(row, "title"),
        url=_row_value(row, "url") or _row_value(row, "source_url") or _row_value(row, "ctftime_url"),
        chunk_index=0,
        score=float(score),
        content=_row_index_text(row),
        category=_row_value(row, "category", "unknown"),
        difficulty=_row_value(row, "difficulty", "unknown"),
        year=_row_int(row, "year"),
        team=_row_value(row, "team"),
        points=_row_int(row, "points"),
        solves=_row_int(row, "solves"),
        techniques=_row_list(row, "techniques"),
        tools=_row_list(row, "tools"),
    )


def _fallback_hits(query: str, filters: SearchFilters) -> list[SearchHit]:
    needles = _query_terms(query)
    scored_hits: list[tuple[int, SearchHit]] = []
    for row in _load_raw_records():
        if not _row_matches_filters(row, filters):
            continue
        row_task = str(row.get("task", "") or row.get("title", "") or "")
        row_event = str(row.get("event", "") or "")
        row_category = str(row.get("category", "unknown") or "unknown")
        row_year = str(row.get("year", "") or "")
        content = _row_index_text(row)
        score = (
            _score_text(row_task, needles) * 3
            + _score_text(row_event, needles) * 2
            + _score_text(row_category, needles) * 2
            + _score_text(row_year, needles)
            + _score_text(content, needles)
        )
        if score <= 0:
            continue
        scored_hits.append((score, _row_to_hit(row, score=score)))

    scored_hits.sort(key=lambda item: item[0], reverse=True)
    hits = [hit for _, hit in scored_hits]
    return _rerank_hits(RerankContext(query=query, filters=filters, top_k=filters.limit()), hits)


def retrieve(
    query: str,
    top_k: int | None = None,
    *,
    category: str | None = None,
    difficulty: str | None = None,
    year: int | None = None,
) -> list[SearchHit]:
    filters = SearchFilters(category=category, difficulty=difficulty, year=year, top_k=top_k)
    limit = filters.limit()
    fetch_k = _fetch_k(limit)
    store = get_vector_store()
    try:
        hits = store.search(
            query,
            top_k=fetch_k,
            category=filters.category,
            difficulty=filters.difficulty,
            year=filters.year,
        )
        return _rerank_hits(RerankContext(query=query, filters=filters, top_k=limit), hits)
    except Exception:
        return _fallback_hits(query, filters)


def retrieve_filtered(query: str, filters: SearchFilters | None = None) -> list[SearchHit]:
    active_filters = filters or SearchFilters()
    limit = active_filters.limit()
    fetch_k = _fetch_k(limit)
    store = get_vector_store()
    try:
        hits = store.filter_search(
            query,
            event=active_filters.event,
            task=active_filters.task,
            category=active_filters.category,
            difficulty=active_filters.difficulty,
            year=active_filters.year,
            top_k=fetch_k,
        )
        return _rerank_hits(RerankContext(query=query, filters=active_filters, top_k=limit), hits)
    except Exception:
        return _fallback_hits(query, active_filters)


def format_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "（知识库中未找到相关 writeup）"
    parts: list[str] = []
    for index, hit in enumerate(hits, 1):
        techniques = ",".join((hit.techniques or [])[:3]) or "-"
        tools = ",".join((hit.tools or [])[:3]) or "-"
        meta = (
            f"category={hit.category} difficulty={hit.difficulty} year={hit.year} "
            f"techniques={techniques} tools={tools}"
        )
        header = f"[{index}] {hit.task or hit.title} | {hit.event} | {meta} | {hit.url}"
        parts.append(f"{header}\n{hit.content[:900]}")
    return "\n\n---\n\n".join(parts)
