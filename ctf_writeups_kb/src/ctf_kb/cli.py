"""
统一 CLI 入口，支持 crawl / ingest / chat / doctor / serve。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


_THIS_FILE = Path(__file__).resolve()
_CTF_WRITEUPS_ROOT = _THIS_FILE.parents[2]
_DEFAULT_SOURCE_LIBRARY = _CTF_WRITEUPS_ROOT / "data" / "source_library_cn.json"


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _dedupe_sequence(values: list[object]) -> list[object]:
    deduped: list[object] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(value)
    return deduped


def _load_seed_urls(args: argparse.Namespace) -> list[str]:
    urls = _parse_csv(args.seed_urls)
    if args.seed_file:
        seed_file = Path(args.seed_file)
        if seed_file.exists():
            urls.extend(
                line.strip()
                for line in seed_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                if line.strip() and not line.strip().startswith("#")
            )
    return [str(value) for value in _dedupe_sequence(urls)]


def _load_source_manifest(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        return []
    if path.suffix.lower() == ".jsonl":
        rows: list[dict[str, object]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            item = json.loads(line)
            if isinstance(item, dict):
                rows.append(item)
        return rows

    data = json.loads(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        sources = data.get("sources", [])
        if isinstance(sources, list):
            return [item for item in sources if isinstance(item, dict)]
    return []


def _load_source_library(path: Path) -> tuple[dict[str, list[str]], list[dict[str, object]]]:
    if not path.exists():
        return {}, []
    raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        return {}, []
    data = json.loads(raw)
    if not isinstance(data, dict):
        return {}, []

    presets_raw = data.get("presets", {})
    presets: dict[str, list[str]] = {}
    if isinstance(presets_raw, dict):
        for key, value in presets_raw.items():
            if isinstance(value, list):
                presets[str(key)] = [str(item) for item in value if str(item).strip()]

    sources_raw = data.get("sources", [])
    sources = [item for item in sources_raw if isinstance(item, dict)] if isinstance(sources_raw, list) else []
    return presets, sources


def _select_library_sources(
    preset_names: list[str],
    library_presets: dict[str, list[str]],
    library_sources: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not preset_names:
        return []
    source_by_id = {
        str(item.get("id", "")).strip(): item
        for item in library_sources
        if str(item.get("id", "")).strip()
    }
    selected: list[dict[str, object]] = []
    for preset in preset_names:
        for source_id in library_presets.get(preset, []):
            item = source_by_id.get(source_id)
            if item:
                selected.append(item)
    return selected


def _load_source_specs(args: argparse.Namespace) -> list[object]:
    sources: list[object] = []
    sources.extend(_load_seed_urls(args))
    library_path = Path(args.source_library) if args.source_library else _DEFAULT_SOURCE_LIBRARY
    library_presets, library_sources = _load_source_library(library_path)
    sources.extend(_select_library_sources(_parse_csv(args.source_presets), library_presets, library_sources))
    for manifest_path in _parse_csv(args.source_manifest):
        sources.extend(_load_source_manifest(Path(manifest_path)))
    return _dedupe_sequence(sources)


def _cmd_crawl(args: argparse.Namespace) -> None:
    from ctf_kb.config import cfg
    from ctf_kb.crawler.ctftime import crawl

    if cfg.offline_mode:
        raise RuntimeError(
            "CTF_WRITEUPS_OFFLINE_MODE=true 时禁止联网 crawl，请先关闭离线模式。"
        )

    pages = args.pages or cfg.crawl_max_pages
    output = Path(args.output) if args.output else Path(cfg.raw_jsonl)
    state = Path(args.state_file) if args.state_file else None
    categories = _parse_csv(args.categories)
    source_specs = _load_source_specs(args)
    asyncio.run(
        crawl(
            output,
            pages=pages,
            state_file=state,
            categories=categories,
            source_specs=source_specs,
        )
    )


def _cmd_ingest(args: argparse.Namespace) -> None:
    from ctf_kb.rag.ingest import ingest

    source = Path(args.file) if args.file else None
    ingest(source)


def _cmd_doctor(_args: argparse.Namespace) -> None:
    from ctf_kb.config import cfg
    from ctf_kb.vector.factory import get_vector_store

    store = get_vector_store()
    raw_path = Path(cfg.raw_jsonl)
    index_path = Path(cfg.index_jsonl)
    milvus_path = Path(cfg.milvus_db_path)
    local_embed_path = Path(cfg.local_embed_model_path) if cfg.local_embed_model_path else None

    print(f"offline_mode={cfg.offline_mode}")
    print(f"vector_backend={cfg.vector_backend}")
    print(f"embedding_backend={store.current_embedding_backend()}")
    print(f"raw_jsonl_exists={raw_path.exists()} path={raw_path}")
    print(f"index_jsonl_exists={index_path.exists()} path={index_path}")
    print(f"milvus_db_exists={milvus_path.exists()} path={milvus_path}")
    print(f"qdrant_path={cfg.qdrant_path}")
    print(f"local_embed_model_exists={bool(local_embed_path and local_embed_path.exists())} path={local_embed_path or ''}")
    print(f"local_llm_configured={bool(cfg.local_llm_base_url and cfg.local_llm_model)}")
    print(f"store_health={json.dumps(store.health(), ensure_ascii=False)}")


def _cmd_chat(_args: argparse.Namespace) -> None:
    from ctf_kb.llm.claude_agent import chat_loop

    chat_loop()


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from ctf_kb.config import cfg

    host = args.host or cfg.api_host
    port = args.port or cfg.api_port
    uvicorn.run("ctf_kb.api.app:app", host=host, port=port, reload=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ctf-writeups-kb",
        description="各大 CTF WP 知识库（Milvus/Qdrant + 在线/离线检索）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl", help="爬取 CTFTime 与 seed writeup")
    p_crawl.add_argument("--pages", type=int, default=0, help="列表页数（默认读取 CRAWL_MAX_PAGES 环境变量）")
    p_crawl.add_argument("--output", type=str, default="", help="输出 JSONL 路径")
    p_crawl.add_argument("--state-file", type=str, default="", help="页码断点状态文件路径")
    p_crawl.add_argument("--categories", type=str, default="", help="仅保留指定分类，逗号分隔")
    p_crawl.add_argument("--seed-urls", type=str, default="", help="附加爬取的种子 URL，逗号分隔")
    p_crawl.add_argument("--seed-file", type=str, default="", help="种子 URL 文件，每行一个")
    p_crawl.add_argument("--source-manifest", type=str, default="", help="来源清单文件，支持 JSON / JSONL，可逗号分隔多个")
    p_crawl.add_argument("--source-library", type=str, default="", help="来源库文件，默认使用 ctf_writeups_kb/data/source_library_cn.json")
    p_crawl.add_argument("--source-presets", type=str, default="", help="来源集合，如 cn-curated,cn-major,cn-web,cn-pwn,cn-crypto,cn-teams")

    p_ingest = sub.add_parser("ingest", help="将 JSONL 导入当前向量后端")
    p_ingest.add_argument("--file", type=str, default="", help="输入 JSONL 路径")

    sub.add_parser("chat", help="交互式知识库问答")
    sub.add_parser("doctor", help="检查离线依赖、索引快照与向量库状态")

    p_serve = sub.add_parser("serve", help="启动 HTTP API 服务")
    p_serve.add_argument("--host", type=str, default="", help="监听地址")
    p_serve.add_argument("--port", type=int, default=0, help="端口号")

    ns = parser.parse_args(argv if argv is not None else sys.argv[1:])
    {
        "crawl": _cmd_crawl,
        "ingest": _cmd_ingest,
        "chat": _cmd_chat,
        "doctor": _cmd_doctor,
        "serve": _cmd_serve,
    }[ns.command](ns)


if __name__ == "__main__":
    main()
