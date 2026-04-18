"""
统一 embedding 客户端：SiliconFlow 在线 embedding + 本地模型 + 持久缓存。
"""
from __future__ import annotations

import shelve
import threading
from functools import lru_cache
from math import sqrt
from pathlib import Path
from typing import Any

import httpx

from ctf_kb.config import cfg
from ctf_kb.vector.common import LRUCache, stable_text_hash

_embedding_cache = LRUCache(getattr(cfg, "embedding_cache_size", 5000))
_persistent_lock = threading.RLock()


def _uses_remote_embedding_api() -> bool:
    if getattr(cfg, "offline_mode", False):
        return False
    return bool(str(cfg.embed_api_base_url or "").strip() and str(cfg.embed_api_key or "").strip())


def _normalized_embed_base_url() -> str:
    base = str(cfg.embed_api_base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _cache_key(text: str) -> str:
    return f"{cfg.embed_model}:{vector_dim()}:{stable_text_hash(text)}"


def _cache_path() -> Path:
    return Path(getattr(cfg, "embedding_cache_path", "") or "")


def _persistent_cache_get(key: str) -> list[float] | None:
    cache_path = _cache_path()
    if not cache_path:
        return None
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with _persistent_lock:
        with shelve.open(str(cache_path)) as db:
            value = db.get(key)
    return list(value) if isinstance(value, list) else None


def _persistent_cache_put(key: str, value: list[float]) -> None:
    cache_path = _cache_path()
    if not cache_path:
        return
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with _persistent_lock:
        with shelve.open(str(cache_path)) as db:
            db[key] = list(value)


@lru_cache(maxsize=1)
def vector_dim() -> int:
    configured = int(getattr(cfg, "embed_dimensions", 0) or 0)
    if configured > 0:
        return configured
    if _uses_remote_embedding_api():
        sample = _embed_via_api(["dimension probe"])
        if sample and sample[0]:
            return len(sample[0])
        raise RuntimeError("embedding api returned empty vector")
    return 384


@lru_cache(maxsize=1)
def _get_embedder():
    from sentence_transformers import SentenceTransformer

    model_name_or_path = str(getattr(cfg, "local_embed_model_path", "") or "").strip() or cfg.embed_model
    return SentenceTransformer(model_name_or_path)


def _normalize_vector(vector: list[float]) -> list[float]:
    norm = sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def _embed_via_api(texts: list[str]) -> list[list[float]]:
    base_url = _normalized_embed_base_url()
    if not base_url:
        raise RuntimeError("EMBED_API_BASE_URL is empty")

    headers = {
        "Authorization": f"Bearer {cfg.embed_api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": cfg.embed_model,
        "input": texts,
        "encoding_format": cfg.embed_encoding_format,
    }
    if getattr(cfg, "embed_dimensions", 0):
        payload["dimensions"] = int(cfg.embed_dimensions)

    with httpx.Client(timeout=float(getattr(cfg, "embed_request_timeout", 60.0))) as client:
        response = client.post(f"{base_url}/embeddings", headers=headers, json=payload)
        response.raise_for_status()
        body = response.json()

    items = body.get("data") or []
    vectors: list[list[float]] = []
    for item in items:
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise RuntimeError("embedding api returned invalid payload")
        vectors.append(_normalize_vector([float(value) for value in embedding]))
    if len(vectors) != len(texts):
        raise RuntimeError("embedding api returned mismatched vector count")
    return vectors


def _embed_via_api_batched(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    batch_size = max(1, int(getattr(cfg, "remote_embed_batch_size", 32)))
    vectors: list[list[float]] = []
    start = 0
    while start < len(texts):
        batch = texts[start : start + batch_size]
        try:
            vectors.extend(_embed_via_api(batch))
            start += len(batch)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 413 and batch_size > 1:
                batch_size = max(1, batch_size // 2)
                continue
            raise
    return vectors


def _embed_locally(texts: list[str]) -> list[list[float]]:
    model = _get_embedder()
    vectors = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return vectors.tolist() if hasattr(vectors, "tolist") else [list(vector) for vector in vectors]


def current_embedding_backend() -> str:
    if _uses_remote_embedding_api():
        return f"remote_api:{cfg.embed_model}"
    model_name_or_path = str(getattr(cfg, "local_embed_model_path", "") or "").strip() or cfg.embed_model
    return f"local:{model_name_or_path}"


def embed(texts: list[str]) -> list[list[float]]:
    vectors: list[list[float] | None] = [None] * len(texts)
    uncached_texts: list[str] = []
    uncached_indices: list[int] = []

    for index, text in enumerate(texts):
        cache_key = _cache_key(text)
        cached = _embedding_cache.get(cache_key)
        if cached is None:
            cached = _persistent_cache_get(cache_key)
            if cached is not None:
                _embedding_cache.put(cache_key, cached)
        if cached is not None:
            vectors[index] = cached
            continue
        uncached_texts.append(text)
        uncached_indices.append(index)

    if uncached_texts:
        new_vectors = _embed_via_api_batched(uncached_texts) if _uses_remote_embedding_api() else _embed_locally(uncached_texts)
        for index, text, vector in zip(uncached_indices, uncached_texts, new_vectors):
            cache_key = _cache_key(text)
            _embedding_cache.put(cache_key, vector)
            _persistent_cache_put(cache_key, vector)
            vectors[index] = vector

    return [vector or [] for vector in vectors]


def clear_embedding_cache() -> None:
    _embedding_cache.clear()
