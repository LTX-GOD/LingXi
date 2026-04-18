"""
Qdrant 向量存储：混合分桶策略，四大类独立 collection，其余落 shared。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from ctf_kb.config import cfg
from ctf_kb.models import Chunk, SearchHit
from ctf_kb.vector.common import LRUCache, MAX_CONTENT, MAX_STR, CORE_QDRANT_BUCKETS, normalize_category, resolve_qdrant_bucket, sanitize_collection_token
from ctf_kb.vector.embedding import clear_embedding_cache, current_embedding_backend, embed, vector_dim

logger = logging.getLogger(__name__)

_query_cache = LRUCache(getattr(cfg, "query_cache_size", 1000))


def _qdrant():
    from qdrant_client import QdrantClient, models

    return QdrantClient, models


@lru_cache(maxsize=1)
def _get_client():
    QdrantClient, _ = _qdrant()
    if str(getattr(cfg, "qdrant_url", "") or "").strip():
        return QdrantClient(url=cfg.qdrant_url, api_key=getattr(cfg, "qdrant_api_key", "") or None)
    return QdrantClient(path=cfg.qdrant_path)


def get_client():
    return _get_client()


def _collection_name(bucket: str) -> str:
    prefix = sanitize_collection_token(
        getattr(cfg, "qdrant_collection_prefix", "") or cfg.collection_name,
        default="ctf_writeups",
    )
    model = sanitize_collection_token(cfg.embed_model or "embed_model", default="embed_model")
    safe_bucket = sanitize_collection_token(bucket, default="shared")
    return f"{prefix}_{safe_bucket}__{model}"


def _ensure_collection(bucket: str) -> None:
    client = get_client()
    _, models = _qdrant()
    collection_name = _collection_name(bucket)
    if client.collection_exists(collection_name):
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(size=vector_dim(), distance=models.Distance.COSINE),
    )
    for field_name, schema in (
        ("category", models.PayloadSchemaType.KEYWORD),
        ("difficulty", models.PayloadSchemaType.KEYWORD),
        ("event", models.PayloadSchemaType.KEYWORD),
        ("task", models.PayloadSchemaType.KEYWORD),
        ("year", models.PayloadSchemaType.INTEGER),
        ("writeup_id", models.PayloadSchemaType.KEYWORD),
    ):
        client.create_payload_index(collection_name=collection_name, field_name=field_name, field_schema=schema)


def _point_payload(chunk: Chunk) -> dict[str, Any]:
    return {
        "writeup_id": chunk.writeup_id,
        "event": chunk.event[:MAX_STR],
        "task": chunk.task[:MAX_STR],
        "title": chunk.title[:MAX_STR],
        "url": chunk.url[:MAX_STR],
        "chunk_index": int(chunk.chunk_index),
        "content": chunk.content[:MAX_CONTENT],
        "category": normalize_category(chunk.category),
        "difficulty": chunk.difficulty[:32],
        "year": int(chunk.year or 0),
    }


def insert_chunks(chunks: list[Chunk], *, category: str | None = None) -> int:
    if not chunks:
        return 0
    client = get_client()
    _, models = _qdrant()
    inserted = 0
    grouped: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        grouped.setdefault(resolve_qdrant_bucket(category or chunk.category), []).append(chunk)

    for bucket, bucket_chunks in grouped.items():
        _ensure_collection(bucket)
        points = []
        vectors = embed([chunk.content for chunk in bucket_chunks])
        for chunk, vector in zip(bucket_chunks, vectors):
            points.append(
                models.PointStruct(
                    id=chunk.id,
                    vector=vector,
                    payload=_point_payload(chunk),
                )
            )
        client.upsert(collection_name=_collection_name(bucket), points=points, wait=True)
        inserted += len(bucket_chunks)
    return inserted


def _build_filter(
    *,
    category: str | None = None,
    difficulty: str | None = None,
    event: str | None = None,
    task: str | None = None,
    year: int | None = None,
):
    _, models = _qdrant()
    must: list[Any] = []
    if category:
        must.append(models.FieldCondition(key="category", match=models.MatchValue(value=normalize_category(category))))
    if difficulty:
        must.append(models.FieldCondition(key="difficulty", match=models.MatchValue(value=difficulty)))
    if event:
        must.append(models.FieldCondition(key="event", match=models.MatchText(text=event)))
    if task:
        must.append(models.FieldCondition(key="task", match=models.MatchText(text=task)))
    if year is not None:
        must.append(models.FieldCondition(key="year", range=models.Range(gte=int(year), lte=int(year))))
    return models.Filter(must=must) if must else None


def _points_from_result(result) -> list[Any]:
    if hasattr(result, "points"):
        return list(result.points)
    return list(result or [])


def _search_bucket(
    query: str,
    *,
    bucket: str,
    category: str | None = None,
    difficulty: str | None = None,
    event: str | None = None,
    task: str | None = None,
    year: int | None = None,
    top_k: int,
) -> list[SearchHit]:
    client = get_client()
    _ensure_collection(bucket)
    result = client.query_points(
        collection_name=_collection_name(bucket),
        query=embed([query])[0],
        query_filter=_build_filter(category=category, difficulty=difficulty, event=event, task=task, year=year),
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    hits: list[SearchHit] = []
    for point in _points_from_result(result):
        payload = dict(point.payload or {})
        hits.append(
            SearchHit(
                chunk_id=str(point.id),
                writeup_id=str(payload.get("writeup_id", "")),
                event=str(payload.get("event", "")),
                task=str(payload.get("task", "")),
                title=str(payload.get("title", "")),
                url=str(payload.get("url", "")),
                chunk_index=int(payload.get("chunk_index", 0) or 0),
                score=float(getattr(point, "score", 0.0) or 0.0),
                content=str(payload.get("content", "")),
                category=str(payload.get("category", "unknown")),
                difficulty=str(payload.get("difficulty", "unknown")),
                year=int(payload.get("year", 0) or 0),
            )
        )
    return hits


def search(
    query: str,
    top_k: int | None = None,
    *,
    category: str | None = None,
    difficulty: str | None = None,
    year: int | None = None,
) -> list[SearchHit]:
    limit = top_k or cfg.top_k
    cache_key = f"search:{hash(query)}:{category or '*'}:{difficulty or '*'}:{year or '*'}:{limit}"
    cached = _query_cache.get(cache_key)
    if cached is not None:
        return cached

    buckets = [resolve_qdrant_bucket(category)] if category else [*CORE_QDRANT_BUCKETS, "shared"]
    hits: list[SearchHit] = []
    for bucket in buckets:
        hits.extend(
            _search_bucket(
                query,
                bucket=bucket,
                category=category if bucket == "shared" or category else None,
                difficulty=difficulty,
                year=year,
                top_k=limit,
            )
        )
    hits.sort(key=lambda item: item.score, reverse=True)
    result = hits[:limit]
    _query_cache.put(cache_key, result)
    return result


def filter_search(
    query: str,
    event: str | None = None,
    task: str | None = None,
    category: str | None = None,
    difficulty: str | None = None,
    year: int | None = None,
    top_k: int | None = None,
) -> list[SearchHit]:
    limit = top_k or cfg.top_k
    cache_key = f"filter:{hash(query)}:{event or '*'}:{task or '*'}:{category or '*'}:{difficulty or '*'}:{year or '*'}:{limit}"
    cached = _query_cache.get(cache_key)
    if cached is not None:
        return cached

    buckets = [resolve_qdrant_bucket(category)] if category else [*CORE_QDRANT_BUCKETS, "shared"]
    hits: list[SearchHit] = []
    per_bucket_limit = max(limit, min(20, limit * 2))
    for bucket in buckets:
        hits.extend(
            _search_bucket(
                query,
                bucket=bucket,
                category=category if bucket == "shared" or category else None,
                difficulty=difficulty,
                event=event,
                task=task,
                year=year,
                top_k=per_bucket_limit,
            )
        )
    hits.sort(key=lambda item: item.score, reverse=True)
    result = hits[:limit]
    _query_cache.put(cache_key, result)
    return result


def count(category: str | None = None) -> int:
    client = get_client()
    if category is not None:
        bucket = resolve_qdrant_bucket(category)
        _ensure_collection(bucket)
        result = client.count(
            collection_name=_collection_name(bucket),
            count_filter=_build_filter(category=category if bucket == "shared" else None),
            exact=True,
        )
        return int(getattr(result, "count", 0) or 0)

    total = 0
    for bucket in [*CORE_QDRANT_BUCKETS, "shared"]:
        _ensure_collection(bucket)
        result = client.count(collection_name=_collection_name(bucket), exact=True)
        total += int(getattr(result, "count", 0) or 0)
    return total


def count_all() -> int:
    return count(category=None)


def clear_caches() -> None:
    _query_cache.clear()
    clear_embedding_cache()


def health() -> dict[str, Any]:
    try:
        total = count_all()
        return {
            "status": "ok",
            "backend": "qdrant",
            "chunks": total,
            "embedding_backend": current_embedding_backend(),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "backend": "qdrant",
            "error": str(exc),
            "embedding_backend": current_embedding_backend(),
        }
