"""
Level2 手工题与 CVE 映射
=======================

集中维护 Web/Dashboard 手工任务的 task_id、CVE 编号和本地 PoC 名称之间的关系。
"""

from __future__ import annotations

from typing import Any


LEVEL2_MANUAL_TASK_HINTS: dict[str, dict[str, str]] = {
    "3ZdueytTkJeRy2wiYmJiqwrzP2XiNqs": {
        "cve": "cve-2024-1561",
        "poc_name": "gradio",
        "product": "Gradio",
        "zone": "z2",
    },
    "FQe9I9sG0rH3oVTSYtvShoYBWhkuYEQX": {
        "cve": "cve-2025-67303",
        "poc_name": "comfyui-manager",
        "product": "ComfyUI Manager",
        "zone": "z2",
    },
    "p71MyGzdIAR13xvgr8SePV4UZwa6p": {
        "cve": "cve-2024-39907",
        "poc_name": "1panel",
        "product": "1Panel",
        "zone": "z2",
    },
}

LEVEL2_CVE_TO_POC: dict[str, str] = {
    "cve-2024-1561": "gradio",
    "cve-2025-67303": "comfyui-manager",
    "cve-2024-39907": "1panel",
}

LEVEL2_TITLE_HINTS: dict[str, dict[str, str]] = {
    "算法效果展示平台": {
        "cve": "cve-2024-1561",
        "poc_name": "gradio",
        "product": "Gradio",
        "zone": "z2",
    },
    "智算模型托管引擎": {
        "cve": "cve-2025-67303",
        "poc_name": "comfyui-manager",
        "product": "ComfyUI Manager",
        "zone": "z2",
    },
    "运维集中调度台": {
        "cve": "cve-2024-39907",
        "poc_name": "1panel",
        "product": "1Panel",
        "zone": "z2",
    },
}

LEVEL2_POC_TO_PRODUCT: dict[str, str] = {
    "gradio": "Gradio",
    "comfyui-manager": "ComfyUI Manager",
    "1panel": "1Panel",
}


def normalize_cve_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return normalized.replace("_", "-")


def resolve_level2_task_hint(task_id: Any = None, challenge_text: str = "") -> dict[str, str]:
    """
    根据 task_id 或题面文本解析 Level2 预设线索。

    返回字段:
    - cve
    - poc_name
    - product
    - zone
    - level
    """

    hint: dict[str, str] = {}

    normalized_task_id = str(task_id or "").strip()
    if normalized_task_id and normalized_task_id in LEVEL2_MANUAL_TASK_HINTS:
        hint.update(LEVEL2_MANUAL_TASK_HINTS[normalized_task_id])

    lowered = str(challenge_text or "").strip().lower()

    for title, title_hint in LEVEL2_TITLE_HINTS.items():
        if title.lower() in lowered:
            for key, value in title_hint.items():
                hint.setdefault(key, value)

    for cve_id, poc_name in LEVEL2_CVE_TO_POC.items():
        if cve_id in lowered:
            hint.setdefault("cve", cve_id)
            hint.setdefault("poc_name", poc_name)

    poc_name = str(hint.get("poc_name", "") or "").strip().lower()
    if poc_name:
        hint.setdefault("product", LEVEL2_POC_TO_PRODUCT.get(poc_name, poc_name))
        hint.setdefault("zone", "z2")
        hint.setdefault("level", "2")

    return hint
