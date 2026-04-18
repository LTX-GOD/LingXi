"""
向量后端工厂：默认 Milvus，可切换到 Qdrant。
"""
from __future__ import annotations

from typing import Protocol

from ctf_kb.config import cfg
from ctf_kb.vector.common import CORE_QDRANT_BUCKETS, resolve_qdrant_bucket


class VectorStore(Protocol):
    def insert_chunks(self, chunks, *, category: str | None = None) -> int: ...
    def search(self, query: str, top_k: int | None = None, *, category: str | None = None, difficulty: str | None = None, year: int | None = None): ...
    def filter_search(self, query: str, event: str | None = None, task: str | None = None, category: str | None = None, difficulty: str | None = None, year: int | None = None, top_k: int | None = None): ...
    def count(self, category: str | None = None) -> int: ...
    def count_all(self) -> int: ...
    def current_embedding_backend(self) -> str: ...
    def health(self): ...

def get_vector_store() -> VectorStore:
    backend = (getattr(cfg, "vector_backend", "milvus") or "milvus").strip().lower()
    if backend == "qdrant":
        from ctf_kb.vector import qdrant_store

        return qdrant_store

    from ctf_kb.vector import milvus_store

    return milvus_store
