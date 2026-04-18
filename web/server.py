"""
Ling-Xi Web Dashboard — FastAPI 后端
====================================
REST API + SSE 实时推送，支持:
  - 任务下发 (创建攻击任务)
  - 任务暂停/恢复/中止
  - 赛区进度监控
  - 实时日志流

⚠️ 合规: 仅用于管理 Agent 任务，所有攻击操作由 Agent 自主执行。
"""
import asyncio
import json
import time
import os
import logging
import re
import uuid
from functools import lru_cache
from typing import Dict, List, Any, Optional
from datetime import datetime
from collections import deque
from enum import Enum
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from log_utils import redact_sensitive_text

logger = logging.getLogger("lingxi.web")

KNOWLEDGE_BUCKET_MAIN = "main_battle_memory"
KNOWLEDGE_BUCKET_FORUM = "forum_memory"
KNOWLEDGE_BUCKET_EXTERNAL = "ctf_writeups"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#-]{2,}|[\u4e00-\u9fff]{2,}")
_ROOT_DIR = Path(__file__).resolve().parent.parent
_CTF_WRITEUPS_ROOT = _ROOT_DIR / "ctf_writeups_kb"
_LEGACY_WRITEUPS_ROOT = _ROOT_DIR / "tou"
_ACTIVE_WRITEUPS_ROOT = _CTF_WRITEUPS_ROOT if _CTF_WRITEUPS_ROOT.exists() else _LEGACY_WRITEUPS_ROOT
_ACTIVE_WRITEUPS_DATA = _ACTIVE_WRITEUPS_ROOT / "data"
_ACTIVE_WRITEUPS_SRC = _ACTIVE_WRITEUPS_ROOT / "src"
_EXTERNAL_RAW_PATH = _ACTIVE_WRITEUPS_DATA / "writeups_raw.jsonl"
_EXTERNAL_INDEX_PATH = _ACTIVE_WRITEUPS_DATA / "writeups_index.jsonl"
_EXTERNAL_DB_PATH = _ACTIVE_WRITEUPS_DATA / "milvus.db"
_EXTERNAL_SOURCE_LIBRARY_PATH = _ACTIVE_WRITEUPS_DATA / "source_library_cn.json"
_EXTERNAL_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web": (
        "web", "xss", "sqli", "sql injection", "ssti", "ssrf", "csrf", "xxe", "jwt",
        "template injection", "upload", "php", "flask", "django", "cookie", "http",
    ),
    "pwn": (
        "pwn", "pwning", "heap", "stack", "rop", "ret2libc", "format string", "uaf",
        "glibc", "pwntools", "shellcode",
    ),
    "crypto": (
        "crypto", "rsa", "aes", "ecdsa", "xor", "cbc", "ecb", "padding oracle",
        "lattice", "number theory", "sage",
    ),
    "reverse": (
        "reverse", "rev", "re ", "ghidra", "ida", "angr", "decompile", "bytecode",
        "symbolic execution",
    ),
    "forensics": (
        "forensics", "forensic", "pcap", "wireshark", "volatility", "memory dump",
        "disk image", "registry", "stego",
    ),
    "osint": (
        "osint", "open source intelligence", "metadata leak", "google dork", "social media",
    ),
    "misc": (
        "misc", "miscellaneous", "jail", "pyjail", "bash jail", "automation", "qr",
        "captcha", "programming", "protocol",
    ),
}
_EXTERNAL_CATEGORY_ALIASES = {
    "web": "web",
    "pwn": "pwn",
    "crypto": "crypto",
    "misc": "misc",
    "miscellaneous": "misc",
    "reverse": "reverse",
    "rev": "reverse",
    "re": "reverse",
    "forensics": "forensics",
    "forensic": "forensics",
    "osint": "osint",
    "unknown": "unknown",
}
_EXTERNAL_NOISE_FRAGMENTS = (
    "this website uses cookies",
    "privacy policy",
    "ctftime.org/static/images/ct/logo.svg",
    "sign in",
    "upcoming",
    "archive",
    "past events",
    "calendar",
    "compare",
    "create new team",
    "feedback",
    "contact us",
    "hosting provided by",
    "share this post",
    "all tasks and writeups are copyrighted",
    "faq",
    "rating(",
)
_EXTERNAL_METADATA_PREFIXES = (
    "category:",
    "event:",
    "points:",
    "service:",
    "tags:",
    "rating:",
    "by ",
    "comments",
    "flag format:",
)


def _default_bucket_display_name(bucket: str | None) -> str:
    normalized = str(bucket or "").strip()
    if normalized == KNOWLEDGE_BUCKET_EXTERNAL:
        return "各大 CTF WP"
    if normalized == KNOWLEDGE_BUCKET_FORUM:
        return "论坛记忆"
    return "主战场记忆"


def _default_normalize_bucket(bucket: str | None) -> str:
    normalized = str(bucket or "").strip()
    if normalized in {KNOWLEDGE_BUCKET_EXTERNAL, "external", "external_writeups", "tou_external_writeups", "ctf_wp"}:
        return KNOWLEDGE_BUCKET_EXTERNAL
    if normalized in {KNOWLEDGE_BUCKET_FORUM, "forum_experience", "lingxi_forum_experience"}:
        return KNOWLEDGE_BUCKET_FORUM
    return KNOWLEDGE_BUCKET_MAIN


