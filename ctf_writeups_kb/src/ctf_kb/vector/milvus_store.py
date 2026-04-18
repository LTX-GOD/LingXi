"""
Milvus Lite 向量存储：默认后端，按 category 多 collection 存储精简索引。
"""
from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from math import sqrt
from typing import Any

from pymilvus import MilvusClient

from ctf_kb.config import cfg
from ctf_kb.models import Chunk, SearchHit
from ctf_kb.vector.common import LRUCache, MAX_CONTENT, MAX_STR, SUPPORTED_CATEGORIES, normalize_category, sanitize_collection_token
from ctf_kb.vector.embedding import clear_embedding_cache, current_embedding_backend, embed, vector_dim

logger = logging.getLogger(__name__)

_query_cache = LRUCache(getattr(cfg, "query_cache_size", 1000))
_performance_stats = {
    "search_count": 0,
    "cache_hits": 0,
    "total_search_time": 0.0,
    "avg_search_time": 0.0,
}


@lru_cache(maxsize=None)
def _collection_name(category: str | None = None) -> str:
    normalized_category = normalize_category(category)
    base = sanitize_collection_token(cfg.collection_name or "ctf_writeups", default="ctf_writeups")
    model = sanitize_collection_token(cfg.embed_model or "embed_model", default="embed_model")
    return f"{base}_{normalized_category}__{model}"


@lru_cache(maxsize=1)
def _get_client() -> MilvusClient:
    import pathlib

    pathlib.Path(cfg.milvus_db_path).parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MILVUS_LITE_IGNORE_LOCK", "1")
    return MilvusClient(cfg.milvus_db_path)


