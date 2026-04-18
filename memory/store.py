"""
解题记忆系统
============
跨题目的经验存储，帮助 Agent 从历史中学习。

功能:
- 记录每道题的攻击过程和结果
- 相同赛区的题目可以共享有用信息
- 发现的通用凭据/路径/模式可复用
"""

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from memory.knowledge_store import (
    KNOWLEDGE_BUCKET_FORUM,
    KNOWLEDGE_BUCKET_MAIN,
    bucket_for_challenge,
    normalize_bucket,
    search_local_knowledge_context,
)

logger = logging.getLogger(__name__)


class MemoryStore:
    """简单的 JSON 文件记忆存储"""

    def __init__(self, path: str = "data/memory.json", wp_dir: str = "wp"):
        self._lock = threading.RLock()
        self.path = path
        self.wp_dir = wp_dir
        self._memories: Dict[str, List[Dict]] = {}
        self._discoveries: Dict[str, List[str]] = {}  # scope/zone → [发现]
        self._credentials: List[Dict] = []  # 通用凭据
        self._load()

    @staticmethod
    def _shared_bucket_name(bucket: Optional[str] = None) -> str:
        normalized = normalize_bucket(bucket)
        if normalized == KNOWLEDGE_BUCKET_FORUM:
            return KNOWLEDGE_BUCKET_FORUM
        return KNOWLEDGE_BUCKET_MAIN

    def _discovery_storage_key(self, zone: str, bucket: Optional[str] = None) -> str:
        normalized_zone = str(zone or "").strip()
        if not normalized_zone:
            return ""
        bucket_name = self._shared_bucket_name(bucket)
        if bucket_name == KNOWLEDGE_BUCKET_FORUM:
            return f"forum::{normalized_zone}"
        return f"main::{normalized_zone}"

    def _discovery_lookup_keys(self, zone: str, bucket: Optional[str] = None) -> List[str]:
        normalized_zone = str(zone or "").strip()
        if not normalized_zone:
            return []
        bucket_name = self._shared_bucket_name(bucket)
        keys = [self._discovery_storage_key(normalized_zone, bucket=bucket_name)]
        if bucket_name != KNOWLEDGE_BUCKET_FORUM:
            keys.append(normalized_zone)  # 兼容历史未分桶数据
        deduped: List[str] = []
        seen: set[str] = set()
        for item in keys:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    def _load(self):
        with self._lock:
            self._load_unlocked()

    def _load_unlocked(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._memories = data.get("memories", {})
                    self._discoveries = data.get("discoveries", {})
                    self._credentials = data.get("credentials", [])
            except Exception as e:
                logger.warning(f"[Memory] 加载失败: {e}")
                self._memories = {}
                self._discoveries = {}
                self._credentials = []

    def _save(self):
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data = {
            "memories": self._memories,
            "discoveries": self._discoveries,
            "credentials": self._credentials,
        }
        tmp_dir = os.path.dirname(self.path) or "."
        fd, tmp_path = tempfile.mkstemp(prefix=".memory_", suffix=".json", dir=tmp_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except OSError:
                pass

    def record_attempt(
        self,
        challenge_code: str,
        result: Dict[str, Any],
        scope_key: Optional[str] = None,
    ):
        """记录一次解题尝试"""
        memory_key = scope_key or challenge_code
        with self._lock:
            if memory_key not in self._memories:
                self._memories[memory_key] = []

            self._memories[memory_key].append(
                {
                    "timestamp": datetime.now().isoformat(),
                    "challenge_code": challenge_code,
                    "success": result.get("success", False),
                    "flag": result.get("flag", ""),
                    "attempts": result.get("attempts", 0),
                    "elapsed": result.get("elapsed", 0),
                    "error": result.get("error", ""),
                }
            )
            self._save_unlocked()

    def _build_wp_slug(
        self,
        challenge_code: str,
        scope_key: Optional[str] = None,
    ) -> str:
        raw = str(scope_key or challenge_code or "challenge").strip()
        slug = re.sub(r"[^0-9A-Za-z._-]+", "_", raw).strip("._")
        return slug or "challenge"

    def _get_wp_jsonl_path(
        self,
        challenge_code: str,
        scope_key: Optional[str] = None,
    ) -> str:
        slug = self._build_wp_slug(challenge_code, scope_key=scope_key)
        return os.path.join(self.wp_dir, f"{slug}.jsonl")

    def _get_wp_markdown_path(
        self,
        challenge_code: str,
        scope_key: Optional[str] = None,
    ) -> str:
        slug = self._build_wp_slug(challenge_code, scope_key=scope_key)
        return os.path.join(self.wp_dir, f"{slug}.md")

    def _read_wp_records_unlocked(self, path: str) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not os.path.exists(path):
            return records
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        records.append(payload)
        except Exception as e:
            logger.warning(f"[Memory] 读取 WP 记录失败: {path} | {e}")
        return records

    @staticmethod
    def _clip_text(value: Any, limit: int = 240) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    def _render_wp_markdown(
        self,
        record: Dict[str, Any],
        recent_records: List[Dict[str, Any]],
    ) -> str:
        latest = record
        title = latest.get("display_code") or latest.get("challenge_code") or "unknown"
        lines = [
            f"# WP - {title}",
            "",
            f"- 更新时间: {latest.get('timestamp', '')}",
            f"- Scope: `{latest.get('scope_key', '')}`",
            f"- Code: `{latest.get('challenge_code', '')}`",
            f"- Zone: `{latest.get('zone', '')}`",
            f"- Target: `{latest.get('target', '')}`",
            f"- Difficulty: `{latest.get('difficulty', '')}`",
            f"- Points: `{latest.get('total_score', '')}`",
            f"- Success: `{latest.get('success', False)}`",
            f"- Attempts: `{latest.get('attempts', 0)}`",
            f"- Elapsed: `{latest.get('elapsed', 0)}`",
        ]
        if latest.get("flag"):
            lines.append(f"- Flag: `{latest.get('flag')}`")
        if latest.get("error"):
            lines.append(f"- Error: `{self._clip_text(latest.get('error'), 400)}`")
        if latest.get("strategy_description"):
            lines.append(
                f"- Strategy: `{self._clip_text(latest.get('strategy_description'), 300)}`"
            )
        if latest.get("final_strategy"):
            lines.append(
                f"- Final Strategy: `{self._clip_text(latest.get('final_strategy'), 300)}`"
            )
        if latest.get("thought_summary"):
            lines.append(
                f"- Thought Summary: `{self._clip_text(latest.get('thought_summary'), 300)}`"
            )
        lines.append(f"- Advisor Calls: `{int(latest.get('advisor_call_count', 0) or 0)}`")
        lines.append(f"- Knowledge Calls: `{int(latest.get('knowledge_call_count', 0) or 0)}`")
        if latest.get("advisor_summary"):
            lines.append(
                f"- Advisor Summary: `{self._clip_text(latest.get('advisor_summary'), 300)}`"
            )
        if latest.get("knowledge_summary"):
            lines.append(
                f"- Knowledge Summary: `{self._clip_text(latest.get('knowledge_summary'), 300)}`"
            )

        payloads = list(latest.get("payloads", []) or [])
        if payloads:
            lines.extend(["", "## Latest Payloads", ""])
            for payload in payloads[-8:]:
                lines.append(f"- `{self._clip_text(payload, 500)}`")

        action_history = list(latest.get("action_history", []) or [])
        if action_history:
            lines.extend(["", "## Latest Action History", ""])
            for action in action_history[-10:]:
                lines.append(f"- {self._clip_text(action, 500)}")

        if latest.get("system_prompt_excerpt") or latest.get("initial_prompt_excerpt"):
            lines.extend(["", "## Prompt Payloads", ""])
            if latest.get("system_prompt_excerpt"):
                lines.append(f"- System Prompt: {self._clip_text(latest.get('system_prompt_excerpt'), 700)}")
            if latest.get("initial_prompt_excerpt"):
                lines.append(f"- User Prompt: {self._clip_text(latest.get('initial_prompt_excerpt'), 700)}")
            if latest.get("memory_context_excerpt"):
                lines.append(f"- Memory Context: {self._clip_text(latest.get('memory_context_excerpt'), 500)}")
            if latest.get("skill_context_excerpt"):
                lines.append(f"- Skill Context: {self._clip_text(latest.get('skill_context_excerpt'), 500)}")

        thought_history = list(latest.get("decision_history", []) or [])
        if thought_history:
            lines.extend(["", "## Thought History", ""])
            for item in thought_history[-8:]:
                lines.append(f"- {self._clip_text(item, 500)}")

        advisor_history = list(latest.get("advisor_history", []) or [])
        if advisor_history:
            lines.extend(["", "## Advisor Trace", ""])
            for item in advisor_history[-6:]:
                lines.append(f"- {self._clip_text(item, 500)}")

        knowledge_history = list(latest.get("knowledge_history", []) or [])
        if knowledge_history:
            lines.extend(["", "## Knowledge Trace", ""])
            for item in knowledge_history[-6:]:
                lines.append(f"- {self._clip_text(item, 500)}")

        lines.extend(["", "## Recent Attempts", ""])
        for item in recent_records[-8:]:
            parts = [
                item.get("timestamp", ""),
                f"success={item.get('success', False)}",
                f"attempts={item.get('attempts', 0)}",
                f"elapsed={item.get('elapsed', 0)}",
            ]
            if item.get("flag"):
                parts.append(f"flag={item.get('flag')}")
            if item.get("error"):
                parts.append(
                    f"error={self._clip_text(item.get('error'), 180)}"
                )
            lines.append(f"- {' | '.join(str(p) for p in parts if p)}")

        return "\n".join(lines) + "\n"

    def record_writeup(
        self,
        challenge: Dict[str, Any],
        result: Dict[str, Any],
        *,
        zone: str = "",
        scope_key: Optional[str] = None,
        strategy_description: str = "",
        memory_context: str = "",
    ):
        """将单题结果持久化到当前目录 wp/ 下。"""
        challenge_code = str(
            challenge.get("display_code")
            or challenge.get("title")
            or challenge.get("code")
            or "unknown"
        )
        jsonl_path = self._get_wp_jsonl_path(challenge_code, scope_key=scope_key)
        markdown_path = self._get_wp_markdown_path(challenge_code, scope_key=scope_key)
        record = {
            "timestamp": datetime.now().isoformat(),
            "scope_key": scope_key or challenge_code,
            "challenge_code": challenge.get("code", challenge_code),
            "display_code": challenge.get("display_code") or challenge.get("title") or challenge_code,
            "zone": zone,
            "difficulty": challenge.get("difficulty", ""),
            "total_score": challenge.get("total_score", 0),
            "target": ", ".join(challenge.get("entrypoint") or []),
            "entrypoint": list(challenge.get("entrypoint") or []),
            "success": bool(result.get("success", False)),
            "flag": result.get("flag", ""),
            "attempts": result.get("attempts", 0),
            "elapsed": result.get("elapsed", 0),
            "error": result.get("error", ""),
            "payloads": list(result.get("payloads", []) or []),
            "action_history": list(result.get("action_history", []) or []),
            "scored_flags": list(result.get("scored_flags", []) or []),
            "flags_found_count": int(result.get("flags_found_count", 0) or 0),
            "flags_scored_count": int(result.get("flags_scored_count", 0) or 0),
            "expected_flag_count": int(result.get("expected_flag_count", 1) or 1),
            "strategy_description": strategy_description,
            "final_strategy": str(result.get("final_strategy", "") or "").strip(),
            "thought_summary": str(result.get("thought_summary", "") or "").strip(),
            "decision_history": list(result.get("decision_history", []) or []),
            "advisor_call_count": int(result.get("advisor_call_count", 0) or 0),
            "advisor_history": list(result.get("advisor_history", []) or []),
            "advisor_summary": str(result.get("advisor_summary", "") or "").strip(),
            "knowledge_call_count": int(result.get("knowledge_call_count", 0) or 0),
            "knowledge_history": list(result.get("knowledge_history", []) or []),
            "knowledge_summary": str(result.get("knowledge_summary", "") or "").strip(),
            "system_prompt_excerpt": self._clip_text(result.get("system_prompt_excerpt", ""), 1200),
            "initial_prompt_excerpt": self._clip_text(result.get("initial_prompt_excerpt", ""), 1200),
            "memory_context_excerpt": self._clip_text(memory_context, 600),
            "skill_context_excerpt": self._clip_text(result.get("skill_context_excerpt", ""), 900),
        }

        with self._lock:
            os.makedirs(self.wp_dir, exist_ok=True)
            with open(jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())

            recent_records = self._read_wp_records_unlocked(jsonl_path)
            markdown = self._render_wp_markdown(record, recent_records)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".wp_",
                suffix=".md",
                dir=self.wp_dir,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(markdown)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, markdown_path)
            finally:
                try:
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                except OSError:
                    pass

    def get_wp_context(
        self,
        challenge_code: str,
        scope_key: Optional[str] = None,
        limit: int = 3,
    ) -> str:
        """读取 wp/ 下同题历史，作为持久化记忆注入。"""
        path = self._get_wp_jsonl_path(challenge_code, scope_key=scope_key)
        with self._lock:
            records = self._read_wp_records_unlocked(path)
        if not records:
            return ""

        lines = []
        success_records = [item for item in records if item.get("success")]
        if success_records:
            latest_success = success_records[-1]
            success_parts = [
                latest_success.get("timestamp", ""),
                "最近成功链路",
            ]
            payloads = list(latest_success.get("payloads", []) or [])
            if payloads:
                success_parts.append(
                    "payloads=" + "; ".join(
                        self._clip_text(payload, 80) for payload in payloads[-3:]
                    )
                )
            actions = list(latest_success.get("action_history", []) or [])
            if actions:
                success_parts.append(
                    "actions=" + " => ".join(
                        self._clip_text(action, 70) for action in actions[-3:]
                    )
                )
            final_strategy = str(latest_success.get("final_strategy", "") or "").strip()
            if final_strategy:
                success_parts.append(
                    "strategy=" + self._clip_text(final_strategy, 100)
                )
            lines.append("  - " + " | ".join(str(part) for part in success_parts if part))

        for item in records[-limit:]:
            parts = [
                item.get("timestamp", ""),
                f"success={item.get('success', False)}",
                f"attempts={item.get('attempts', 0)}",
                f"elapsed={item.get('elapsed', 0)}",
            ]
            if item.get("flag"):
                parts.append(f"flag={item.get('flag')}")
            if item.get("error"):
                parts.append(f"error={self._clip_text(item.get('error'), 120)}")
            payloads = list(item.get("payloads", []) or [])
            if payloads:
                payload_summary = "; ".join(
                    self._clip_text(payload, 80) for payload in payloads[-3:]
                )
                parts.append(f"payloads={payload_summary}")
            if item.get("strategy_description"):
                parts.append(
                    f"strategy={self._clip_text(item.get('strategy_description'), 100)}"
                )
            final_strategy = str(item.get("final_strategy", "") or "").strip()
            if final_strategy:
                parts.append(
                    f"final={self._clip_text(final_strategy, 100)}"
                )
            lines.append("  - " + " | ".join(str(part) for part in parts if part))

        return "📝 本地 WP 持久化记忆:\n" + "\n".join(lines)

    def add_discovery(self, zone: str, discovery: str, bucket: Optional[str] = None):
        """记录赛区内的发现（可跨题复用）"""
        zone_key = self._discovery_storage_key(zone, bucket=bucket)
        if not zone_key:
            return
        with self._lock:
            if zone_key not in self._discoveries:
                self._discoveries[zone_key] = []
            if discovery not in self._discoveries[zone_key]:
                self._discoveries[zone_key].append(discovery)
                self._save_unlocked()
                logger.info(f"[Memory] 新发现 ({zone_key}): {discovery[:100]}")

    def add_credential(
        self,
        host: str,
        username: str,
        password: str,
        service: str = "",
        *,
        bucket: Optional[str] = None,
        zone: str = "",
    ):
        """记录发现的凭据"""
        cred = {
            "host": host,
            "username": username,
            "password": password,
            "service": service,
            "bucket": self._shared_bucket_name(bucket) if bucket else "",
            "zone": str(zone or "").strip(),
        }
        with self._lock:
            if cred not in self._credentials:
                self._credentials.append(cred)
                self._save_unlocked()
                logger.info(f"[Memory] 新凭据: {username}@{host}")

    def get_zone_discoveries(self, zone: str, bucket: Optional[str] = None) -> List[str]:
        """获取赛区内的所有发现"""
        with self._lock:
            result: List[str] = []
            for key in self._discovery_lookup_keys(zone, bucket=bucket):
                result.extend(self._discoveries.get(key, []))
            return result

    def get_credentials(self, *, bucket: Optional[str] = None, zone: str = "") -> List[Dict]:
        """获取所有已发现的凭据"""
        bucket_name = self._shared_bucket_name(bucket) if bucket else ""
        normalized_zone = str(zone or "").strip().lower()
        with self._lock:
            items: List[Dict] = []
            for cred in self._credentials:
                cred_bucket = str(cred.get("bucket", "") or "").strip()
                cred_zone = str(cred.get("zone", "") or "").strip().lower()
                if bucket_name == KNOWLEDGE_BUCKET_FORUM and cred_bucket != KNOWLEDGE_BUCKET_FORUM:
                    continue
                if bucket_name == KNOWLEDGE_BUCKET_MAIN and cred_bucket == KNOWLEDGE_BUCKET_FORUM:
                    continue
                if normalized_zone and cred_zone and cred_zone != normalized_zone:
                    continue
                items.append(dict(cred))
            return items

    def get_challenge_history(
        self,
        challenge_code: str,
        scope_key: Optional[str] = None,
    ) -> List[Dict]:
        """获取题目的历史尝试"""
        memory_key = scope_key or challenge_code
        with self._lock:
            return [dict(h) for h in self._memories.get(memory_key, [])]

    def get_context_for_challenge(
        self,
        challenge_code: str,
        zone: str,
        scope_key: Optional[str] = None,
        include_shared: bool = True,
        *,
        knowledge_bucket: Optional[str] = None,
        challenge: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        生成用于注入 Agent 提示的记忆上下文。
        """
        parts = []
        active_challenge = dict(challenge or {})
        if not active_challenge:
            active_challenge = {
                "display_code": challenge_code,
                "code": challenge_code,
                "zone": zone,
            }
        bucket_name = (
            self._shared_bucket_name(knowledge_bucket)
            if knowledge_bucket
            else self._shared_bucket_name(bucket_for_challenge(active_challenge))
        )

        # 题目历史
        history = self.get_challenge_history(challenge_code, scope_key=scope_key)
        if history:
            success_count = sum(1 for item in history if item.get("success"))
            parts.append(
                f"📋 此题历史尝试 {len(history)} 次，成功 {success_count} 次"
            )

        wp_context = self.get_wp_context(challenge_code, scope_key=scope_key)
        if wp_context:
            parts.append(wp_context)

        discoveries: List[str] = []
        creds: List[Dict] = []
        if include_shared:
            # 赛区发现
            discoveries = self.get_zone_discoveries(zone, bucket=bucket_name)
            if discoveries:
                parts.append(
                    f"🔍 赛区已知发现:\n" + "\n".join(f"  - {d}" for d in discoveries[-10:])
                )

            # 凭据
            creds = self.get_credentials(bucket=bucket_name, zone=zone)
            if creds:
                cred_lines = [
                    f"  - {c['username']}:{c['password']}@{c['host']} ({c.get('service', '')})"
                    for c in creds[-5:]
                ]
                parts.append(f"🔑 已知凭据:\n" + "\n".join(cred_lines))

        knowledge_context = search_local_knowledge_context(
            active_challenge,
            zone=zone,
            scope_key=str(scope_key or ""),
            top_k=2,
        )
        if knowledge_context:
            parts.append(knowledge_context)

        context_text = "\n\n".join(parts) if parts else ""
        logger.info(
            "[Memory] 上下文注入: challenge=%s bucket=%s history=%s wp=%s discoveries=%s credentials=%s structured_knowledge=%s payload=%s",
            challenge_code,
            bucket_name,
            len(history),
            bool(wp_context),
            len(discoveries),
            len(creds),
            bool(knowledge_context),
            self._clip_text(knowledge_context or "", 500) or "—",
        )
        return context_text


# 全局单例
_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    global _store
    if _store is None:
        _store = MemoryStore()
    return _store