# 导入知识库模块
try:
    from memory.knowledge_store import (
        get_knowledge_store,
        KNOWLEDGE_BUCKET_MAIN as _LOCAL_KNOWLEDGE_BUCKET_MAIN,
        KNOWLEDGE_BUCKET_FORUM as _LOCAL_KNOWLEDGE_BUCKET_FORUM,
        KNOWLEDGE_BUCKET_EXTERNAL as _LOCAL_KNOWLEDGE_BUCKET_EXTERNAL,
        bucket_display_name as _bucket_display_name,
        normalize_bucket as _normalize_bucket,
        search_knowledge_records,
    )
    from memory.knowledge_service import (
        get_knowledge_service_base_url,
        knowledge_service_enabled,
        search_knowledge_service,
    )
    KNOWLEDGE_BUCKET_MAIN = _LOCAL_KNOWLEDGE_BUCKET_MAIN
    KNOWLEDGE_BUCKET_FORUM = _LOCAL_KNOWLEDGE_BUCKET_FORUM
    KNOWLEDGE_BUCKET_EXTERNAL = _LOCAL_KNOWLEDGE_BUCKET_EXTERNAL
    bucket_display_name = _bucket_display_name
    normalize_bucket = _normalize_bucket
    _knowledge_available = True
except ImportError:
    def get_knowledge_store():
        raise RuntimeError("Knowledge store not available")

    def search_knowledge_records(*args, **kwargs):
        return []

    def knowledge_service_enabled() -> bool:
        return False

    def get_knowledge_service_base_url() -> str:
        return "http://127.0.0.1:8791"

    def search_knowledge_service(*args, **kwargs):
        return {"query": "", "total": 0, "results": [], "backend": "disabled"}

    bucket_display_name = _default_bucket_display_name
    normalize_bucket = _default_normalize_bucket
    _knowledge_available = False
    logger.warning("[Web] Knowledge store module not available")

app = FastAPI(title="Ling-Xi Dashboard", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ═══════════════════════════════════════════════════════
# 1. 全局状态
# ═══════════════════════════════════════════════════════

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class TaskRecord:
    """单个攻击任务"""
    def __init__(self, task_id: str, challenge_code: str, target: str,
                 difficulty: str = "", points: int = 0, zone: str = ""):
        self.task_id = task_id
        self.challenge_code = challenge_code  # 兼容旧名，实际对应官方 code
        self.target = target
        self.difficulty = difficulty
        self.points = points  # 对应官方 total_score
        self.zone = zone
        self.status = TaskStatus.PENDING
        self.created_at = datetime.now()
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.attempts = 0
        self.flag = ""
        self.error = ""
        self._asyncio_task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "challenge_code": self.challenge_code,
            "target": self.target,
            "difficulty": self.difficulty,
            "points": self.points,
            "zone": self.zone,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "attempts": self.attempts,
            "flag": self.flag,
            "error": self.error,
        }


# 全局数据
_agent_state = {
    "status": "idle",       # idle / running / paused
    "start_time": 0,
    "total_score": 0,
    "total_solved": 0,
}

_zones: List[dict] = []
_tasks: Dict[str, TaskRecord] = {}
_log_buffer: deque = deque(maxlen=500)
_sse_clients: List[asyncio.Queue] = []

# 外部注入的回调（由 main.py 设置）
_on_start_task = None   # async def(challenge_code: str) -> None
_on_pause_task = None   # async def(task_id: str) -> None
_on_abort_task = None   # async def(task_id: str) -> None
_on_resume_task = None  # async def(task_id: str) -> None
_on_start_all = None    # async def() -> None
_on_pause_all = None    # async def() -> None


def register_callbacks(
    on_start_task=None, on_pause_task=None, on_abort_task=None,
    on_resume_task=None, on_start_all=None, on_pause_all=None,
):
    """main.py 注册任务控制回调"""
    global _on_start_task, _on_pause_task, _on_abort_task
    global _on_resume_task, _on_start_all, _on_pause_all
    _on_start_task = on_start_task
    _on_pause_task = on_pause_task
    _on_abort_task = on_abort_task
    _on_resume_task = on_resume_task
    _on_start_all = on_start_all
    _on_pause_all = on_pause_all


# ═══════════════════════════════════════════════════════
# 2. 状态更新 API（供 main.py 调用）
# ═══════════════════════════════════════════════════════

def update_agent_state(data: dict):
    _agent_state.update(data)
    _broadcast({"type": "agent_state", "data": {**_agent_state, "zones": _zones}})


def update_zones(zones_data: list):
    global _zones
    _zones = zones_data
    _broadcast({"type": "zones", "data": _zones})


def upsert_task(rec: TaskRecord):
    _tasks[rec.task_id] = rec
    _broadcast({"type": "task_update", "data": rec.to_dict()})


