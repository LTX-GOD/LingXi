"""
FastAPI HTTP 服务。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from memory.knowledge_store import (
    KNOWLEDGE_BUCKET_EXTERNAL,
    KNOWLEDGE_BUCKET_FORUM,
    KNOWLEDGE_BUCKET_MAIN,
    KnowledgeRecord,
    get_knowledge_store,
    search_knowledge_records,
)
from ctf_kb.config import cfg
from ctf_kb.llm.claude_agent import run_agent
from ctf_kb.rag.retriever import SearchFilters, retrieve_filtered
from ctf_kb.vector.factory import get_vector_store

app = FastAPI(title="CTF KB API", version="0.3.0")
_LOCAL_EXPERIENCE_BUCKETS = {
    KNOWLEDGE_BUCKET_MAIN,
    KNOWLEDGE_BUCKET_FORUM,
}


@app.get("/health")
def health() -> dict[str, Any]:
    return get_vector_store().health()


@app.get("/search")
def search(
    q: str = Query(..., description="查询关键词"),
    top_k: int = Query(default=5, ge=1, le=20),
    event: str | None = Query(default=None),
    task: str | None = Query(default=None),
    category: str | None = Query(default=None),
    difficulty: str | None = Query(default=None),
    year: int | None = Query(default=None, ge=2010, le=2035),
    bucket: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    outcome_type: str | None = Query(default=None),
) -> dict[str, Any]:
    if not q.strip():
        raise HTTPException(status_code=400, detail="q 不能为空")
    if bucket in _LOCAL_EXPERIENCE_BUCKETS:
        hits = search_knowledge_records(
            q,
            bucket=bucket,
            top_k=top_k,
            source_type=source_type,
            outcome_type=outcome_type,
            category=category,
        )
        return {
            "query": q,
            "filters": {
                "bucket": bucket,
                "source_type": source_type,
                "outcome_type": outcome_type,
                "category": category,
            },
            "total": len(hits),
            "results": [
                {
                    "record_id": hit.record.record_id,
                    "source": hit.record.bucket,
                    "source_type": hit.record.source_type,
                    "outcome_type": hit.record.outcome_type,
                    "challenge_code": hit.record.challenge_code,
                    "scope_key": hit.record.scope_key,
                    "zone": hit.record.zone,
                    "category": hit.record.category,
                    "confidence": round(hit.record.confidence, 4),
                    "verification_state": hit.record.verification_state,
                    "quality_score": round(hit.record.quality_score, 4),
                    "content": (
                        hit.record.summary
                        or " | ".join(hit.record.evidence[:2])
                        or "knowledge record"
                    )[:600],
                }
                for hit in hits
            ],
        }
    hits = retrieve_filtered(
        q,
        SearchFilters(
            event=event,
            task=task,
            category=category,
            difficulty=difficulty,
            year=year,
            top_k=top_k,
        ),
    )
    return {
        "query": q,
        "filters": {
            "event": event,
            "task": task,
            "category": category,
            "difficulty": difficulty,
            "year": year,
            "bucket": bucket or KNOWLEDGE_BUCKET_EXTERNAL,
            "source_type": source_type or "external_writeup",
            "outcome_type": outcome_type or "reference",
        },
        "total": len(hits),
        "results": [
            {
                "chunk_id": hit.chunk_id,
                "writeup_id": hit.writeup_id,
                "event": hit.event,
                "task": hit.task,
                "url": hit.url,
                "category": hit.category,
                "difficulty": hit.difficulty,
                "year": hit.year,
                "team": hit.team,
                "score": round(hit.score, 4),
                "content": hit.content[:600],
                "source": KNOWLEDGE_BUCKET_EXTERNAL,
                "source_type": "external_writeup",
                "outcome_type": "reference",
            }
            for hit in hits
        ],
    }


class ExperienceIngestRequest(BaseModel):
    bucket: str
    record: dict[str, Any]


@app.post("/experience/ingest")
def experience_ingest(req: ExperienceIngestRequest) -> dict[str, Any]:
    if req.bucket not in _LOCAL_EXPERIENCE_BUCKETS:
        raise HTTPException(status_code=400, detail="bucket 非法")
    merged = dict(req.record or {})
    merged["bucket"] = req.bucket
    record = KnowledgeRecord.from_dict(merged)
    if not record.record_id:
        raise HTTPException(status_code=400, detail="record_id 不能为空")
    get_knowledge_store().ingest(record)
    return {
        "status": "ok",
        "bucket": record.bucket,
        "record_id": record.record_id,
    }


@app.post("/experience/vector_ingest")
def experience_vector_ingest(req: ExperienceIngestRequest) -> dict[str, Any]:
    if req.bucket not in _LOCAL_EXPERIENCE_BUCKETS:
        raise HTTPException(status_code=400, detail="bucket 非法")
    merged = dict(req.record or {})
    merged["bucket"] = req.bucket
    record = KnowledgeRecord.from_dict(merged)
    if not record.record_id:
        raise HTTPException(status_code=400, detail="record_id 不能为空")

    from ctf_kb.models import Chunk
    from ctf_kb.vector.factory import get_vector_store

    chunks: list[Chunk] = []
    base_id = record.record_id
    chunk_idx = 0

    # 构建基础元数据
    category = record.category or "unknown"
    challenge_code = record.challenge_code or ""
    zone = record.zone or ""

    # Chunk 1: Summary
    if record.summary and record.summary.strip():
        chunks.append(Chunk(
            id=f"{base_id}_chunk_{chunk_idx}",
            writeup_id=base_id,
            event=f"{zone}_{challenge_code}" if zone and challenge_code else challenge_code,
            task=challenge_code,
            title=record.summary[:100],
            url="",
            tags="[]",
            chunk_index=chunk_idx,
            content=record.summary,
            category=category,
            difficulty="unknown",
            year=0,
        ))
        chunk_idx += 1

    # Chunk 2-N: Evidence items (限制前5条)
    for evidence in (record.evidence or [])[:5]:
        if evidence and evidence.strip():
            chunks.append(Chunk(
                id=f"{base_id}_chunk_{chunk_idx}",
                writeup_id=base_id,
                event=f"{zone}_{challenge_code}" if zone and challenge_code else challenge_code,
                task=challenge_code,
                title=f"Evidence: {evidence[:50]}",
                url="",
                tags="[]",
                chunk_index=chunk_idx,
                content=evidence,
                category=category,
                difficulty="unknown",
                year=0,
            ))
            chunk_idx += 1

    if not chunks:
        return {
            "status": "skipped",
            "reason": "no_content",
            "bucket": record.bucket,
            "record_id": record.record_id,
            "chunks_created": 0,
        }

    inserted = get_vector_store().insert_chunks(chunks, category=category)

    return {
        "status": "ok",
        "bucket": record.bucket,
        "record_id": record.record_id,
        "chunks_created": len(chunks),
        "chunks_inserted": inserted,
    }


class ChatRequest(BaseModel):
    message: str


@app.post("/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="message 不能为空")
    answer = run_agent(req.message, stream_print=False)
    return {"answer": answer}
