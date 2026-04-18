"""
文本切块：结构优先、低重叠、可限制单文档块数。
"""
from __future__ import annotations

import re

from ctf_kb.config import cfg


DEFAULT_CHUNK_SIZE = 1_200
DEFAULT_OVERLAP = 96
DEFAULT_MAX_CHUNK = 1_800


def _split_paragraphs(text: str) -> list[str]:
    parts: list[str] = []
    code_block_re = re.compile(r"```.*?```", re.DOTALL)
    pos = 0
    for match in code_block_re.finditer(text):
        before = text[pos : match.start()]
        if before.strip():
            parts.extend(part for part in re.split(r"\n{2,}", before) if part.strip())
        parts.append(match.group(0))
        pos = match.end()
    tail = text[pos:]
    if tail.strip():
        parts.extend(part for part in re.split(r"\n{2,}", tail) if part.strip())
    return parts


def _chunk_limits(
    *,
    size: int | None = None,
    overlap: int | None = None,
    max_chunk: int | None = None,
) -> tuple[int, int, int]:
    chunk_size = max(200, int(size or getattr(cfg, "chunk_size", DEFAULT_CHUNK_SIZE)))
    chunk_overlap = max(0, int(overlap if overlap is not None else getattr(cfg, "chunk_overlap", DEFAULT_OVERLAP)))
    hard_limit = max(chunk_size, int(max_chunk or getattr(cfg, "chunk_max_chars", DEFAULT_MAX_CHUNK)))
    return chunk_size, min(chunk_overlap, chunk_size // 2), hard_limit


def chunk_text(
    text: str,
    *,
    size: int | None = None,
    overlap: int | None = None,
    max_chunk: int | None = None,
    max_chunks: int | None = None,
) -> list[str]:
    chunk_size, chunk_overlap, hard_limit = _chunk_limits(size=size, overlap=overlap, max_chunk=max_chunk)
    paragraphs = _split_paragraphs(text)
    if not paragraphs:
        return [text[:hard_limit]] if text.strip() else []

    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(paragraph) > hard_limit:
            if buffer.strip():
                chunks.append(buffer.strip())
                buffer = ""
            for index in range(0, len(paragraph), chunk_size):
                chunks.append(paragraph[index : index + chunk_size].strip())
        elif len(buffer) + len(paragraph) + 2 > chunk_size and buffer:
            chunks.append(buffer.strip())
            overlap_prefix = buffer[-chunk_overlap:] if chunk_overlap else ""
            buffer = f"{overlap_prefix}\n\n{paragraph}".strip()
        else:
            buffer = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph

        if max_chunks and len(chunks) >= max_chunks:
            return chunks[:max_chunks]

    if buffer.strip() and (not max_chunks or len(chunks) < max_chunks):
        chunks.append(buffer.strip())

    return chunks if chunks else [text[:hard_limit]]