def get_task_record(task_id: str) -> Optional[TaskRecord]:
    """按 task_id 获取任务记录（供主流程回调查询）。"""
    return _tasks.get(task_id)


def push_log(level: str, message: str, source: str = "agent"):
    safe_message = redact_sensitive_text(message)
    entry = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "source": source,
        "message": safe_message,
    }
    _log_buffer.append(entry)
    _broadcast({"type": "log", "data": entry})


def push_event(event_type: str, data: dict):
    _broadcast({"type": event_type, "data": data})


def _broadcast(msg: dict):
    dead = []
    for q in _sse_clients:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _sse_clients.remove(q)


# ═══════════════════════════════════════════════════════
# 3. 知识库辅助函数
# ═══════════════════════════════════════════════════════

_BUCKET_ALIASES: dict[str, set[str]] = {
    KNOWLEDGE_BUCKET_MAIN: {
        "main",
        KNOWLEDGE_BUCKET_MAIN,
        "main_memory",
        "lingxi_main_experience",
    },
    KNOWLEDGE_BUCKET_FORUM: {
        "forum",
        KNOWLEDGE_BUCKET_FORUM,
        "forum_memory",
        "forum_experience",
        "lingxi_forum_experience",
    },
    KNOWLEDGE_BUCKET_EXTERNAL: {
        KNOWLEDGE_BUCKET_EXTERNAL,
        "external",
        "external_ctf_wp",
        "external_writeups",
        "tou_external_writeups",
        "ctf_wp",
    },
}


def _resolve_bucket(bucket: str) -> str:
    raw = str(bucket or "").strip()
    for canonical, aliases in _BUCKET_ALIASES.items():
        if raw in aliases:
            return canonical
    raise HTTPException(status_code=404, detail=f"Unknown knowledge bucket: {bucket}")


def _collapse_text(text: str | None, limit: int = 260) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value or default).strip() or default)
    except (TypeError, ValueError):
        return default


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _TOKEN_RE.findall(str(text or "").lower()):
        if token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _path_mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return None


def _latest_mtime_iso(paths: list[Path]) -> str | None:
    timestamps: list[float] = []
    for path in paths:
        try:
            timestamps.append(path.stat().st_mtime)
        except OSError:
            continue
    if not timestamps:
        return None
    return datetime.fromtimestamp(max(timestamps)).isoformat()


def _normalize_external_category_value(value: str | None) -> str:
    lowered = str(value or "").strip().lower()
    if not lowered:
        return "unknown"
    if lowered in _EXTERNAL_CATEGORY_ALIASES:
        return _EXTERNAL_CATEGORY_ALIASES[lowered]
    return "unknown"


def _normalize_external_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return [str(item or "").strip() for item in parsed if str(item or "").strip()]
            except json.JSONDecodeError:
                pass
        return [token.strip() for token in re.split(r"[,/|]", text) if token.strip()]
    if isinstance(raw, list):
        return [str(item or "").strip() for item in raw if str(item or "").strip()]
    return []


def _strip_markdown_links(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("```", "\n")
    text = text.replace("**", "")
    text = text.replace("__", "")
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


def _prefer_external_body(text: str) -> str:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return ""
    if "[EXTERNAL]" in normalized:
        normalized = normalized.split("[EXTERNAL]", 1)[1].strip()
    elif "[CTFTIME]" in normalized:
        normalized = normalized.split("[CTFTIME]", 1)[1].strip()
        heading_match = re.search(r"(?m)^(?:#|##)\s+\S", normalized)
        if heading_match:
            normalized = normalized[heading_match.start():].strip()
    normalized = re.sub(r"^https?://\S+\s*", "", normalized)
    return normalized.strip()


def _clean_external_text(text: str, *, limit: int = 2400) -> str:
    preferred = _strip_markdown_links(_prefer_external_body(text))
    kept_lines: list[str] = []
    previous = ""
    for raw_line in preferred.splitlines():
        line = re.sub(r"^[#>*+\-\s]+", "", raw_line).strip()
        if not line:
            continue
        lowered = line.lower()
        if re.fullmatch(r"https?://\S+", line):
            continue
        if any(fragment in lowered for fragment in _EXTERNAL_NOISE_FRAGMENTS):
            continue
        if lowered in {
            "ctfs", "tasks", "writeups", "teams", "home", "about", "contact", "upcoming",
            "archive", "calendar", "rating", "compare",
        }:
            continue
        line = re.sub(r"\s+", " ", line).strip(" |")
        if not line or line == previous:
            continue
        previous = line
        kept_lines.append(line)
        if sum(len(item) for item in kept_lines) >= limit:
            break
    return "\n".join(kept_lines).strip()


def _extract_external_heading(cleaned_text: str) -> str:
    for line in cleaned_text.splitlines()[:8]:
        candidate = str(line or "").strip()
        lowered = candidate.lower()
        if not candidate or len(candidate) > 120:
            continue
        if lowered.startswith(_EXTERNAL_METADATA_PREFIXES):
            continue
        return candidate
    return ""


def _clean_external_title(text: str, *, inferred_category: str = "unknown") -> str:
    candidate = str(text or "").strip().replace("\\", "/")
    if not candidate:
        return ""
    parts = [part.strip() for part in candidate.split("/") if part.strip()]
    if len(parts) >= 2 and _normalize_external_category_value(parts[0]) != "unknown":
        candidate = "/".join(parts[1:])
    candidate = re.sub(r"^[#>*+\-\s]+", "", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" -_/")
    if inferred_category != "unknown":
        prefix = f"{inferred_category}/"
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix):].strip()
    return candidate or str(text or "").strip()


