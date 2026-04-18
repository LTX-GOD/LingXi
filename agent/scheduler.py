"""
四赛区闯关调度器
===============
管理四赛区的顺序解锁 + 智能优先级调度。

赛区 (对应关卡 level):
  Z1 (level=1): 识器·明理 — SRC 场景 (easy-medium)
  Z2 (level=2): 洞见·虚实 — CVE/云/AI 安全
  Z3 (level=3): 执刃·循迹 — 多层网络/OA
  Z4 (level=4): 铸剑·止戈 — AD 域渗透

解锁机制: 平台自动管理关卡解锁 (current_level 表示当前可见的最高关卡)
实例管理: 每队同时最多运行 3 个实例
"""
import asyncio
import logging
import os
import random
import time
from typing import Dict, List, Any, Optional, Set
from dataclasses import dataclass, field
from enum import Enum
from agent.main_battle_progress import should_clear_stale_solved
from tools.platform_api import CompetitionAPIClient, APIError, run_platform_api_io

logger = logging.getLogger(__name__)


class Zone(Enum):
    Z1_SRC = "zone1"       # 识器·明理 — SRC 真实场景 (level=1)
    Z2_CVE = "zone2"       # 洞见·虚实 — CVE/云安全/AI (level=2)
    Z3_NETWORK = "zone3"   # 执刃·循迹 — 多层网络/OA   (level=3)
    Z4_AD = "zone4"        # 铸剑·止戈 — AD 域渗透     (level=4)


# 关卡 → 赛区映射
LEVEL_TO_ZONE = {
    1: Zone.Z1_SRC,
    2: Zone.Z2_CVE,
    3: Zone.Z3_NETWORK,
    4: Zone.Z4_AD,
}

# 赛区详细信息
ZONE_INFO = {
    Zone.Z1_SRC: {
        "name": "识器·明理",
        "level": 1,
        "desc": "SRC 真实场景，自动化众测与主流漏洞发现",
        "focus": ["sql_injection", "xss", "file_upload", "ssrf", "rce", "deserialization"],
        "priority_order": "easy_first",
    },
    Zone.Z2_CVE: {
        "name": "洞见·虚实",
        "level": 2,
        "desc": "CVE、云安全、AI 基础设施与中间件/面板漏洞实测",
        "focus": ["nuclei_fingerprint", "middleware_rce", "cloud_metadata", "ai_platform_misconfig"],
        "priority_order": "easy_first",
    },
    Zone.Z3_NETWORK: {
        "name": "执刃·循迹",
        "level": 3,
        "desc": "综合渗透链路，多 Flag 多跳突破、横向移动与权限维持",
        "focus": ["multi_flag_chain", "lateral_movement", "proxy_tunnel", "credential_reuse"],
        "priority_order": "sequential",
    },
    Zone.Z4_AD: {
        "name": "铸剑·止戈",
        "level": 4,
        "desc": "基础域渗透，模拟企业核心内网环境",
        "focus": ["kerberoasting", "dcsync", "golden_ticket", "pass_the_hash", "bloodhound"],
        "priority_order": "sequential",
    },
}

# 难度权重（用于排序）
DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2, "unknown": 1}

# 重试队列等级（最小实现）
# L1: 首次尝试
# L2: 1-2 次失败后重试
# L3: >=3 次失败的高难重试
RETRY_L3_THRESHOLD = 3
RETRY_L2_THRESHOLD = 1
MIXED_START_DIFFICULTIES = ("easy", "medium", "hard")
MIXED_START_REVERSED = ("hard", "medium", "easy")
RECENTLY_STOPPED_UNSOLVED_COOLDOWN_SECONDS = 180
PREFERRED_INSTANCE_ZONE_LEVELS = (4, 3, 1)


@dataclass
class ZoneStatus:
    """单个赛区的状态"""
    zone: Zone
    unlocked: bool = False
    total_score: int = 0
    excluded_total: int = 0
    demo_skipped: int = 0
    solved: Set[str] = field(default_factory=set)
    failed: Dict[str, int] = field(default_factory=dict)  # code → retry count
    challenges: List[Dict] = field(default_factory=list)
    cooldown_until: Dict[str, float] = field(default_factory=dict)  # code -> next retry ts
    attempt_history: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # code -> history


