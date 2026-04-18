"""
共享的 Kali Docker 容器名解析。
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

try:
    from dotenv import load_dotenv
except ImportError:

    def load_dotenv(*args, **kwargs):
        return False


load_dotenv(override=False)

logger = logging.getLogger(__name__)

DEFAULT_KALI_CONTAINER_NAME = "kali-pentest"


class KaliContainerResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class KaliContainerResolution:
    name: str
    source: str
    requested: str = ""


def list_running_docker_container_names(log: Optional[logging.Logger] = None) -> list[str]:
    active_logger = log or logger
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except Exception as exc:
        active_logger.warning("[KaliContainer] 自动探测 Kali 容器失败: %s", exc)
        return []

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        active_logger.warning(
            "[KaliContainer] docker ps 执行失败，无法自动探测容器: %s",
            detail or result.returncode,
        )
        return []

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def discover_running_kali_containers(
    container_names: Optional[Sequence[str]] = None,
) -> list[str]:
    names = list(container_names) if container_names is not None else list_running_docker_container_names()
    candidates: list[str] = []
    for name in names:
        lowered = name.lower()
        if name == DEFAULT_KALI_CONTAINER_NAME or lowered.startswith("kali") or "kali" in lowered:
            candidates.append(name)
    return candidates


def _resolve_requested_name(
    requested: str,
    *,
    source: str,
    strict: bool,
    running: Sequence[str],
    log: Optional[logging.Logger],
) -> KaliContainerResolution:
    active_logger = log or logger
    if running and requested not in running:
        discovered = discover_running_kali_containers(running)
        if len(discovered) == 1:
            active_logger.warning(
                "[KaliContainer] %s=%s 未运行，自动回退到唯一可用的 Kali 容器: %s",
                "container" if source == "explicit" else "DOCKER_CONTAINER_NAME",
                requested,
                discovered[0],
            )
            return KaliContainerResolution(
                name=discovered[0],
                source="fallback_running",
                requested=requested,
            )
        if strict and len(discovered) > 1:
            joined = ", ".join(discovered)
            if source == "explicit":
                raise KaliContainerResolutionError(
                    f"指定容器 {requested} 未运行，且检测到多个运行中的 Kali 容器: {joined}；请显式修正配置"
                )
            raise KaliContainerResolutionError(
                f"DOCKER_CONTAINER_NAME={requested} 未运行，且检测到多个运行中的 Kali 容器: {joined}；请显式修正配置"
            )
    return KaliContainerResolution(name=requested, source=source, requested=requested)


def resolve_kali_container(
    container: Optional[str] = None,
    *,
    strict: bool = False,
    log: Optional[logging.Logger] = None,
) -> KaliContainerResolution:
    requested = str(container or "").strip()
    running = list_running_docker_container_names(log=log)
    if requested:
        return _resolve_requested_name(
            requested,
            source="explicit",
            strict=strict,
            running=running,
            log=log,
        )

    env_name = (os.getenv("DOCKER_CONTAINER_NAME") or "").strip()
    if env_name:
        return _resolve_requested_name(
            env_name,
            source="env",
            strict=strict,
            running=running,
            log=log,
        )

    discovered = discover_running_kali_containers(running)
    if len(discovered) == 1:
        (log or logger).info(
            "[KaliContainer] 未配置 DOCKER_CONTAINER_NAME，自动选择运行中的 Kali 容器: %s",
            discovered[0],
        )
        return KaliContainerResolution(
            name=discovered[0],
            source="discovered",
        )
    if len(discovered) > 1 and strict:
        joined = ", ".join(discovered)
        raise KaliContainerResolutionError(
            "未配置 DOCKER_CONTAINER_NAME，且检测到多个运行中的 Kali 容器: "
            f"{joined}；请显式设置目标容器名"
        )
    return KaliContainerResolution(
        name=DEFAULT_KALI_CONTAINER_NAME,
        source="default",
    )


def get_kali_container_name(
    container: Optional[str] = None,
    *,
    strict: bool = False,
    log: Optional[logging.Logger] = None,
) -> str:
    return resolve_kali_container(container, strict=strict, log=log).name
