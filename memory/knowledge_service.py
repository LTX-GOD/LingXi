"""
统一知识服务管理
================
将知识服务作为独立 HTTP 进程常驻运行，避免在主进程里直接初始化
向量库与检索运行时，降低并发题目时的连接抖动与 keepalive 噪音。
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from runtime_env import get_project_python
from memory.knowledge_store import DEFAULT_KNOWLEDGE_DIR

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_CTF_WRITEUPS_ROOT = _ROOT / "ctf_writeups_kb"
_LEGACY_WRITEUPS_ROOT = _ROOT / "tou"
_ACTIVE_WRITEUPS_ROOT = (
    _CTF_WRITEUPS_ROOT
    if _CTF_WRITEUPS_ROOT.exists()
    else _LEGACY_WRITEUPS_ROOT
)
_ACTIVE_WRITEUPS_SRC = _ACTIVE_WRITEUPS_ROOT / "src"
_STATE_DIR = _ROOT / "wp"
_STATE_FILE = _STATE_DIR / "knowledge_service.json"
_LEGACY_STATE_FILE = _STATE_DIR / "tou_service.json"
_LOG_FILE = _STATE_DIR / "knowledge_service.log"
_LEGACY_LOG_FILE = _STATE_DIR / "tou_service.log"


def _env(name: str, default: str, *, legacy: str | None = None) -> str:
    for key in (name, legacy):
        if not key:
            continue
        value = os.getenv(key)
        if value is not None:
            stripped = value.strip()
            if stripped:
                return stripped
    return default


def _env_bool(name: str, default: bool, *, legacy: str | None = None) -> bool:
    return _env(name, "true" if default else "false", legacy=legacy).lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _env_float(name: str, default: float, *, legacy: str | None = None) -> float:
    try:
        return float(_env(name, str(default), legacy=legacy))
    except ValueError:
        return default


def _env_int(name: str, default: int, *, legacy: str | None = None) -> int:
    try:
        return int(_env(name, str(default), legacy=legacy))
    except ValueError:
        return default


_SERVICE_HOST = _env(
    "KNOWLEDGE_SERVICE_HOST",
    "127.0.0.1",
    legacy="TOU_SERVICE_HOST",
)
_SERVICE_PORT = _env_int(
    "KNOWLEDGE_SERVICE_PORT",
    8791,
    legacy="TOU_SERVICE_PORT",
)
_SERVICE_ENABLED = _env_bool(
    "KNOWLEDGE_SERVICE_ENABLED",
    True,
    legacy="TOU_SERVICE_ENABLED",
)
_HEALTH_TIMEOUT = max(
    0.5,
    _env_float(
        "KNOWLEDGE_SERVICE_HEALTH_TIMEOUT",
        2.5,
        legacy="TOU_SERVICE_HEALTH_TIMEOUT",
    ),
)
_STARTUP_TIMEOUT = max(
    2.0,
    _env_float(
        "KNOWLEDGE_SERVICE_STARTUP_TIMEOUT",
        15.0,
        legacy="TOU_SERVICE_STARTUP_TIMEOUT",
    ),
)
_SEARCH_TIMEOUT = max(
    1.0,
    _env_float(
        "KNOWLEDGE_SERVICE_SEARCH_TIMEOUT",
        8.0,
        legacy="TOU_SERVICE_SEARCH_TIMEOUT",
    ),
)
_INGEST_TIMEOUT = max(
    1.0,
    _env_float(
        "KNOWLEDGE_SERVICE_INGEST_TIMEOUT",
        8.0,
        legacy="TOU_SERVICE_INGEST_TIMEOUT",
    ),
)
_UVICORN_LOG_LEVEL = _env(
    "KNOWLEDGE_SERVICE_LOG_LEVEL",
    "warning",
    legacy="TOU_SERVICE_LOG_LEVEL",
)


def knowledge_service_enabled() -> bool:
    return _SERVICE_ENABLED and _ACTIVE_WRITEUPS_ROOT.exists() and _ACTIVE_WRITEUPS_SRC.exists()


def get_knowledge_service_base_url() -> str:
    explicit = _env(
        "KNOWLEDGE_SERVICE_BASE_URL",
        "",
        legacy="TOU_SERVICE_BASE_URL",
    ).rstrip("/")
    if explicit:
        return explicit
    return f"http://{_SERVICE_HOST}:{_SERVICE_PORT}"


def _read_state() -> dict[str, Any]:
    for path in (_STATE_FILE, _LEGACY_STATE_FILE):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            logger.warning("[KnowledgeService] 读取服务状态失败: %s", exc)
            break
    return {}


def _write_state(payload: dict[str, Any]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _pid_is_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_pid(pid: int | None, *, timeout: float = 3.0) -> None:
    if not _pid_is_alive(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return


def _healthcheck(base_url: str | None = None, timeout: float | None = None) -> dict[str, Any] | None:
    url = f"{(base_url or get_knowledge_service_base_url()).rstrip('/')}/health"
    try:
        with httpx.Client(timeout=timeout or _HEALTH_TIMEOUT) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not isinstance(body, dict):
            return None
        status = str(body.get("status", "") or "").lower()
        if status not in {"ok", "degraded"}:
            return None
        return body
    except Exception:
        return None


def _build_service_env() -> dict[str, str]:
    env = os.environ.copy()
    pythonpath_parts: list[str] = [str(_ACTIVE_WRITEUPS_SRC), str(_ROOT)]
    existing = str(env.get("PYTHONPATH", "") or "").strip()
    if existing:
        pythonpath_parts.append(existing)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in pythonpath_parts:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    env["PYTHONPATH"] = os.pathsep.join(deduped)
    env.setdefault("API_HOST", _SERVICE_HOST)
    env.setdefault("API_PORT", str(_SERVICE_PORT))
    env.setdefault("LING_XI_PYTHON", get_project_python())
    env["LING_XI_KNOWLEDGE_DIR"] = str(DEFAULT_KNOWLEDGE_DIR)
    return env


def _spawn_service_process() -> subprocess.Popen[Any]:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        get_project_python(),
        "-m",
        "uvicorn",
        "ctf_kb.api.app:app",
        "--host",
        _SERVICE_HOST,
        "--port",
        str(_SERVICE_PORT),
        "--log-level",
        _UVICORN_LOG_LEVEL,
    ]
    log_handle = _LOG_FILE.open("ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(_ACTIVE_WRITEUPS_ROOT),
            env=_build_service_env(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        log_handle.close()
    return proc


def ensure_knowledge_service_running(
    *,
    force_restart: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    if not knowledge_service_enabled():
        return {
            "status": "disabled",
            "base_url": get_knowledge_service_base_url(),
        }

    base_url = get_knowledge_service_base_url()
    effective_timeout = max(2.0, float(timeout or _STARTUP_TIMEOUT))

    if not force_restart:
        health = _healthcheck(base_url=base_url)
        if health is not None:
            state = _read_state()
            info = {
                "status": "running",
                "base_url": base_url,
                "pid": int(state.get("pid", 0) or 0) or None,
                "chunks": health.get("chunks"),
                "collection": health.get("collection"),
            }
            _write_state(
                {
                    **state,
                    "base_url": base_url,
                    "last_ok_at": time.time(),
                    "chunks": health.get("chunks"),
                    "collection": health.get("collection"),
                }
            )
            return info

    state = _read_state()
    pid = int(state.get("pid", 0) or 0) or None
    if _pid_is_alive(pid):
        logger.warning(
            "[KnowledgeService] 发现旧服务进程但健康检查失败，尝试重启: pid=%s",
            pid,
        )
        _stop_pid(pid)

    proc = _spawn_service_process()
    deadline = time.time() + effective_timeout
    last_error = "knowledge service did not become healthy"
    while time.time() < deadline:
        if proc.poll() is not None:
            last_error = f"knowledge service exited early with code {proc.returncode}"
            break
        health = _healthcheck(base_url=base_url, timeout=min(_HEALTH_TIMEOUT, 1.5))
        if health is not None:
            payload = {
                "pid": proc.pid,
                "base_url": base_url,
                "started_at": time.time(),
                "last_ok_at": time.time(),
                "log_file": str(_LOG_FILE),
                "chunks": health.get("chunks"),
                "collection": health.get("collection"),
            }
            _write_state(payload)
            return {
                "status": "running",
                "base_url": base_url,
                "pid": proc.pid,
                "chunks": health.get("chunks"),
                "collection": health.get("collection"),
            }
        time.sleep(0.25)

    raise RuntimeError(last_error)


def search_knowledge_service(
    query: str,
    *,
    top_k: int,
    event: str | None = None,
    task: str | None = None,
    category: str | None = None,
    difficulty: str | None = None,
    year: int | None = None,
    bucket: str | None = None,
    source_type: str | None = None,
    outcome_type: str | None = None,
    allow_startup: bool = True,
) -> dict[str, Any]:
    if not knowledge_service_enabled():
        return {"query": query, "total": 0, "results": [], "backend": "disabled"}

    base_url = get_knowledge_service_base_url().rstrip("/")

    def _request() -> dict[str, Any]:
        # 构建参数字典，只包含非空值
        params = {
            "q": query,
            "top_k": max(1, int(top_k)),
        }
        # 只添加非空的可选参数
        if event:
            params["event"] = event
        if task:
            params["task"] = task
        if category:
            params["category"] = category
        if difficulty:
            params["difficulty"] = difficulty
        if year is not None:
            params["year"] = year
        if bucket:
            params["bucket"] = bucket
        if source_type:
            params["source_type"] = source_type
        if outcome_type:
            params["outcome_type"] = outcome_type

        with httpx.Client(timeout=_SEARCH_TIMEOUT) as client:
            resp = client.get(f"{base_url}/search", params=params)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("knowledge service returned invalid JSON")
        return body

    try:
        return _request()
    except Exception as first_exc:
        if not allow_startup:
            raise
        logger.warning(
            "[KnowledgeService] 查询失败，尝试拉起服务后重试: %s",
            first_exc,
        )
        ensure_knowledge_service_running(timeout=_STARTUP_TIMEOUT)
        return _request()


def ingest_knowledge_record(
    record: dict[str, Any],
    *,
    bucket: str | None = None,
) -> dict[str, Any]:
    if not knowledge_service_enabled():
        return {"status": "disabled", "bucket": bucket or record.get("bucket", "")}

    base_url = get_knowledge_service_base_url().rstrip("/")
    payload = {
        "bucket": bucket or record.get("bucket", ""),
        "record": record,
    }

    def _request() -> dict[str, Any]:
        with httpx.Client(timeout=_INGEST_TIMEOUT) as client:
            resp = client.post(f"{base_url}/experience/ingest", json=payload)
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("knowledge service returned invalid JSON")
        return body

    try:
        return _request()
    except Exception as first_exc:
        logger.warning(
            "[KnowledgeService] 写入失败，尝试拉起服务后重试: %s",
            first_exc,
        )
        ensure_knowledge_service_running(timeout=_STARTUP_TIMEOUT)
        return _request()
