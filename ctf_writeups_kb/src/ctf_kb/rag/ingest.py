"""
入库流水线：读取原始 JSONL -> 规范化 -> 去重 -> 切块 -> 写入向量库与轻量索引快照。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from ctf_kb.config import cfg
from ctf_kb.crawler.ctftime import normalize_category, normalize_difficulty
from ctf_kb.models import Chunk
from ctf_kb.rag.chunker import chunk_text


@dataclass(frozen=True)
class NormalizedRecord:
    writeup_id: str
    event: str
    task: str
    title: str
    url: str
    source_url: str
    content: str
    index_content: str
    category: str = "unknown"
    difficulty: str = "unknown"
    year: int = 0
    tags: tuple[str, ...] = ()
    techniques: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    team: str = ""
    points: int = 0
    solves: int = 0
    ctftime_url: str = ""
    external_url: str = ""
    source: str = "archive"


@dataclass(frozen=True)
class IngestStats:
    raw_rows: int = 0
    accepted_records: int = 0
    skipped_duplicates: int = 0
    total_chunks: int = 0


def max_chunks_per_writeup() -> int:
    return max(1, int(getattr(cfg, "max_chunks_per_writeup", 12)))


def _index_snapshot_path() -> Path:
    return Path(getattr(cfg, "index_jsonl", Path(cfg.raw_jsonl).with_name("writeups_index.jsonl")))


def _coerce_int(value: object) -> int:
    try:
        return int(str(value or 0).strip() or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        items = []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        lowered = item.lower()
        if not lowered or lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(lowered)
    return tuple(deduped)


def _truncate_index_content(content: str) -> str:
    text = (content or "").strip()
    if not text:
        return ""
    collapsed = "\n".join(line.rstrip() for line in text.splitlines())
    return collapsed[: max(getattr(cfg, "index_content_chars", 6000), getattr(cfg, "min_index_content_chars", 120))].strip()


def _preferred_url(item: dict) -> str:
    return str(
        item.get("url")
        or item.get("external_url")
        or item.get("ctftime_url")
        or item.get("source_url")
        or ""
    ).strip()


def iter_raw_records(raw_file: Path) -> Iterator[dict]:
    with raw_file.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            if not str(item.get("writeup_id", "") or "").strip():
                continue
            yield item


def normalize_record(item: dict) -> NormalizedRecord:
    content = str(item.get("content", "") or "").strip()
    source_url = str(item.get("source_url", "") or _preferred_url(item)).strip()
    url = _preferred_url(item) or source_url
    event = str(item.get("event", "") or "").strip()
    task = str(item.get("task", "") or item.get("title", "") or "").strip()
    title = str(item.get("title", "") or task or event or "").strip()

    return NormalizedRecord(
        writeup_id=str(item.get("writeup_id", "") or "").strip(),
        event=event,
        task=task,
        title=title,
        url=url,
        source_url=source_url or url,
        content=content,
        index_content=_truncate_index_content(content),
        category=normalize_category(str(item.get("category", "unknown") or "unknown")),
        difficulty=normalize_difficulty(str(item.get("difficulty", "unknown") or "unknown")),
        year=_coerce_int(item.get("year")),
        tags=_normalize_list(item.get("tags", [])),
        techniques=_normalize_list(item.get("techniques", [])),
        tools=_normalize_list(item.get("tools", [])),
        team=str(item.get("team", "") or "").strip(),
        points=_coerce_int(item.get("points")),
        solves=_coerce_int(item.get("solves")),
        ctftime_url=str(item.get("ctftime_url", "") or "").strip(),
        external_url=str(item.get("external_url", "") or "").strip(),
        source=str(item.get("source", "archive") or "archive").strip(),
    )


def _record_fingerprint(record: NormalizedRecord) -> str:
    payload = f"{record.title}\n{record.task}\n{record.index_content}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dedupe_record(
    record: NormalizedRecord,
    seen_ids: set[str],
    seen_urls: set[str],
    seen_hashes: set[str],
) -> bool:
    urls = {value for value in (record.url, record.source_url, record.ctftime_url, record.external_url) if value}
    fingerprint = _record_fingerprint(record)

    if record.writeup_id in seen_ids:
        return False
    if urls & seen_urls:
        return False
    if fingerprint in seen_hashes:
        return False

    seen_ids.add(record.writeup_id)
    seen_urls.update(urls)
    seen_hashes.add(fingerprint)
    return True


def build_chunks(record: NormalizedRecord) -> list[Chunk]:
    source_text = record.index_content or record.content
    chunks = chunk_text(
        source_text,
        size=getattr(cfg, "chunk_size", None),
        overlap=getattr(cfg, "chunk_overlap", None),
        max_chunk=getattr(cfg, "chunk_max_chars", None),
        max_chunks=max_chunks_per_writeup(),
    )
    built: list[Chunk] = []
    for index, text in enumerate(chunks):
        built.append(
            Chunk(
                id=f"{record.writeup_id}_{index}",
                writeup_id=record.writeup_id,
                event=record.event,
                task=record.task,
                title=record.title,
                url=record.url,
                tags="[]",
                chunk_index=index,
                content=text,
                category=record.category,
                difficulty=record.difficulty,
                year=record.year,
                team="",
                points=0,
                solves=0,
                techniques="[]",
                tools="[]",
            )
        )
    return built


def route_chunks(chunks_by_category: dict[str, list[Chunk]], chunks: list[Chunk]) -> None:
    for chunk in chunks:
        chunks_by_category.setdefault(chunk.category, []).append(chunk)


def _snapshot_row(record: NormalizedRecord) -> dict[str, object]:
    payload = asdict(record)
    payload["tags"] = list(record.tags)
    payload["techniques"] = list(record.techniques)
    payload["tools"] = list(record.tools)
    return payload


def flush_batches(chunks_by_category: dict[str, list[Chunk]]) -> dict[str, int]:
    from ctf_kb.vector.factory import get_vector_store

    store = get_vector_store()
    inserted_by_category: dict[str, int] = {}
    for category, chunks in sorted(chunks_by_category.items()):
        inserted_by_category[category] = store.insert_chunks(chunks, category=category)
    chunks_by_category.clear()
    return inserted_by_category


def report_stats(stats: IngestStats, inserted_by_category: dict[str, int]) -> None:
    from ctf_kb.vector.factory import get_vector_store

    store = get_vector_store()
    total = store.count_all()
    print(f"[*] 原始记录: {stats.raw_rows}")
    print(f"[*] 接受记录: {stats.accepted_records}")
    print(f"[*] 跳过重复: {stats.skipped_duplicates}")
    print(f"[*] 总切块数: {stats.total_chunks}")
    print(f"\n完成，本次新增 {sum(inserted_by_category.values())} chunks，总量 {total}。")
    for category, inserted in sorted(inserted_by_category.items()):
        category_total = store.count(category=category)
        print(f"  - {category}: 新增 {inserted}，集合现有 {category_total}")


def ingest(raw_file: Path | None = None) -> None:
    src = raw_file or Path(cfg.raw_jsonl)
    if not src.exists():
        raise FileNotFoundError(f"原始数据文件不存在: {src}\n请先运行 crawl 子命令。")

    snapshot_path = _index_snapshot_path()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    chunks_by_category: dict[str, list[Chunk]] = {}
    inserted_by_category: dict[str, int] = {}
    stats = IngestStats()

    with snapshot_path.open("w", encoding="utf-8") as snapshot_file:
        for item in iter_raw_records(src):
            stats = IngestStats(
                raw_rows=stats.raw_rows + 1,
                accepted_records=stats.accepted_records,
                skipped_duplicates=stats.skipped_duplicates,
                total_chunks=stats.total_chunks,
            )
            record = normalize_record(item)
            if not record.index_content or len(record.index_content) < getattr(cfg, "min_index_content_chars", 120):
                continue
            if not dedupe_record(record, seen_ids, seen_urls, seen_hashes):
                stats = IngestStats(
                    raw_rows=stats.raw_rows,
                    accepted_records=stats.accepted_records,
                    skipped_duplicates=stats.skipped_duplicates + 1,
                    total_chunks=stats.total_chunks,
                )
                continue

            snapshot_file.write(json.dumps(_snapshot_row(record), ensure_ascii=False) + "\n")
            chunks = build_chunks(record)
            route_chunks(chunks_by_category, chunks)
            stats = IngestStats(
                raw_rows=stats.raw_rows,
                accepted_records=stats.accepted_records + 1,
                skipped_duplicates=stats.skipped_duplicates,
                total_chunks=stats.total_chunks + len(chunks),
            )

            if sum(len(bucket) for bucket in chunks_by_category.values()) >= getattr(cfg, "ingest_flush_size", 256):
                flushed = flush_batches(chunks_by_category)
                for category, count in flushed.items():
                    inserted_by_category[category] = inserted_by_category.get(category, 0) + count

    if chunks_by_category:
        flushed = flush_batches(chunks_by_category)
        for category, count in flushed.items():
            inserted_by_category[category] = inserted_by_category.get(category, 0) + count

    report_stats(stats, inserted_by_category)
