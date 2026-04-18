#!/usr/bin/env python3
"""
离线入库脚本：从 writeups_index.jsonl 直接导入到 Milvus，无需 crawl4ai
"""
import json
import sys
from pathlib import Path

# 添加路径
sys.path.insert(0, str(Path(__file__).parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from ctf_kb.config import cfg
from ctf_kb.models import Chunk
from ctf_kb.vector.factory import get_vector_store
from ctf_kb.rag.chunker import chunk_text


def load_index_records(index_file: Path):
    """从索引文件加载记录"""
    records = []
    with index_file.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict) and record.get("writeup_id"):
                    records.append(record)
            except json.JSONDecodeError:
                continue
    return records


def build_chunks_from_record(record: dict, max_chunks: int = 12) -> list[Chunk]:
    """从记录构建 chunks"""
    content = record.get("index_content") or record.get("content", "")
    if not content.strip():
        return []

    chunks = chunk_text(
        content,
        size=getattr(cfg, "chunk_size", None),
        overlap=getattr(cfg, "chunk_overlap", None),
        max_chunk=getattr(cfg, "chunk_max_chars", None),
        max_chunks=max_chunks,
    )

    built = []
    for index, text in enumerate(chunks):
        built.append(
            Chunk(
                id=f"{record['writeup_id']}_{index}",
                writeup_id=record["writeup_id"],
                event=record.get("event", ""),
                task=record.get("task", ""),
                title=record.get("title", ""),
                url=record.get("url", ""),
                tags="[]",
                chunk_index=index,
                content=text,
                category=record.get("category", "unknown"),
                difficulty=record.get("difficulty", "unknown"),
                year=record.get("year", 0),
                team="",
                points=0,
                solves=0,
                techniques="[]",
                tools="[]",
            )
        )
    return built


def main():
    index_file = Path(cfg.index_jsonl)
    if not index_file.exists():
        print(f"错误: 索引文件不存在: {index_file}")
        return 1

    print(f"[*] 从索引文件加载: {index_file}")
    records = load_index_records(index_file)
    print(f"[*] 加载了 {len(records)} 条记录")

    if not records:
        print("错误: 没有可用的记录")
        return 1

    # 按 category 分组
    chunks_by_category = {}
    total_chunks = 0

    for i, record in enumerate(records, 1):
        if i % 50 == 0:
            print(f"[*] 处理进度: {i}/{len(records)}")

        chunks = build_chunks_from_record(record)
        if not chunks:
            continue

        category = record.get("category", "unknown")
        chunks_by_category.setdefault(category, []).extend(chunks)
        total_chunks += len(chunks)

    print(f"\n[*] 总共生成 {total_chunks} 个 chunks")
    print(f"[*] 分类分布:")
    for cat, chunks in sorted(chunks_by_category.items()):
        print(f"    - {cat}: {len(chunks)} chunks")

    # 写入向量库
    print("\n[*] 开始写入向量库...")
    store = get_vector_store()

    for category, chunks in sorted(chunks_by_category.items()):
        print(f"[*] 写入 {category}: {len(chunks)} chunks")
        inserted = store.insert_chunks(chunks, category=category)
        print(f"    ✓ 成功插入 {inserted} chunks")

    # 统计
    total = store.count_all()
    print(f"\n[✓] 完成！向量库总量: {total} chunks")

    for category in sorted(chunks_by_category.keys()):
        count = store.count(category=category)
        print(f"    - {category}: {count} chunks")

    return 0


if __name__ == "__main__":
    sys.exit(main())