def _infer_external_category(item: dict[str, Any], cleaned_text: str) -> str:
    explicit = _normalize_external_category_value(item.get("category"))
    if explicit != "unknown":
        return explicit

    for tag in _normalize_external_tags(item.get("tags")):
        normalized = _normalize_external_category_value(tag)
        if normalized != "unknown":
            return normalized

    task_or_title = f"{item.get('task', '')} {item.get('title', '')}".replace("\\", "/")
    path_prefix = task_or_title.split("/", 1)[0].strip().lower()
    normalized_prefix = _normalize_external_category_value(path_prefix)
    if normalized_prefix != "unknown":
        return normalized_prefix

    corpus = "\n".join(
        part for part in [
            str(item.get("task", "") or ""),
            str(item.get("title", "") or ""),
            str(item.get("event", "") or ""),
            str(item.get("url", "") or ""),
            cleaned_text[:6000],
        ]
        if part
    ).lower()
    scores = {category: 0 for category in _EXTERNAL_CATEGORY_KEYWORDS}
    for category, keywords in _EXTERNAL_CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in corpus:
                scores[category] += 1
    best_category, best_score = max(scores.items(), key=lambda entry: entry[1])
    return best_category if best_score > 0 else "unknown"


def _build_external_summary(cleaned_text: str, *, title: str = "", event: str = "") -> str:
    candidates: list[str] = []
    title_l = str(title or "").strip().lower()
    event_l = str(event or "").strip().lower()
    for line in cleaned_text.splitlines():
        candidate = str(line or "").strip()
        lowered = candidate.lower()
        if not candidate:
            continue
        if lowered in {title_l, event_l}:
            continue
        if lowered.startswith(_EXTERNAL_METADATA_PREFIXES):
            continue
        if len(candidate) < 24:
            continue
        candidates.append(candidate)
        if sum(len(item) for item in candidates) >= 320:
            break
    base = " ".join(candidates[:2]) or cleaned_text
    return _collapse_text(base, limit=360)


def _extract_external_year(*parts: str) -> int:
    for part in parts:
        for match in re.findall(r"\b(20\d{2})\b", str(part or "")):
            year = int(match)
            if 2010 <= year <= 2035:
                return year
    return 0


def _external_record_lookup() -> dict[str, dict[str, Any]]:
    records, _ = _external_source_records()
    lookup: dict[str, dict[str, Any]] = {}
    for item in records:
        record_id = str(item.get("writeup_id", "") or "").strip()
        if record_id and record_id not in lookup:
            lookup[record_id] = item
    return lookup


@lru_cache(maxsize=8)
def _load_jsonl_records_cached(path_text: str, mtime_ns: int) -> tuple[dict[str, Any], ...]:
    path = Path(path_text)
    if not path.exists() or mtime_ns < 0:
        return ()
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return tuple(records)