def _get_optimal_index_params(data_size: int) -> dict[str, Any]:
    dim = vector_dim()
    if data_size < 1000:
        return {"index_type": "FLAT", "metric_type": "IP", "params": {}}
    if data_size < 10000:
        nlist = min(max(int(sqrt(data_size)), 64), 1024)
        return {"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": nlist}}
    nlist = min(max(int(sqrt(data_size)), 128), 2048)
    return {
        "index_type": "IVF_PQ",
        "metric_type": "IP",
        "params": {"nlist": nlist, "m": min(max(dim // 8, 1), 64), "nbits": 8},
    }


def _get_optimal_search_params(data_size: int, index_type: str) -> dict[str, Any]:
    if index_type == "FLAT":
        return {"metric_type": "IP", "params": {}}
    if index_type in {"IVF_FLAT", "IVF_PQ"}:
        nprobe = min(max(int(sqrt(max(data_size, 1)) / 10), getattr(cfg, "base_nprobe", 16)), 128)
        return {"metric_type": "IP", "params": {"nprobe": nprobe}}
    return {"metric_type": "IP", "params": {"nprobe": getattr(cfg, "base_nprobe", 16)}}


def _ensure_collection(client: MilvusClient, *, category: str | None = None) -> None:
    collection_name = _collection_name(category)
    if client.has_collection(collection_name):
        return

    from pymilvus import DataType

    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("id", DataType.VARCHAR, max_length=96, is_primary=True)
    schema.add_field("writeup_id", DataType.VARCHAR, max_length=64)
    schema.add_field("event", DataType.VARCHAR, max_length=MAX_STR)
    schema.add_field("task", DataType.VARCHAR, max_length=MAX_STR)
    schema.add_field("title", DataType.VARCHAR, max_length=MAX_STR)
    schema.add_field("url", DataType.VARCHAR, max_length=MAX_STR)
    schema.add_field("chunk_index", DataType.INT32)
    schema.add_field("content", DataType.VARCHAR, max_length=MAX_CONTENT)
    schema.add_field("category", DataType.VARCHAR, max_length=32)
    schema.add_field("difficulty", DataType.VARCHAR, max_length=32)
    schema.add_field("year", DataType.INT32)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=vector_dim())

    params = _get_optimal_index_params(0)
    index_params = client.prepare_index_params()
    index_params.add_index(
        field_name="vector",
        index_type=params["index_type"],
        metric_type=params["metric_type"],
        params=params["params"],
    )
    client.create_collection(collection_name=collection_name, schema=schema, index_params=index_params)
    logger.info("Collection '%s' created.", collection_name)


def get_client() -> MilvusClient:
    return _get_client()


def ensure_category_collection(category: str | None = None) -> None:
    _ensure_collection(get_client(), category=category)


def _existing_ids(client: MilvusClient, collection_name: str) -> set[str]:
    try:
        rows = client.query(collection_name=collection_name, filter="", output_fields=["id"], limit=65535)
    except Exception:
        return set()
    return {str(row.get("id", "")) for row in rows if str(row.get("id", ""))}


def insert_chunks(chunks: list[Chunk], *, category: str | None = None) -> int:
    if not chunks:
        return 0

    normalized_category = normalize_category(category or chunks[0].category)
    client = get_client()
    _ensure_collection(client, category=normalized_category)
    collection_name = _collection_name(normalized_category)
    seen = _existing_ids(client, collection_name)
    new_chunks = [chunk for chunk in chunks if chunk.id not in seen]
    if not new_chunks:
        return 0

    batch_size = max(1, int(getattr(cfg, "insert_batch_size", 256)))
    for start in range(0, len(new_chunks), batch_size):
        batch = new_chunks[start : start + batch_size]
        vectors = embed([chunk.content for chunk in batch])
        for chunk, vector in zip(batch, vectors):
            chunk.vector = vector
        payload = [
            {
                "id": chunk.id,
                "writeup_id": chunk.writeup_id,
                "event": chunk.event[:MAX_STR],
                "task": chunk.task[:MAX_STR],
                "title": chunk.title[:MAX_STR],
                "url": chunk.url[:MAX_STR],
                "chunk_index": chunk.chunk_index,
                "content": chunk.content[:MAX_CONTENT],
                "category": normalize_category(chunk.category),
                "difficulty": chunk.difficulty[:32],
                "year": int(chunk.year or 0),
                "vector": chunk.vector,
            }
            for chunk in batch
        ]
        client.insert(collection_name=collection_name, data=payload)

    logger.info("Inserted %d new chunks into %s", len(new_chunks), collection_name)
    return len(new_chunks)


def _collection_stats(category: str | None = None) -> dict[str, Any]:
    client = get_client()
    _ensure_collection(client, category=category)
    try:
        return client.get_collection_stats(_collection_name(category))
    except Exception:
        return {"row_count": 0}


def _search_single_collection(
    query: str,
    *,
    category: str | None = None,
    event: str | None = None,
    task: str | None = None,
    difficulty: str | None = None,
    year: int | None = None,
    top_k: int,
) -> list[SearchHit]:
    client = get_client()
    _ensure_collection(client, category=category)
    collection_name = _collection_name(category)
    stats = _collection_stats(category)
    search_params = _get_optimal_search_params(stats.get("row_count", 0), "IVF_FLAT")

    filters: list[str] = []
    if event:
        filters.append(f'event like "%{event.replace("\"", "\\\"")}%"')
    if task:
        filters.append(f'task like "%{task.replace("\"", "\\\"")}%"')
    if difficulty:
        filters.append(f'difficulty == "{difficulty.replace("\"", "\\\"")}"')
    if category:
        filters.append(f'category == "{normalize_category(category)}"')
    if year is not None:
        filters.append(f"year == {int(year)}")
    filter_expr = " and ".join(filters) if filters else ""

    kwargs: dict[str, Any] = {
        "collection_name": collection_name,
        "data": [embed([query])[0]],
        "limit": top_k,
        "output_fields": ["id", "writeup_id", "event", "task", "title", "url", "chunk_index", "content", "category", "difficulty", "year"],
        "search_params": search_params,
    }
    if filter_expr:
        kwargs["filter"] = filter_expr

    results = client.search(**kwargs)
    hits: list[SearchHit] = []
    for row in results[0]:
        entity = row.get("entity", row)
        hits.append(
            SearchHit(
                chunk_id=str(entity.get("id", "")),
                writeup_id=str(entity.get("writeup_id", "")),
                event=str(entity.get("event", "")),
                task=str(entity.get("task", "")),
                title=str(entity.get("title", "")),
                url=str(entity.get("url", "")),
                chunk_index=int(entity.get("chunk_index", 0) or 0),
                score=float(row.get("distance", 0.0)),
                content=str(entity.get("content", "")),
                category=str(entity.get("category", "unknown") or "unknown"),
                difficulty=str(entity.get("difficulty", "unknown") or "unknown"),
                year=int(entity.get("year", 0) or 0),
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
    started = time.time()
    limit = top_k or cfg.top_k
    cache_key = f"search:{hash(query)}:{category or '*'}:{difficulty or '*'}:{year or '*'}:{limit}"
    cached = _query_cache.get(cache_key)
    if cached is not None:
        _performance_stats["cache_hits"] += 1
        _performance_stats["search_count"] += 1
        return cached

    categories = [normalize_category(category)] if category else [*SUPPORTED_CATEGORIES]
    hits: list[SearchHit] = []
    for current in categories:
        try:
            hits.extend(_search_single_collection(query, category=current, difficulty=difficulty, year=year, top_k=limit))
        except Exception:
            continue
    hits.sort(key=lambda item: item.score, reverse=True)
    result = hits[:limit]
    _query_cache.put(cache_key, result)
    elapsed = time.time() - started
    _performance_stats["search_count"] += 1
    _performance_stats["total_search_time"] += elapsed
    _performance_stats["avg_search_time"] = _performance_stats["total_search_time"] / _performance_stats["search_count"]
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
        _performance_stats["cache_hits"] += 1
        _performance_stats["search_count"] += 1
        return cached

    categories = [normalize_category(category)] if category else [*SUPPORTED_CATEGORIES]
    hits: list[SearchHit] = []
    per_collection_limit = max(limit, min(20, limit * 2))
    for current in categories:
        try:
            hits.extend(
                _search_single_collection(
                    query,
                    category=current,
                    event=event,
                    task=task,
                    difficulty=difficulty,
                    year=year,
                    top_k=per_collection_limit,
                )
            )
        except Exception:
            continue
    hits.sort(key=lambda item: item.score, reverse=True)
    result = hits[:limit]
    _query_cache.put(cache_key, result)
    _performance_stats["search_count"] += 1
    return result


def count(category: str | None = None) -> int:
    client = get_client()
    if category is not None:
        _ensure_collection(client, category=category)
        return client.get_collection_stats(_collection_name(category)).get("row_count", 0)
    total = 0
    for current in SUPPORTED_CATEGORIES:
        try:
            _ensure_collection(client, category=current)
            total += client.get_collection_stats(_collection_name(current)).get("row_count", 0)
        except Exception:
            continue
    return total


def count_all() -> int:
    return count(category=None)


def get_performance_stats() -> dict[str, Any]:
    stats = _performance_stats.copy()
    if stats["search_count"] > 0:
        stats["cache_hit_rate"] = stats["cache_hits"] / stats["search_count"]
    else:
        stats["cache_hit_rate"] = 0.0
    return stats


def clear_caches() -> None:
    _query_cache.clear()
    clear_embedding_cache()
    logger.info("Milvus and embedding caches cleared")


def health() -> dict[str, Any]:
    try:
        total = count_all()
        return {
            "status": "ok",
            "backend": "milvus",
            "chunks": total,
            "embedding_backend": current_embedding_backend(),
        }
    except Exception as exc:
        return {
            "status": "degraded",
            "backend": "milvus",
            "error": str(exc),
            "embedding_backend": current_embedding_backend(),
        }