class ZoneScheduler:
    """
    闯关调度器 — 管理四赛区的顺序解锁 + 智能调度

    策略:
    1. 平台通过 current_level 控制关卡解锁
    2. 题目按 level 字段自动分配到对应赛区
    3. 每个赛区内按难度排序 (easy → hard)
    4. 实例管理：同时最多 3 个运行中 (平台限制)
    5. 智能 hint 策略：前几次不用 hint，卡住了再用
    """

    MAX_RUNNING_INSTANCES = 3  # 官方限制

    def __init__(self, api_client: CompetitionAPIClient, config=None):
        self.api = api_client
        self.config = config
        self.zones: Dict[Zone, ZoneStatus] = {}
        self._init_zones()
        self.current_zone: Zone = Zone.Z1_SRC
        self.current_level: int = 1
        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.running_instances: Set[str] = set()  # 当前运行中的实例 code 集合
        self._challenge_zone_index: Dict[str, Zone] = {}  # code -> zone
        self._last_refresh_ts: float = 0.0
        self._last_refresh_attempt_ts: float = 0.0

    def _init_zones(self):
        for z in Zone:
            self.zones[z] = ZoneStatus(zone=z, unlocked=(z == Zone.Z1_SRC))

    @staticmethod
    def _challenge_match_text(challenge: Dict) -> str:
        return " ".join(
            str(challenge.get(key, "") or "").strip().lower()
            for key in ("code", "title", "display_code")
            if str(challenge.get(key, "") or "").strip()
        )

    @staticmethod
    def _demo_allowlist_tokens() -> tuple[str, ...]:
        raw = str(os.getenv("MAIN_BATTLE_DEMO_ALLOWLIST", "") or "").strip()
        if not raw:
            return ()
        return tuple(token.strip().lower() for token in raw.split(",") if token.strip())

    @classmethod
    def _is_demo_challenge(cls, challenge: Dict) -> bool:
        text = cls._challenge_match_text(challenge)
        return "demo" in text

    @classmethod
    def _is_demo_allowlisted(cls, challenge: Dict) -> bool:
        text = cls._challenge_match_text(challenge)
        tokens = cls._demo_allowlist_tokens()
        return any(token in text for token in tokens)

    async def refresh_challenges(self) -> List[Dict]:
        """从平台拉取题目并分配到赛区"""
        self._last_refresh_attempt_ts = time.time()
        try:
            data = await run_platform_api_io(self.api.get_challenges)
            challenges = data.get("challenges", [])
            self.current_level = data.get("current_level", 1)
            self._challenge_zone_index.clear()
            excluded_demo_total = 0

            # 清空旧数据（保留 solved/failed）
            for z in self.zones.values():
                z.challenges = []
                z.total_score = 0
                z.excluded_total = 0
                z.demo_skipped = 0

            self.running_instances.clear()

            # 收集当前平台返回的所有题目code
            current_challenge_codes = set()

            for c in challenges:
                zone = self._classify_zone(c)
                if self._is_demo_challenge(c) and not self._is_demo_allowlisted(c):
                    self.zones[zone].excluded_total += 1
                    self.zones[zone].demo_skipped += 1
                    excluded_demo_total += 1
                    continue

                # 生成稳定的memory_scope_key（基于title而不是随机code）
                title = c.get("title", "")
                code = c.get("code", "")
                if title:
                    # 使用title作为稳定的scope_key，确保同一道题目的历史经验可以复用
                    c["memory_scope_key"] = title
                elif code:
                    # 如果没有title，回退到code
                    c["memory_scope_key"] = code

                self.zones[zone].challenges.append(c)

                if code:
                    self._challenge_zone_index[code] = zone
                    current_challenge_codes.add(code)

                # 追踪运行中的实例
                if c.get("instance_status") == "running":
                    self.running_instances.add(code)

                # 根据平台返回的实际状态更新 solved 集合
                flag_count = c.get("flag_count", 0)
                flag_got = c.get("flag_got_count", 0)
                if flag_count > 0 and flag_got >= flag_count:
                    # 题目已完成，添加到 solved
                    self.zones[zone].solved.add(code)
                    self.zones[zone].failed.pop(code, None)
                    self.zones[zone].cooldown_until.pop(code, None)
                    self.zones[zone].attempt_history.pop(code, None)
                else:
                    if should_clear_stale_solved(
                        locally_solved=code in self.zones[zone].solved,
                        flag_got_count=flag_got,
                        flag_count=flag_count,
                        instance_status=str(c.get("instance_status", "") or ""),
                    ):
                        self.zones[zone].solved.discard(code)
                        logger.info(
                            "[Zone] ♻️ 清理本地过期 solved: %s | remote=%s/%s instance=%s",
                            code,
                            flag_got,
                            flag_count,
                            c.get("instance_status", ""),
                        )

            for zone, status in self.zones.items():
                status.total_score = sum(
                    ch.get("total_got_score", 0) for ch in status.challenges
                )

            # 根据 current_level 更新赛区解锁状态
            self._update_unlock_from_level()
            self._last_refresh_ts = time.time()
            self._last_refresh_attempt_ts = self._last_refresh_ts

            # 日志
            for z, status in self.zones.items():
                info = ZONE_INFO[z]
                if status.challenges:
                    logger.debug(
                        f"[Zone] {info['name']} (L{info['level']}): "
                        f"{'🔓' if status.unlocked else '🔒'} | "
                        f"题目: {len(status.challenges)} | "
                        f"已解: {len(status.solved)} | "
                        f"得分: {status.total_score}"
                    )

            logger.debug(
                f"[Zone] 当前关卡: L{self.current_level} | 运行中实例: "
                f"{len(self.running_instances)}/{self.MAX_RUNNING_INSTANCES}"
            )
            unsolved_visible = sum(
                1
                for status in self.zones.values()
                for ch in status.challenges
                if (ch.get("flag_count", 0) or 0) > (ch.get("flag_got_count", 0) or 0)
            )
            if unsolved_visible > self.MAX_RUNNING_INSTANCES:
                logger.debug(
                    "[Zone] 当前可见未完成主战场题目 %s 道，但官方平台同一时间最多只允许运行 %s 个实例；其余题目会排队等待空闲槽位。",
                    unsolved_visible,
                    self.MAX_RUNNING_INSTANCES,
                )
            if excluded_demo_total > 0:
                logger.debug(
                    "[Zone] 已排除非白名单 demo 题 %s 道：不参与自动调度、不计入总题数。",
                    excluded_demo_total,
                )

            visible_challenges = [
                item
                for status in self.zones.values()
                for item in status.challenges
            ]
            return visible_challenges
        except Exception as e:
            self._last_refresh_attempt_ts = time.time()
            logger.error(f"[Zone] 拉取题目失败: {e}")
            return []

    def need_refresh(self, interval_seconds: int) -> bool:
        """是否需要刷新赛题状态。"""
        latest = max(self._last_refresh_ts, self._last_refresh_attempt_ts)
        if latest <= 0:
            return True
        return (time.time() - latest) >= max(1, interval_seconds)

    def _classify_zone(self, challenge: Dict) -> Zone:
        """根据题目 level 字段判断所属赛区"""
        level = challenge.get("level", 1)
        return LEVEL_TO_ZONE.get(level, Zone.Z1_SRC)

    def _update_unlock_from_level(self):
        """根据平台 current_level 更新赛区解锁状态"""
        for z in Zone:
            zone_level = ZONE_INFO[z]["level"]
            was_unlocked = self.zones[z].unlocked
            self.zones[z].unlocked = zone_level <= self.current_level

            if self.zones[z].unlocked and not was_unlocked:
                logger.info(f"[Zone] 🔓 解锁赛区: {ZONE_INFO[z]['name']} (level={zone_level})")

        # 更新当前赛区为最高已解锁赛区
        for z in reversed([Zone.Z1_SRC, Zone.Z2_CVE, Zone.Z3_NETWORK, Zone.Z4_AD]):
            if self.zones[z].unlocked:
                self.current_zone = z
                break

    def can_start_instance(self) -> bool:
        """是否还能启动新实例"""
        return len(self.running_instances) < self.MAX_RUNNING_INSTANCES

    def get_running_count(self) -> int:
        """当前运行中的实例数"""
        return len(self.running_instances)

    @staticmethod
    def _is_challenge_fully_solved(challenge: Dict[str, Any]) -> bool:
        flag_count = int(challenge.get("flag_count", 0) or 0)
        flag_got = int(challenge.get("flag_got_count", 0) or 0)
        return flag_count > 0 and flag_got >= flag_count

    def get_reclaimable_running_instances(self) -> List[Dict[str, Any]]:
        """
        返回可以安全回收的运行中实例。

        平台偶尔会遗留“题目已解但实例仍显示 running”的脏状态。
        这些实例会持续占用 3 个启动槽位，导致后续题目无法被调度。
        仅回收“已经拿满全部 flag”的题目，避免误停 L3/L4 部分完成的多 flag 题。
        """
        reclaimable: List[Dict[str, Any]] = []
        for status in self.zones.values():
            for challenge in status.challenges:
                code = str(challenge.get("code", "") or "").strip()
                if not code:
                    continue
                if challenge.get("instance_status") != "running":
                    continue
                if code not in self.running_instances:
                    continue
                if not self._is_challenge_fully_solved(challenge):
                    continue
                reclaimable.append(challenge)
        return reclaimable

    def _update_cached_instance_state(
        self,
        code: str,
        *,
        instance_status: str,
        entrypoint: Optional[List[str]] = None,
    ) -> None:
        """同步更新本地缓存中的题目实例状态，避免调度依赖过期的 platform 快照。"""
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        for challenge in self.zones[zone].challenges:
            if challenge.get("code", "") != code:
                continue
            challenge["instance_status"] = instance_status
            if entrypoint is not None:
                challenge["entrypoint"] = list(entrypoint)
            elif instance_status != "running":
                challenge["entrypoint"] = []
            break

    def _update_cached_flag_progress(
        self,
        code: str,
        *,
        flag_got_count: Optional[int] = None,
        flag_count: Optional[int] = None,
    ) -> None:
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        status = self.zones[zone]
        for challenge in status.challenges:
            if challenge.get("code", "") != code:
                continue
            if flag_count is not None:
                challenge["flag_count"] = max(
                    int(challenge.get("flag_count", 0) or 0),
                    int(flag_count or 0),
                )
            if flag_got_count is not None:
                challenge["flag_got_count"] = max(
                    int(challenge.get("flag_got_count", 0) or 0),
                    int(flag_got_count or 0),
                )
            if self._is_challenge_fully_solved(challenge):
                status.solved.add(code)
                status.failed.pop(code, None)
                status.cooldown_until.pop(code, None)
            break

    def mark_instance_started(self, code: str, entrypoint: Optional[List[str]] = None):
        """标记实例已启动"""
        self.running_instances.add(code)
        self._update_cached_instance_state(
            code,
            instance_status="running",
            entrypoint=entrypoint,
        )

    def mark_instance_stopped(self, code: str):
        """标记实例已停止"""
        self.running_instances.discard(code)
        self._update_cached_instance_state(code, instance_status="stopped")

    @staticmethod
    def _normalize_difficulty(value: Any) -> str:
        normalized = str(value or "unknown").strip().lower()
        return normalized if normalized in {"easy", "medium", "hard"} else "unknown"

    def _pick_mixed_difficulty_start_batch(
        self,
        candidates: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], Set[str]]:
        """
        当 3 个实例槽位都空闲时，随机挑选 easy / medium / hard 各一题，
        并按 hard -> medium -> easy 的顺序启动。
        若凑不齐三档难度，则回退到普通调度顺序。
        """
        if len(candidates) < len(MIXED_START_DIFFICULTIES):
            return [], set()

        zone_priority = {zone: idx for idx, zone in enumerate(self._get_zone_priority())}
        selected_by_difficulty: Dict[str, Dict[str, Any]] = {}
        selected_codes: Set[str] = set()

        for difficulty in MIXED_START_DIFFICULTIES:
            pool = [
                challenge
                for challenge in candidates
                if challenge.get("code", "") not in selected_codes
                and self._normalize_difficulty(challenge.get("difficulty")) == difficulty
            ]
            if not pool:
                return [], set()

            best_zone_rank = min(
                zone_priority.get(self.get_zone_for_challenge(challenge.get("code", "")), len(zone_priority))
                for challenge in pool
            )
            zone_filtered = [
                challenge
                for challenge in pool
                if zone_priority.get(
                    self.get_zone_for_challenge(challenge.get("code", "")),
                    len(zone_priority),
                ) == best_zone_rank
            ]
            best_retry_count = min(
                self.zones[self.get_zone_for_challenge(challenge.get("code", ""))].failed.get(
                    challenge.get("code", ""),
                    0,
                )
                if self.get_zone_for_challenge(challenge.get("code", "")) in self.zones
                else 0
                for challenge in zone_filtered
            )
            retry_filtered = []
            for challenge in zone_filtered:
                zone = self.get_zone_for_challenge(challenge.get("code", ""))
                retry_count = self.zones[zone].failed.get(challenge.get("code", ""), 0) if zone in self.zones else 0
                if retry_count == best_retry_count:
                    retry_filtered.append(challenge)

            chosen = random.choice(retry_filtered)
            selected_by_difficulty[difficulty] = dict(chosen)
            selected_codes.add(chosen.get("code", ""))

        ordered_batch: List[Dict[str, Any]] = []
        for launch_rank, difficulty in enumerate(MIXED_START_REVERSED):
            challenge = dict(selected_by_difficulty[difficulty])
            challenge["_startup_priority"] = launch_rank
            challenge["_startup_strategy"] = "mixed_difficulty_random_reverse"
            ordered_batch.append(challenge)

        logger.info(
            "[Zone] 三实例混合启动已启用: hard=%s | medium=%s | easy=%s",
            selected_by_difficulty["hard"].get("code", ""),
            selected_by_difficulty["medium"].get("code", ""),
            selected_by_difficulty["easy"].get("code", ""),
        )
        return ordered_batch, selected_codes

    @staticmethod
    def _preferred_instance_zones() -> List[Zone]:
        ordered: List[Zone] = []
        seen: Set[Zone] = set()
        for level in PREFERRED_INSTANCE_ZONE_LEVELS:
            zone = LEVEL_TO_ZONE.get(level)
            if zone is None or zone in seen:
                continue
            ordered.append(zone)
            seen.add(zone)
        return ordered

    def _pick_preferred_zone_start_batch(
        self,
        candidates: List[Dict[str, Any]],
        *,
        max_slots: int,
    ) -> tuple[List[Dict[str, Any]], Set[str]]:
        """
        固定新实例槽位分配：
        优先保证 L4 / L3 / L1 各有 1 个靶机在打，再让剩余空位走普通补位逻辑。
        """
        if max_slots <= 0:
            return [], set()

        selected: List[Dict[str, Any]] = []
        selected_codes: Set[str] = set()
        selected_labels: List[str] = []

        for launch_rank, zone in enumerate(self._preferred_instance_zones()):
            if len(selected) >= max_slots:
                break
            if not self.zones[zone].unlocked:
                continue

            chosen: Optional[Dict[str, Any]] = None
            for challenge in candidates:
                code = challenge.get("code", "")
                if code in selected_codes:
                    continue
                if self.get_zone_for_challenge(code) != zone:
                    continue
                chosen = challenge
                break

            if chosen is None:
                continue

            scheduled = dict(chosen)
            scheduled["_startup_priority"] = launch_rank
            scheduled["_startup_strategy"] = "preferred_zone_slots"
            selected.append(scheduled)
            selected_codes.add(chosen.get("code", ""))
            selected_labels.append(f"L{ZONE_INFO[zone]['level']}={chosen.get('code', '')}")

        if selected_labels:
            logger.info("[Zone] 固定槽位启动: %s", " | ".join(selected_labels))

        return selected, selected_codes

    def get_next_challenges(self, max_count: int = 8, exclude_codes: Optional[Set[str]] = None) -> List[Dict]:
        """
        获取下一批要做的题目（智能优先级排序）

        策略:
        1. 优先续跑已在 running 的实例
        2. 新实例优先按固定槽位分配：L4 / L3 / L1 各 1 个
        3. 余下空位再按常规优先级补位
        """
        result = []
        max_retries = self.config.agent.max_retries if self.config else 4
        running_budget = max(0, self.MAX_RUNNING_INSTANCES - len(self.running_instances))
        runnable_new = []
        runnable_running = []
        excluded = exclude_codes or set()
        now_ts = time.time()

        # 遍历所有已解锁赛区（优先当前赛区）
        zone_order = self._get_zone_priority()

        for zone in zone_order:
            status = self.zones[zone]
            if not status.unlocked:
                continue

            # 过滤出待做的题目，并按重试等级分层
            pending = []
            for c in status.challenges:
                code = c.get("code", "")
                if code in status.solved:
                    continue
                if code in self.active_tasks:
                    continue
                if code in excluded:
                    continue
                if status.failed.get(code, 0) >= max_retries:
                    continue
                cooldown_until = status.cooldown_until.get(code, 0)
                if cooldown_until > now_ts:
                    continue
                pending.append(c)

            # 多级队列: L1/L2/L3
            info = ZONE_INFO[zone]
            queue_buckets: Dict[int, List[Dict[str, Any]]] = {1: [], 2: [], 3: []}
            for c in pending:
                code = c.get("code", "")
                retries = status.failed.get(code, 0)
                queue_level = self._retry_level_from_count(retries)
                queue_buckets[queue_level].append(c)

            ordered_pending: List[Dict[str, Any]] = []
            for queue_level in (1, 2, 3):
                bucket = queue_buckets[queue_level]
                if info["priority_order"] == "easy_first":
                    bucket.sort(
                        key=lambda c: (
                            DIFFICULTY_ORDER.get(c.get("difficulty", "unknown"), 1),
                            status.failed.get(c.get("code", ""), 0),
                        )
                    )
                else:
                    bucket.sort(key=lambda c: status.failed.get(c.get("code", ""), 0))
                ordered_pending.extend(bucket)

            for c in ordered_pending:
                code = c.get("code", "")
                if code in self.running_instances:
                    runnable_running.append(c)
                else:
                    runnable_new.append(c)

        # 先执行已运行实例（不占启动额度）
        for c in runnable_running:
            if len(result) >= max_count:
                break
            result.append(c)

        selected_preferred_codes: Set[str] = set()
        preferred_batch, selected_preferred_codes = self._pick_preferred_zone_start_batch(
            runnable_new,
            max_slots=min(running_budget, max(0, max_count - len(result))),
        )
        for challenge in preferred_batch:
            if len(result) >= max_count or running_budget <= 0:
                break
            result.append(challenge)
            running_budget -= 1

        # 再补充可新启动实例（受 3 实例上限约束）
        for c in runnable_new:
            if len(result) >= max_count or running_budget <= 0:
                break
            if c.get("code", "") in selected_preferred_codes:
                continue
            result.append(c)
            running_budget -= 1

        return result

    def _get_zone_priority(self) -> List[Zone]:
        """获取赛区优先级（新解锁/更高关卡优先，老赛区靠后）"""
        unlocked = [zone for zone in Zone if self.zones[zone].unlocked]
        unlocked.sort(key=lambda zone: ZONE_INFO[zone]["level"], reverse=True)
        locked = [zone for zone in Zone if zone not in unlocked]
        locked.sort(key=lambda zone: ZONE_INFO[zone]["level"], reverse=True)
        return unlocked + locked

    def mark_solved(self, code: str, zone: Optional[Zone] = None):
        """标记题目已解决"""
        z = zone or self._challenge_zone_index.get(code)
        if not z:
            return
        status = self.zones[z]
        status.solved.add(code)
        status.failed.pop(code, None)
        status.cooldown_until.pop(code, None)
        status.total_score = sum(ch.get("total_got_score", 0) for ch in status.challenges)
        logger.info(f"[Zone] ✅ {code} 已解 (赛区得分: {status.total_score})")

    def mark_failed(self, code: str):
        """标记题目失败"""
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        status = self.zones[zone]
        status.failed[code] = status.failed.get(code, 0) + 1
        backoff = self.config.agent.retry_backoff_seconds if self.config else 60
        status.cooldown_until[code] = time.time() + max(1, backoff)

    def mark_transient_failure(self, code: str, cooldown_seconds: Optional[int] = None):
        """
        标记基础设施级瞬时失败。
        只进入冷却，不消耗题目本身的失败次数，避免网络/模型/平台抖动把题目打入死队列。
        """
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        status = self.zones[zone]
        backoff = cooldown_seconds
        if backoff is None:
            backoff = self.config.agent.retry_backoff_seconds if self.config else 60
        status.cooldown_until[code] = time.time() + max(1, int(backoff))

    def mark_recently_stopped_unsolved(self, code: str, cooldown_seconds: Optional[int] = None):
        """
        标记“刚被主动停掉且尚未解出”的题目。
        这类题通常已经确认当前轮次 ROI 很低，不能让调度器马上把它重新拉起。
        """
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        status = self.zones[zone]
        if code in status.solved:
            return
        backoff = cooldown_seconds
        if backoff is None:
            retry_backoff = self.config.agent.retry_backoff_seconds if self.config else 60
            backoff = max(RECENTLY_STOPPED_UNSOLVED_COOLDOWN_SECONDS, int(retry_backoff) * 2)
        next_retry_at = time.time() + max(1, int(backoff))
        status.cooldown_until[code] = max(status.cooldown_until.get(code, 0), next_retry_at)
        logger.info(
            "[Zone] 未解题目进入重启冷却: code=%s cooldown=%ss",
            code,
            max(1, int(backoff)),
        )

    def record_attempt_result(self, code: str, result: Dict[str, Any]):
        """记录单题尝试摘要，供后续重试注入上下文。"""
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return
        status = self.zones[zone]
        history = status.attempt_history.setdefault(code, [])
        history.append(
            {
                "timestamp": time.time(),
                "success": result.get("success", False),
                "attempts": result.get("attempts", 0),
                "elapsed": result.get("elapsed", 0),
                "error": result.get("error", ""),
                "retry_count": status.failed.get(code, 0),
                "retry_level": self._retry_level_from_count(status.failed.get(code, 0)),
            }
        )
        limit = self.config.agent.attempt_history_limit if self.config else 3
        if len(history) > limit:
            status.attempt_history[code] = history[-limit:]

        flags_scored_count = result.get("flags_scored_count")
        expected_flag_count = result.get("expected_flag_count")
        if flags_scored_count is not None or expected_flag_count is not None:
            self._update_cached_flag_progress(
                code,
                flag_got_count=int(flags_scored_count or 0) if flags_scored_count is not None else None,
                flag_count=int(expected_flag_count or 0) if expected_flag_count is not None else None,
            )

    def get_attempt_history(self, code: str) -> List[Dict[str, Any]]:
        """获取题目历史尝试记录。"""
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return []
        history = self.zones[zone].attempt_history.get(code, [])
        return [dict(item) for item in history]

    def get_zone_for_challenge(self, code: str) -> Optional[Zone]:
        """查找题目所属赛区"""
        return self._challenge_zone_index.get(code)

    def _retry_level_from_count(self, retry_count: int) -> int:
        """按失败次数映射重试等级。"""
        if retry_count >= RETRY_L3_THRESHOLD:
            return 3
        if retry_count >= RETRY_L2_THRESHOLD:
            return 2
        return 1

    def get_retry_count(self, code: str) -> int:
        """查询题目的累计失败次数。"""
        zone = self._challenge_zone_index.get(code)
        if not zone:
            return 0
        return self.zones[zone].failed.get(code, 0)

    def get_retry_level(self, code: str) -> int:
        """查询题目当前重试队列等级（L1/L2/L3）。"""
        return self._retry_level_from_count(self.get_retry_count(code))

    def get_zone_strategy(self, zone: Zone) -> str:
        """获取赛区专属攻击策略"""
        return ZONE_STRATEGIES.get(zone, "")

    def get_status_summary(self) -> str:
        """获取全局状态摘要"""
        lines = [f"📊 赛区状态 (当前关卡: L{self.current_level}):"]
        total_solved = 0
        total_score = 0
        for z in Zone:
            s = self.zones[z]
            info = ZONE_INFO[z]
            lock = "🔓" if s.unlocked else "🔒"
            queue_counts = {1: 0, 2: 0, 3: 0}
            for c in s.challenges:
                code = c.get("code", "")
                if code in s.solved:
                    continue
                level = self._retry_level_from_count(s.failed.get(code, 0))
                queue_counts[level] += 1
            lines.append(
                f"  {lock} {info['name']} (L{info['level']}): "
                f"{len(s.solved)}/{len(s.challenges)} 题 | {s.total_score} 分 | "
                f"队列 L1/L2/L3={queue_counts[1]}/{queue_counts[2]}/{queue_counts[3]}"
            )
            total_solved += len(s.solved)
            total_score += s.total_score
        lines.append(f"  总计: {total_solved} 题 | {total_score} 分")
        lines.append(f"  运行中实例: {len(self.running_instances)}/{self.MAX_RUNNING_INSTANCES}")
        return "\n".join(lines)