def _load_jsonl_records(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return ()
    return _load_jsonl_records_cached(str(path), mtime_ns)


def _external_source_records() -> tuple[tuple[dict[str, Any], ...], str]:
    if _EXTERNAL_INDEX_PATH.exists():
        return _load_jsonl_records(_EXTERNAL_INDEX_PATH), "index_snapshot"
    if _EXTERNAL_RAW_PATH.exists():
        return _load_jsonl_records(_EXTERNAL_RAW_PATH), "raw_archive"
    return (), "missing_assets"


def _build_external_status() -> dict[str, Any]:
    assets = {
        "root": str(_ACTIVE_WRITEUPS_ROOT),
        "root_exists": _ACTIVE_WRITEUPS_ROOT.exists(),
        "src_exists": _ACTIVE_WRITEUPS_SRC.exists(),
        "data_exists": _ACTIVE_WRITEUPS_DATA.exists(),
        "raw_exists": _EXTERNAL_RAW_PATH.exists(),
        "index_exists": _EXTERNAL_INDEX_PATH.exists(),
        "db_exists": _EXTERNAL_DB_PATH.exists(),
        "source_library_exists": _EXTERNAL_SOURCE_LIBRARY_PATH.exists(),
    }
    health: dict[str, Any] | None = None
    if knowledge_service_enabled():
        try:
            with httpx.Client(timeout=0.8) as client:
                resp = client.get(f"{get_knowledge_service_base_url().rstrip('/')}/health")
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    health = body
        except Exception:
            health = None

    browse_ready = assets["index_exists"] or assets["raw_exists"]
    if health:
        status = str(health.get("status", "running") or "running").lower()
    elif not assets["root_exists"] or not assets["src_exists"]:
        status = "missing_assets"
    elif browse_ready and not assets["db_exists"]:
        status = "degraded"
    elif browse_ready and assets["db_exists"]:
        status = "stopped"
    elif assets["db_exists"]:
        status = "index_missing"
    else:
        status = "missing_assets"

    return {
        "status": status,
        "service_enabled": knowledge_service_enabled(),
        "base_url": get_knowledge_service_base_url(),
        "assets": assets,
        "health": health or {},
        "browse_ready": browse_ready,
        "updated_at": _latest_mtime_iso(
            [
                _EXTERNAL_RAW_PATH,
                _EXTERNAL_INDEX_PATH,
                _EXTERNAL_DB_PATH,
                _EXTERNAL_SOURCE_LIBRARY_PATH,
            ]
        ),
    }


def _normalize_external_record(item: dict[str, Any]) -> dict[str, Any]:
    url = str(
        item.get("url")
        or item.get("source_url")
        or item.get("external_url")
        or item.get("ctftime_url")
        or ""
    ).strip()
    event = str(item.get("event", "") or "").strip()
    raw_content = str(item.get("index_content") or item.get("content") or "").strip()
    cleaned_text = _clean_external_text(raw_content)
    category = _infer_external_category(item, cleaned_text)
    heading = _extract_external_heading(cleaned_text)
    title = (
        _clean_external_title(item.get("task", ""), inferred_category=category)
        or _clean_external_title(item.get("title", ""), inferred_category=category)
        or _clean_external_title(heading, inferred_category=category)
        or str(item.get("event") or "未命名题解").strip()
    )
    difficulty = str(item.get("difficulty", "unknown") or "unknown").strip() or "unknown"
    year = _safe_int(item.get("year"), 0) or _extract_external_year(event, title, url)
    summary = _build_external_summary(cleaned_text, title=title, event=event)
    return {
        "record_id": str(item.get("writeup_id", "") or "").strip() or title,
        "created_at": _latest_mtime_iso([_EXTERNAL_INDEX_PATH, _EXTERNAL_RAW_PATH]) or "",
        "bucket": KNOWLEDGE_BUCKET_EXTERNAL,
        "title": title,
        "challenge_code": title,
        "event": event,
        "category": category,
        "difficulty": difficulty,
        "year": year,
        "summary": summary,
        "content": _collapse_text(cleaned_text or raw_content, limit=900),
        "url": url,
        "source": KNOWLEDGE_BUCKET_EXTERNAL,
        "source_type": "external_writeup",
        "outcome_type": "reference",
        "confidence": 1.0,
        "quality_score": 1.0,
        "verified_flags_count": 0,
        "discoveries_count": 0,
        "credentials_count": 0,
    }


def _external_category_stats(records: tuple[dict[str, Any], ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in records:
        category = str(_normalize_external_record(item).get("category", "unknown") or "unknown").strip() or "unknown"
        counts[category] = counts.get(category, 0) + 1
    return dict(sorted(counts.items(), key=lambda entry: (-entry[1], entry[0])))


def _browse_external_records(limit: int = 50, category: str | None = None) -> list[dict[str, Any]]:
    records, _ = _external_source_records()
    normalized_category = str(category or "").strip().lower()
    filtered = []
    for item in records:
        normalized = _normalize_external_record(item)
        if normalized_category and str(normalized.get("category", "unknown")).strip().lower() != normalized_category:
            continue
        filtered.append(normalized)
    filtered.sort(
        key=lambda item: (
            _safe_int(item.get("year"), 0),
            str(item.get("event", "")),
            str(item.get("title", "")),
        ),
        reverse=True,
    )
    return filtered[: max(1, int(limit or 50))]


def _search_external_fallback(query: str, top_k: int = 10, category: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    tokens = _tokenize(query)
    if not tokens:
        return "fallback-empty-query", []

    records, source_backend = _external_source_records()
    normalized_category = str(category or "").strip().lower()
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in records:
        normalized = _normalize_external_record(item)
        current_category = str(normalized.get("category", "unknown") or "unknown").strip().lower()
        if normalized_category and current_category != normalized_category:
            continue

        title_l = f"{normalized.get('title', '')} {normalized.get('challenge_code', '')}".lower()
        meta_l = f"{normalized.get('event', '')} {current_category} {normalized.get('difficulty', '')} {normalized.get('year', '')}".lower()
        content_l = f"{normalized.get('summary', '')}\n{normalized.get('content', '')}".lower()
        score = 0.0
        for token in tokens:
            if token in title_l:
                score += 10.0
            if token in meta_l:
                score += 4.0
            hits = content_l.count(token)
            if hits:
                score += min(hits, 6)
        if score <= 0:
            continue
        normalized["score"] = round(score, 4)
        scored.append((score, normalized))

    scored.sort(key=lambda item: item[0], reverse=True)
    return f"{source_backend}-keyword", [item for _, item in scored[: max(1, int(top_k or 10))]]


def _normalize_local_record(record: Any) -> dict[str, Any]:
    summary = record.summary or " | ".join(record.evidence[:2]) or "knowledge record"
    return {
        "record_id": record.record_id,
        "created_at": record.created_at,
        "bucket": record.bucket,
        "title": record.challenge_code or record.scope_key or record.record_id,
        "challenge_code": record.challenge_code,
        "event": record.zone,
        "category": record.category or "unknown",
        "difficulty": "",
        "year": 0,
        "summary": _collapse_text(summary, limit=320),
        "content": _collapse_text(summary, limit=600),
        "url": "",
        "source": record.bucket,
        "source_type": record.source_type,
        "outcome_type": record.outcome_type,
        "confidence": round(float(record.confidence or 0.0), 4),
        "quality_score": round(float(record.quality_score or 0.0), 4),
        "verified_flags_count": len(record.verified_flags),
        "discoveries_count": len(record.discoveries),
        "credentials_count": len(record.credentials),
    }


def _summarize_local_bucket(bucket: str) -> dict[str, Any]:
    if not _knowledge_available:
        return {
            "bucket": bucket,
            "display_name": bucket_display_name(bucket),
            "kind": "local",
            "total": 0,
            "success": 0,
            "failure": 0,
            "categories": {},
            "updated_at": None,
            "status": "unavailable",
        }

    records = get_knowledge_store().load_bucket(bucket)
    categories: dict[str, int] = {}
    for record in records:
        category = record.category or "unknown"
        categories[category] = categories.get(category, 0) + 1
    success_count = sum(1 for record in records if str(record.outcome_type or "").strip().lower() == "success")
    failure_count = max(0, len(records) - success_count)
    updated_at = max((record.created_at for record in records), default=None)
    return {
        "bucket": bucket,
        "display_name": bucket_display_name(bucket),
        "kind": "local",
        "total": len(records),
        "success": success_count,
        "failure": failure_count,
        "categories": dict(sorted(categories.items(), key=lambda entry: (-entry[1], entry[0]))),
        "updated_at": updated_at,
        "status": "ready" if records else "idle",
    }


def _summarize_external_bucket(status_payload: dict[str, Any]) -> dict[str, Any]:
    records, source_backend = _external_source_records()
    return {
        "bucket": KNOWLEDGE_BUCKET_EXTERNAL,
        "display_name": bucket_display_name(KNOWLEDGE_BUCKET_EXTERNAL),
        "kind": "external",
        "total": len(records),
        "success": len(records),
        "failure": 0,
        "categories": _external_category_stats(records),
        "updated_at": status_payload.get("updated_at"),
        "status": status_payload.get("status", "missing_assets"),
        "backend": (status_payload.get("health") or {}).get("backend") or source_backend,
        "service_enabled": status_payload.get("service_enabled", False),
        "browse_ready": status_payload.get("browse_ready", False),
    }


def _browse_local_bucket(bucket: str, limit: int = 50, category: str | None = None) -> list[dict[str, Any]]:
    if not _knowledge_available:
        return []
    normalized_category = str(category or "").strip().lower()
    records = get_knowledge_store().load_bucket(bucket)
    if normalized_category:
        records = [record for record in records if str(record.category or "unknown").strip().lower() == normalized_category]
    records.sort(key=lambda record: record.created_at, reverse=True)
    return [_normalize_local_record(record) for record in records[: max(1, int(limit or 50))]]


def _search_local_bucket(bucket: str, query: str, top_k: int = 10, category: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    if knowledge_service_enabled():
        try:
            response = search_knowledge_service(
                query,
                top_k=max(1, int(top_k or 10)),
                bucket=bucket,
                category=category or None,
                allow_startup=True,
            )
            results = []
            for item in list(response.get("results", []) or []):
                summary = str(item.get("content", "") or "").strip()
                results.append(
                    {
                        "record_id": str(item.get("record_id", "") or item.get("challenge_code", "") or ""),
                        "created_at": "",
                        "bucket": bucket,
                        "title": str(item.get("challenge_code", "") or item.get("scope_key", "") or "知识记录"),
                        "challenge_code": str(item.get("challenge_code", "") or ""),
                        "event": str(item.get("zone", "") or ""),
                        "category": str(item.get("category", "unknown") or "unknown"),
                        "difficulty": "",
                        "year": 0,
                        "summary": _collapse_text(summary, limit=320),
                        "content": _collapse_text(summary, limit=600),
                        "url": "",
                        "source": str(item.get("source", bucket) or bucket),
                        "source_type": str(item.get("source_type", "") or ""),
                        "outcome_type": str(item.get("outcome_type", "") or ""),
                        "confidence": float(item.get("confidence", 0.0) or 0.0),
                        "quality_score": float(item.get("quality_score", 0.0) or 0.0),
                        "verified_flags_count": 0,
                        "discoveries_count": 0,
                        "credentials_count": 0,
                    }
                )
            if results:
                return "knowledge-service", results
        except Exception as exc:
            logger.warning("[Web] Local knowledge service search failed, fallback to JSONL: %s", exc)

    if not _knowledge_available:
        return "unavailable", []

    hits = search_knowledge_records(
        query,
        bucket=bucket,
        top_k=max(1, int(top_k or 10)),
        category=category or None,
        include_low_confidence=True,
    )
    return "jsonl-fallback", [_normalize_local_record(hit.record) for hit in hits]


def _search_external_bucket(query: str, top_k: int = 10, category: str | None = None) -> tuple[str, list[dict[str, Any]]]:
    if knowledge_service_enabled():
        try:
            response = search_knowledge_service(
                query,
                top_k=max(1, int(top_k or 10)),
                bucket=KNOWLEDGE_BUCKET_EXTERNAL,
                category=category or None,
                allow_startup=True,
            )
            results = []
            lookup = _external_record_lookup()
            for item in list(response.get("results", []) or []):
                record_id = str(item.get("writeup_id", "") or "").strip()
                base_item = lookup.get(record_id) or dict(item)
                if item.get("content"):
                    base_item = {
                        **base_item,
                        "index_content": item.get("content"),
                        "content": item.get("content"),
                        "task": item.get("task") or base_item.get("task", ""),
                        "event": item.get("event") or base_item.get("event", ""),
                        "category": item.get("category") or base_item.get("category", "unknown"),
                        "difficulty": item.get("difficulty") or base_item.get("difficulty", "unknown"),
                        "year": item.get("year") or base_item.get("year", 0),
                        "url": item.get("url") or base_item.get("url", ""),
                    }
                normalized = _normalize_external_record(base_item)
                normalized["record_id"] = record_id or str(item.get("chunk_id", "") or normalized["record_id"])
                normalized["quality_score"] = float(item.get("score", 0.0) or 0.0)
                results.append(normalized)
            if results:
                return "knowledge-service", results
        except Exception as exc:
            logger.warning("[Web] External knowledge service search failed, fallback to snapshot: %s", exc)

    return _search_external_fallback(query, top_k=top_k, category=category)


# ═══════════════════════════════════════════════════════
# 4. HTTP 路由
# ═══════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(static_dir, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/knowledge", response_class=HTMLResponse)
async def knowledge_page():
    html_path = os.path.join(static_dir, "knowledge.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ─── 状态 ───

@app.get("/api/state")
async def get_state():
    return {
        **_agent_state,
        "zones": _zones,
        "tasks": [t.to_dict() for t in _tasks.values()],
    }


@app.get("/api/logs")
async def get_logs(limit: int = 200):
    return list(_log_buffer)[-limit:]


# ─── 知识库 ───

@app.get("/api/knowledge")
async def get_knowledge_stats():
    """获取知识库总览（主战场记忆 / 论坛记忆 / 各大 CTF WP）。"""
    try:
        external_status = _build_external_status()
        buckets_info = [
            _summarize_local_bucket(KNOWLEDGE_BUCKET_MAIN),
            _summarize_local_bucket(KNOWLEDGE_BUCKET_FORUM),
            _summarize_external_bucket(external_status),
        ]
        return {
            "buckets": buckets_info,
            "service": external_status,
        }
    except Exception as e:
        logger.error(f"[Web] Failed to load knowledge stats: {e}")
        return {"error": str(e), "buckets": [], "service": _build_external_status()}


@app.get("/api/knowledge/status")
async def get_knowledge_status():
    """获取外部知识服务与资产状态。"""
    return _build_external_status()


@app.get("/api/knowledge/search")
async def search_knowledge(
    bucket: str,
    q: str,
    top_k: int = 10,
    category: str = "",
):
    """统一知识搜索入口。"""
    resolved_bucket = _resolve_bucket(bucket)
    query = str(q or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="q 不能为空")

    try:
        if resolved_bucket == KNOWLEDGE_BUCKET_EXTERNAL:
            backend, results = _search_external_bucket(
                query,
                top_k=max(1, min(int(top_k or 10), 20)),
                category=category or None,
            )
        else:
            backend, results = _search_local_bucket(
                resolved_bucket,
                query,
                top_k=max(1, min(int(top_k or 10), 20)),
                category=category or None,
            )

        return {
            "bucket": resolved_bucket,
            "display_name": bucket_display_name(resolved_bucket),
            "query": query,
            "backend": backend,
            "results": results,
            "total": len(results),
        }
    except Exception as e:
        logger.error("[Web] Failed to search knowledge: %s", e)
        return {
            "bucket": resolved_bucket,
            "display_name": bucket_display_name(resolved_bucket),
            "query": query,
            "error": str(e),
            "results": [],
            "total": 0,
        }


@app.get("/api/knowledge/{bucket}")
async def get_knowledge_records(bucket: str, limit: int = 50, category: str = ""):
    """获取指定 bucket 的浏览记录。"""
    resolved_bucket = _resolve_bucket(bucket)
    try:
        if resolved_bucket == KNOWLEDGE_BUCKET_EXTERNAL:
            records = _browse_external_records(limit=limit, category=category or None)
            backend = "index_snapshot" if _EXTERNAL_INDEX_PATH.exists() else "raw_archive"
        else:
            records = _browse_local_bucket(resolved_bucket, limit=limit, category=category or None)
            backend = "knowledge_store"

        return {
            "bucket": resolved_bucket,
            "display_name": bucket_display_name(resolved_bucket),
            "backend": backend,
            "records": records,
        }
    except Exception as e:
        logger.error(f"[Web] Failed to load knowledge records: {e}")
        return {
            "bucket": resolved_bucket,
            "display_name": bucket_display_name(resolved_bucket),
            "error": str(e),
            "records": [],
        }


# ─── 任务管理 ───

class CreateTaskRequest(BaseModel):
    challenge_code: str
    target: str = ""
    difficulty: str = ""
    points: int = 0
    zone: str = ""


@app.post("/api/tasks")
async def create_task(req: CreateTaskRequest):
    """创建并启动新任务"""
    task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    rec = TaskRecord(
        task_id=task_id,
        challenge_code=req.challenge_code,
        target=req.target,
        difficulty=req.difficulty,
        points=req.points,
        zone=req.zone,
    )
    _tasks[task_id] = rec

    push_log("info", f"Task created: {req.challenge_code} → {req.target}", "web")
    _broadcast({"type": "task_update", "data": rec.to_dict()})

    # 调用回调启动任务
    if _on_start_task:
        try:
            await _on_start_task(task_id, req.challenge_code)
        except Exception as e:
            push_log("error", f"Start task failed: {e}", "web")
    else:
        push_log("warn", "Task callback not registered; task remains pending", "web")

    return {"ok": True, "task_id": task_id}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """暂停任务"""
    if task_id not in _tasks:
        raise HTTPException(404, "Task not found")

    rec = _tasks[task_id]
    if rec.status != TaskStatus.RUNNING:
        raise HTTPException(400, f"Cannot pause task in {rec.status.value} status")

    rec.status = TaskStatus.PAUSED
    _broadcast({"type": "task_update", "data": rec.to_dict()})
    push_log("warn", f"Task paused: {rec.challenge_code}", "web")

    if _on_pause_task:
        await _on_pause_task(task_id)

    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    """恢复任务"""
    if task_id not in _tasks:
        raise HTTPException(404, "Task not found")

    rec = _tasks[task_id]
    if rec.status != TaskStatus.PAUSED:
        raise HTTPException(400, f"Cannot resume task in {rec.status.value} status")

    rec.status = TaskStatus.RUNNING
    _broadcast({"type": "task_update", "data": rec.to_dict()})
    push_log("info", f"Task resumed: {rec.challenge_code}", "web")

    if _on_resume_task:
        await _on_resume_task(task_id)

    return {"ok": True}


@app.post("/api/tasks/{task_id}/abort")
async def abort_task(task_id: str):
    """中止任务"""
    if task_id not in _tasks:
        raise HTTPException(404, "Task not found")

    rec = _tasks[task_id]
    if rec.status in (TaskStatus.COMPLETED, TaskStatus.ABORTED):
        raise HTTPException(400, f"Task already {rec.status.value}")

    rec.status = TaskStatus.ABORTED
    rec.finished_at = datetime.now()
    _broadcast({"type": "task_update", "data": rec.to_dict()})
    push_log("error", f"Task aborted: {rec.challenge_code}", "web")

    if _on_abort_task:
        await _on_abort_task(task_id)

    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除已完成/中止的任务"""
    if task_id not in _tasks:
        raise HTTPException(404, "Task not found")

    rec = _tasks[task_id]
    if rec.status in (TaskStatus.RUNNING, TaskStatus.PAUSED):
        raise HTTPException(400, "Cannot delete active task. Abort first.")

    del _tasks[task_id]
    _broadcast({"type": "task_deleted", "data": {"task_id": task_id}})
    return {"ok": True}


# ─── 全局控制 ───

@app.post("/api/agent/start")
async def agent_start():
    """启动全部"""
    if _on_start_all:
        await _on_start_all()
    update_agent_state({"status": "running", "start_time": int(time.time())})
    push_log("info", "Agent started", "web")
    return {"ok": True}


@app.post("/api/agent/pause")
async def agent_pause():
    """暂停全部"""
    if _on_pause_all:
        await _on_pause_all()
    update_agent_state({"status": "paused"})
    push_log("warn", "Agent paused", "web")
    return {"ok": True}


# ─── SSE ───

@app.get("/api/events")
async def sse_events(request: Request):
    queue = asyncio.Queue(maxsize=100)
    _sse_clients.append(queue)

    async def event_generator():
        try:
            # 初始全量推送
            init = {
                "type": "init",
                "data": {
                    **_agent_state,
                    "zones": _zones,
                    "tasks": [t.to_dict() for t in _tasks.values()],
                },
            }
            yield f"data: {json.dumps(init, ensure_ascii=False)}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'ping'})}\n\n"
        finally:
            if queue in _sse_clients:
                _sse_clients.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ═══════════════════════════════════════════════════════
# 4. 服务器启动
# ═══════════════════════════════════════════════════════

async def start_web_server(host: str = "0.0.0.0", port: int = 7890):
    import uvicorn
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    logger.info(f"[Web] Dashboard: http://localhost:{port}")
    return task
