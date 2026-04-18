"""
向量层通用工具。
"""
from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from typing import Any


MAX_STR = 512
MAX_CONTENT = 4096
SUPPORTED_CATEGORIES = ("web", "pwn", "crypto", "misc", "reverse", "forensics", "osint", "unknown")
CORE_QDRANT_BUCKETS = ("web", "pwn", "crypto", "misc")


class LRUCache:
    def __init__(self, maxsize: int):
        self.maxsize = max(1, int(maxsize))
        self.cache: OrderedDict[str, Any] = OrderedDict()
        self.lock = threading.RLock()

    def get(self, key: str) -> Any:
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                return self.cache[key]
            return None

    def put(self, key: str, value: Any) -> None:
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
            elif len(self.cache) >= self.maxsize:
                self.cache.popitem(last=False)
            self.cache[key] = value

    def clear(self) -> None:
        with self.lock:
            self.cache.clear()


def sanitize_collection_token(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", str(value or "").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or default


def normalize_category(category: str | None) -> str:
    value = (category or "unknown").strip().lower()
    aliases = {
        "rev": "reverse",
        "re": "reverse",
        "miscellaneous": "misc",
        "forensic": "forensics",
    }
    normalized = aliases.get(value, value)
    return normalized if normalized in SUPPORTED_CATEGORIES else "unknown"


def resolve_qdrant_bucket(category: str | None) -> str:
    normalized = normalize_category(category)
    return normalized if normalized in CORE_QDRANT_BUCKETS else "shared"


def stable_text_hash(text: str) -> str:
    normalized = " ".join((text or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