# ─── 赛区专属攻击策略 ───

ZONE_STRATEGIES = {
    Zone.Z1_SRC: """
## 赛区策略: 识器·明理（SRC 场景）
这是 SRC 真实场景题，目标是尽快拿到足够多的有效 Flag。攻击路线：

1. **30 秒内完成低成本摸底** → `nmap -sV -sC -Pn target`、主页/登录页/`robots.txt`/`/.git/HEAD`/`/docs`
2. **目录与接口快速枚举** → `gobuster` / `ffuf` 小字典先跑，先找隐藏路由和未鉴权接口
3. **优先工具而非手工空转**:
   - SQL 注入优先 `sqlmap`，复杂登录流用 `-r request.txt`
   - 常见 Web 漏洞先用现成工具验证，再用 Python 精确利用
4. **漏洞优先级**:
   - SQL 注入 / 认证绕过 / 弱口令
   - 文件上传 (双扩展、MIME、解析差异、大小写)
   - SSRF / 目录遍历 / 命令注入 / 反序列化
5. **时间纪律** → 单题连续数分钟没有新证据就降优先级，先拿其他题的分
6. **Flag 位置** → `/flag`、`/root/flag*`、`/home/*/flag*`、源码与配置文件、数据库表
""",

    Zone.Z2_CVE: """
## 赛区策略: 洞见·虚实（CVE/云/AI）
聚焦已知 CVE、AI 基础设施和中间件/面板，默认先 `nuclei` 再定向利用。攻击路线：

1. **指纹优先** → `nmap -sV -Pn`、Banner、响应头、`/login`、`/api`、`/v1`、`/docs`、`/openapi.json`
2. **中间件/面板优先跑 `nuclei`**：
   - `nuclei -u URL -as -rl 150` 做通用指纹与高危模板命中
   - 对 `1Panel`、`ComfyUI Manager`、`Dify`、`GeoServer`、`Gradio`、`HugeGraph Gremlin RCE`、`GNU InetUtils Telnetd`、`Langflow`、`Nacos`、`OFBiz` 优先做版本识别和模板验证
3. **再做定向利用**：
   - 命中指纹后立刻补 `searchsploit service version`、默认路由、未授权面板、插件/工作流导入、文件写入和 RCE
   - AI 基础设施优先检查工作流导入、插件安装、任务执行、文件读取、SSRF、命令执行和凭据泄露
4. **云安全**:
   - SSRF → 元数据 `169.254.169.254` 或 `metadata.tencentyun.com`
   - 对象存储公开访问 / Bucket 遍历
   - API Key 泄露 (.env, .git, config)
5. **效率纪律**：
   - `nuclei` / 指纹没给出方向前，不要一上来就手写大段 PoC
   - 命中 `1Panel`、`GeoServer`、`Gradio`、`Langflow`、`Nacos`、`OFBiz` 等组件时，优先走“现成利用链 + 最小验证”
""",

    Zone.Z3_NETWORK: """
## 赛区策略: 执刃·循迹（多层网络/OA）
多跳渗透与综合渗透链路，重点是 **多 Flag**、**横向移动** 和 **代理隧道**。

1. **按题型规划链路**：
   - `Corporate Breach`：先拿公开入口与办公系统 foothold，再收集账号、共享目录、运维痕迹，按多 Flag 链路逐层推进
   - `Deep Penetration`：默认会有更多内网段和更多 Flag，先保住首个落点，再快速建立代理/隧道
   - `Internal Infiltration`：优先内网资产识别、服务映射、凭据复用和权限提升，让每一跳都带出下一跳入口
2. **外网突破** → 先拿到跳板机 (Web 漏洞/弱口令)
3. **内网信息收集**:
   - `ifconfig` / `ip addr` 查看网段
   - `arp -a` 存活主机
   - `cat /etc/hosts` 内部域名
   - 扫描子网: `nmap -sT -Pn 192.168.x.0/24`
4. **建立隧道**:
   - chisel: `chisel server -p 8080 --reverse` + `chisel client ip:8080 R:socks`
   - SSH 动态转发: `ssh -D 1080 user@pivot`
   - frp
5. **横向移动**:
   - 密码/密钥复用
   - 配置文件、脚本、环境变量中的凭据
   - 数据库中的用户密码与连接串
6. **多 Flag 节奏**：
   - 一旦拿到一个 Flag，立即复盘当前主机是否还有第二个 Flag、第二条凭据、第二个管理面
   - 不要把“拿到 shell”当终点，要把 shell 当作通往下一跳和下一枚 Flag 的起点
7. **纪律**: 每突破一层都记录网段、主机、凭据和可复用入口，不做与比赛无关的破坏性持久化
8. **Flag 线索**: 先搜源码、共享目录、运维脚本、历史命令和内网管理面板
""",

    Zone.Z4_AD: """
## 赛区策略: 铸剑·止戈（AD 域渗透）
基础域渗透。核心路线: **域用户 → 域管 → 域控**。

1. **域信息收集**:
   - `net user /domain`, `net group "domain admins" /domain`
   - BloodHound: `bloodhound-python -d domain -u user -p pass -ns DC_IP -c all`
   - LDAP 枚举: `ldapsearch` / `impacket-GetADUsers`
2. **凭据获取**:
   - Kerberoasting: `impacket-GetUserSPNs domain/user:pass -dc-ip DC -request`
   - AS-REP Roasting: `impacket-GetNPUsers domain/ -usersfile users.txt`
   - NTLM Relay
3. **横向提权**:
   - Pass-the-Hash: `impacket-psexec -hashes :HASH domain/admin@target`
   - WMIExec, Evil-WinRM
   - 委派攻击 (非约束/约束/RBCD)
4. **拿域控**:
   - DCSync: `impacket-secretsdump domain/admin:pass@DC`
   - Golden Ticket: 获取 krbtgt hash
   - Zerologon (CVE-2020-1472)
5. **Flag**: C:\\\\flag.txt, Administrator 桌面, AD description 属性
""",
}
