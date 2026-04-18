"""
数据模型：Writeup、Chunk、SearchHit。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Writeup:
    writeup_id: str
    event: str
    task: str
    tags: list[str]
    url: str
    content: str
    title: str = ""


@dataclass
class Chunk:
    id: str                  # f"{writeup_id}_{chunk_index}"
    writeup_id: str
    event: str
    task: str
    title: str
    url: str
    tags: str                # JSON array string for Milvus varchar field
    chunk_index: int
    content: str
    category: str = "unknown"
    difficulty: str = "unknown"
    year: int = 0
    team: str = ""
    points: int = 0
    solves: int = 0
    techniques: str = "[]"
    tools: str = "[]"
    vector: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class SearchHit:
    chunk_id: str
    writeup_id: str
    event: str
    task: str
    title: str
    url: str
    chunk_index: int
    score: float
    content: str
    category: str = "unknown"
    difficulty: str = "unknown"
    year: int = 0
    team: str = ""
    points: int = 0
    solves: int = 0
    techniques: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
