"""
统一配置：在线/离线 embedding、向量后端、知识库瘦身与性能参数。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False


_THIS_FILE = Path(__file__).resolve()
_CTF_WRITEUPS_ROOT = _THIS_FILE.parents[3]
_PROJECT_ROOT = _THIS_FILE.parents[5]
_DATA_ROOT = _THIS_FILE.parents[2] / "data"

load_dotenv(_PROJECT_ROOT / ".env", override=False)
load_dotenv(_CTF_WRITEUPS_ROOT / ".env", override=False)

def _env(name: str, default: str, *, legacy: str | None = None) -> str:
    for key in (name, legacy):
        if not key:
            continue
        value = os.environ.get(key)
        if value is not None:
            stripped = value.strip()
            if stripped:
                return stripped
    return default


def _env_int(name: str, default: int, *, legacy: str | None = None) -> int:
    try:
        return int(_env(name, str(default), legacy=legacy))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, legacy: str | None = None) -> float:
    try:
        return float(_env(name, str(default), legacy=legacy))
    except ValueError:
        return default


def _env_bool(name: str, default: bool, *, legacy: str | None = None) -> bool:
    return _env(name, "true" if default else "false", legacy=legacy).lower() == "true"


@dataclass(frozen=True)
class OptimizedConfig:
    milvus_db_path: str = field(
        default_factory=lambda: _env("MILVUS_DB_PATH", str(_DATA_ROOT / "milvus.db"))
    )
    qdrant_path: str = field(
        default_factory=lambda: _env("QDRANT_PATH", str(_DATA_ROOT / "qdrant"))
    )
    qdrant_url: str = field(default_factory=lambda: _env("QDRANT_URL", ""))
    qdrant_api_key: str = field(default_factory=lambda: _env("QDRANT_API_KEY", ""))
    qdrant_collection_prefix: str = field(
        default_factory=lambda: _env("QDRANT_COLLECTION_PREFIX", "ctf_writeups")
    )
    vector_backend: str = field(default_factory=lambda: _env("VECTOR_BACKEND", "milvus").lower())

    raw_jsonl: str = field(
        default_factory=lambda: _env("RAW_JSONL", str(_DATA_ROOT / "writeups_raw.jsonl"))
    )
    index_jsonl: str = field(
        default_factory=lambda: _env("INDEX_JSONL", str(_DATA_ROOT / "writeups_index.jsonl"))
    )
    collection_name: str = field(
        default_factory=lambda: _env("COLLECTION_NAME", "ctf_writeups")
    )
    top_k: int = field(default_factory=lambda: _env_int("TOP_K", 5))
    crawl_max_pages: int = field(default_factory=lambda: _env_int("CRAWL_MAX_PAGES", 10))

    embed_model: str = field(
        default_factory=lambda: _env("EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
    )
    embed_api_base_url: str = field(
        default_factory=lambda: _env("EMBED_API_BASE_URL", "https://api.siliconflow.cn")
    )
    embed_api_key: str = field(default_factory=lambda: _env("EMBED_API_KEY", ""))
    embed_encoding_format: str = field(
        default_factory=lambda: _env("EMBED_ENCODING_FORMAT", "float")
    )
    embed_dimensions: int = field(default_factory=lambda: _env_int("EMBED_DIMENSIONS", 4096))
    embed_request_timeout: float = field(
        default_factory=lambda: _env_float("EMBED_REQUEST_TIMEOUT", 60.0)
    )
    local_embed_model_path: str = field(
        default_factory=lambda: _env("LOCAL_EMBED_MODEL_PATH", "")
    )
    embedding_cache_path: str = field(
        default_factory=lambda: _env(
            "EMBEDDING_CACHE_PATH",
            str(_DATA_ROOT / "embedding_cache"),
        )
    )

    offline_mode: bool = field(
        default_factory=lambda: _env_bool(
            "CTF_WRITEUPS_OFFLINE_MODE",
            False,
            legacy="TOU_OFFLINE_MODE",
        )
    )
    offline_answer_mode: str = field(
        default_factory=lambda: _env("OFFLINE_ANSWER_MODE", "auto").lower()
    )
    local_llm_base_url: str = field(default_factory=lambda: _env("LOCAL_LLM_BASE_URL", ""))
    local_llm_model: str = field(default_factory=lambda: _env("LOCAL_LLM_MODEL", ""))
    local_llm_api_key: str = field(default_factory=lambda: _env("LOCAL_LLM_API_KEY", "offline"))
    llm_role: str = field(
        default_factory=lambda: _env(
            "CTF_WRITEUPS_LLM_ROLE",
            "advisor",
            legacy="TOU_LLM_ROLE",
        )
    )

    api_host: str = field(default_factory=lambda: _env("API_HOST", "127.0.0.1"))
    api_port: int = field(default_factory=lambda: _env_int("API_PORT", 8000))

    chunk_size: int = field(default_factory=lambda: _env_int("CHUNK_SIZE", 1200))
    chunk_overlap: int = field(default_factory=lambda: _env_int("CHUNK_OVERLAP", 96))
    chunk_max_chars: int = field(default_factory=lambda: _env_int("MAX_CHUNK_CHARS", 1800))
    max_chunks_per_writeup: int = field(
        default_factory=lambda: _env_int("MAX_CHUNKS_PER_WRITEUP", 12)
    )
    min_index_content_chars: int = field(
        default_factory=lambda: _env_int("MIN_INDEX_CONTENT_CHARS", 120)
    )
    index_content_chars: int = field(
        default_factory=lambda: _env_int("INDEX_CONTENT_CHARS", 6000)
    )
    fallback_scan_chars: int = field(
        default_factory=lambda: _env_int("FALLBACK_SCAN_CHARS", 2400)
    )
    ingest_flush_size: int = field(
        default_factory=lambda: _env_int("INGEST_FLUSH_SIZE", 256)
    )

    enable_optimizations: bool = field(
        default_factory=lambda: _env_bool("ENABLE_OPTIMIZATIONS", True)
    )
    insert_batch_size: int = field(
        default_factory=lambda: _env_int("INSERT_BATCH_SIZE", 256)
    )
    remote_embed_batch_size: int = field(
        default_factory=lambda: _env_int("REMOTE_EMBED_BATCH_SIZE", 32)
    )
    query_cache_size: int = field(
        default_factory=lambda: _env_int("QUERY_CACHE_SIZE", 1000)
    )
    embedding_cache_size: int = field(
        default_factory=lambda: _env_int("EMBEDDING_CACHE_SIZE", 5000)
    )
    enable_query_cache: bool = field(
        default_factory=lambda: _env_bool("ENABLE_QUERY_CACHE", True)
    )
    enable_embedding_cache: bool = field(
        default_factory=lambda: _env_bool("ENABLE_EMBEDDING_CACHE", True)
    )
    auto_index_optimization: bool = field(
        default_factory=lambda: _env_bool("AUTO_INDEX_OPTIMIZATION", True)
    )
    preferred_index_type: str = field(
        default_factory=lambda: _env("PREFERRED_INDEX_TYPE", "auto")
    )
    adaptive_search_params: bool = field(
        default_factory=lambda: _env_bool("ADAPTIVE_SEARCH_PARAMS", True)
    )
    base_nprobe: int = field(default_factory=lambda: _env_int("BASE_NPROBE", 16))
    base_ef: int = field(default_factory=lambda: _env_int("BASE_EF", 64))
    enable_performance_stats: bool = field(
        default_factory=lambda: _env_bool("ENABLE_PERFORMANCE_STATS", True)
    )
    log_slow_queries: bool = field(
        default_factory=lambda: _env_bool("LOG_SLOW_QUERIES", True)
    )
    slow_query_threshold: float = field(
        default_factory=lambda: _env_float("SLOW_QUERY_THRESHOLD", 1.0)
    )


optimized_cfg = OptimizedConfig()


@dataclass(frozen=True)
class Config:
    milvus_db_path: str = field(default_factory=lambda: optimized_cfg.milvus_db_path)
    qdrant_path: str = field(default_factory=lambda: optimized_cfg.qdrant_path)
    qdrant_url: str = field(default_factory=lambda: optimized_cfg.qdrant_url)
    qdrant_api_key: str = field(default_factory=lambda: optimized_cfg.qdrant_api_key)
    qdrant_collection_prefix: str = field(
        default_factory=lambda: optimized_cfg.qdrant_collection_prefix
    )
    vector_backend: str = field(default_factory=lambda: optimized_cfg.vector_backend)
    raw_jsonl: str = field(default_factory=lambda: optimized_cfg.raw_jsonl)
    index_jsonl: str = field(default_factory=lambda: optimized_cfg.index_jsonl)
    collection_name: str = field(default_factory=lambda: optimized_cfg.collection_name)
    top_k: int = field(default_factory=lambda: optimized_cfg.top_k)
    crawl_max_pages: int = field(default_factory=lambda: optimized_cfg.crawl_max_pages)
    embed_model: str = field(default_factory=lambda: optimized_cfg.embed_model)
    embed_api_base_url: str = field(default_factory=lambda: optimized_cfg.embed_api_base_url)
    embed_api_key: str = field(default_factory=lambda: optimized_cfg.embed_api_key)
    embed_encoding_format: str = field(
        default_factory=lambda: optimized_cfg.embed_encoding_format
    )
    embed_dimensions: int = field(default_factory=lambda: optimized_cfg.embed_dimensions)
    embed_request_timeout: float = field(
        default_factory=lambda: optimized_cfg.embed_request_timeout
    )
    local_embed_model_path: str = field(
        default_factory=lambda: optimized_cfg.local_embed_model_path
    )
    embedding_cache_path: str = field(
        default_factory=lambda: optimized_cfg.embedding_cache_path
    )
    offline_mode: bool = field(default_factory=lambda: optimized_cfg.offline_mode)
    offline_answer_mode: str = field(
        default_factory=lambda: optimized_cfg.offline_answer_mode
    )
    local_llm_base_url: str = field(
        default_factory=lambda: optimized_cfg.local_llm_base_url
    )
    local_llm_model: str = field(default_factory=lambda: optimized_cfg.local_llm_model)
    local_llm_api_key: str = field(default_factory=lambda: optimized_cfg.local_llm_api_key)
    llm_role: str = field(default_factory=lambda: optimized_cfg.llm_role)
    api_host: str = field(default_factory=lambda: optimized_cfg.api_host)
    api_port: int = field(default_factory=lambda: optimized_cfg.api_port)
    chunk_size: int = field(default_factory=lambda: optimized_cfg.chunk_size)
    chunk_overlap: int = field(default_factory=lambda: optimized_cfg.chunk_overlap)
    chunk_max_chars: int = field(default_factory=lambda: optimized_cfg.chunk_max_chars)
    max_chunks_per_writeup: int = field(
        default_factory=lambda: optimized_cfg.max_chunks_per_writeup
    )
    min_index_content_chars: int = field(
        default_factory=lambda: optimized_cfg.min_index_content_chars
    )
    index_content_chars: int = field(
        default_factory=lambda: optimized_cfg.index_content_chars
    )
    fallback_scan_chars: int = field(
        default_factory=lambda: optimized_cfg.fallback_scan_chars
    )
    ingest_flush_size: int = field(default_factory=lambda: optimized_cfg.ingest_flush_size)
    enable_optimizations: bool = field(
        default_factory=lambda: optimized_cfg.enable_optimizations
    )
    insert_batch_size: int = field(default_factory=lambda: optimized_cfg.insert_batch_size)
    remote_embed_batch_size: int = field(
        default_factory=lambda: optimized_cfg.remote_embed_batch_size
    )
    query_cache_size: int = field(default_factory=lambda: optimized_cfg.query_cache_size)
    embedding_cache_size: int = field(
        default_factory=lambda: optimized_cfg.embedding_cache_size
    )
    enable_query_cache: bool = field(
        default_factory=lambda: optimized_cfg.enable_query_cache
    )
    enable_embedding_cache: bool = field(
        default_factory=lambda: optimized_cfg.enable_embedding_cache
    )
    auto_index_optimization: bool = field(
        default_factory=lambda: optimized_cfg.auto_index_optimization
    )
    preferred_index_type: str = field(
        default_factory=lambda: optimized_cfg.preferred_index_type
    )
    adaptive_search_params: bool = field(
        default_factory=lambda: optimized_cfg.adaptive_search_params
    )
    base_nprobe: int = field(default_factory=lambda: optimized_cfg.base_nprobe)
    base_ef: int = field(default_factory=lambda: optimized_cfg.base_ef)
    enable_performance_stats: bool = field(
        default_factory=lambda: optimized_cfg.enable_performance_stats
    )
    log_slow_queries: bool = field(
        default_factory=lambda: optimized_cfg.log_slow_queries
    )
    slow_query_threshold: float = field(
        default_factory=lambda: optimized_cfg.slow_query_threshold
    )


cfg = Config()
