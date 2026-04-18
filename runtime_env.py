"""
统一运行时环境解析
==================
确保宿主进程与本地 Python 子进程统一使用当前项目目录下的 `.venv`。
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_project_root() -> Path:
    return Path(__file__).resolve().parent


def _candidate_project_pythons() -> list[Path]:
    root = get_project_root()
    candidates = [
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    ]
    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate)
        if text in seen:
            continue
        seen.add(text)
        deduped.append(candidate)
    return deduped


@lru_cache(maxsize=1)
def get_project_python() -> str:
    explicit = str(os.getenv("LING_XI_PYTHON", "") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit

    for candidate in _candidate_project_pythons():
        if candidate.exists():
            return str(candidate)

    return sys.executable or "python3"


def ensure_project_venv() -> None:
    """
    若当前不是使用项目 `.venv` 运行，则自动 re-exec 到项目解释器。
    """
    desired = get_project_python()
    if not desired or not Path(desired).exists():
        return

    desired_prefix = str(Path(desired).resolve().parent.parent)
    current_prefix = str(Path(sys.prefix).resolve()) if sys.prefix else ""
    if current_prefix == desired_prefix:
        os.environ.setdefault("LING_XI_PYTHON", desired)
        return

    current = sys.executable or ""
    if current == desired:
        os.environ.setdefault("LING_XI_PYTHON", desired)
        return

    os.environ["LING_XI_PYTHON"] = desired
    argv = [desired]
    if len(sys.orig_argv) > 1:
        argv.extend(sys.orig_argv[1:])
    elif sys.argv and sys.argv[0]:
        argv.extend(sys.argv)
    os.execv(desired, argv)
