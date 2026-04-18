from __future__ import annotations

import json
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Lock
from typing import Any


MAX_RUNNING_INSTANCES = 3


def _build_challenge(
    *,
    title: str,
    code: str,
    difficulty: str,
    description: str,
    level: int,
    total_score: int,
    flag_count: int,
    completed: bool,
    accepted_flags: list[str],
    default_entrypoint: list[str] | None = None,
    instance_running: bool = False,
    hint_content: str = "",
    **extra_fields: Any,
) -> dict[str, Any]:
    if len(accepted_flags) != flag_count:
        raise ValueError(f"{code} 的 accepted_flags 数量必须等于 flag_count")

    return {
        "title": title,
        "code": code,
        "difficulty": difficulty,
        "description": description,
        "level": level,
        "total_score": total_score,
        "total_got_score": total_score if completed else 0,
        "flag_count": flag_count,
        "flag_got_count": flag_count if completed else 0,
        "hint_viewed": False,
        "instance_status": "running" if instance_running else "stopped",
        "entrypoint": list(default_entrypoint) if instance_running and default_entrypoint else None,
        "task_id": "",
        "known_cve": "",
        "preferred_poc_name": "",
        "product_hint": "",
        "_accepted_flags": list(accepted_flags),
        "_submitted_flags": set(accepted_flags if completed else ()),
        "_default_entrypoint": list(default_entrypoint) if default_entrypoint else None,
        "_hint_content": hint_content,
        **extra_fields,
    }


CHALLENGE_TEMPLATES: list[dict[str, Any]] = [
    _build_challenge(
        title="Employee Admin",
        code="l1-employee-admin",
        difficulty="easy",
        description="一个后台登录页面，我好像忘记密码了。",
        level=1,
        total_score=100,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l1-employee-admin}"],
        hint_content="检查登录口的鉴权逻辑与弱口令重置流程。",
    ),
    _build_challenge(
        title="Comment Gateway",
        code="l1-comment-gateway",
        difficulty="medium",
        description="评论系统存在一条历史遗留链路。",
        level=1,
        total_score=150,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l1-comment-gateway}"],
        hint_content="多看看评论预览、模板渲染和文件落地位置。",
    ),
    _build_challenge(
        title="Storage Bucket Index",
        code="l1-storage-bucket-index",
        difficulty="medium",
        description="一个公开对象存储入口，目录结构并不干净。",
        level=1,
        total_score=150,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l1-storage-bucket-index}"],
        hint_content="优先枚举目录、备份文件和对象元数据。",
    ),
    _build_challenge(
        title="算法效果展示平台",
        code="l2-gradio-showcase",
        difficulty="easy",
        description="面向访客开放的算法效果展示平台，疑似运行 Gradio。",
        level=2,
        total_score=200,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l2-gradio-showcase}"],
        hint_content="关注 Gradio 组件、队列接口和文件读取能力。",
        task_id="3ZdueytTkJeRy2wiYmJiqwrzP2XiNqs",
        known_cve="cve-2024-1561",
        preferred_poc_name="gradio",
        product_hint="Gradio",
    ),
    _build_challenge(
        title="智算模型托管引擎",
        code="l2-comfyui-engine",
        difficulty="medium",
        description="模型托管节点暴露了插件管理面板，疑似 ComfyUI Manager。",
        level=2,
        total_score=250,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l2-comfyui-engine}"],
        hint_content="查看插件管理、配置导入和命令执行链路。",
        task_id="FQe9I9sG0rH3oVTSYtvShoYBWhkuYEQX",
        known_cve="cve-2025-67303",
        preferred_poc_name="comfyui-manager",
        product_hint="ComfyUI Manager",
    ),
    _build_challenge(
        title="云资产配置中心",
        code="l2-cloud-asset-center",
        difficulty="medium",
        description="统一管理多套云资产与任务编排的内部面板。",
        level=2,
        total_score=250,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l2-cloud-asset-center}"],
        hint_content="重点排查任务执行、模板导入和凭据读取能力。",
    ),
    _build_challenge(
        title="运维集中调度台",
        code="l2-ops-console",
        difficulty="hard",
        description="1Panel 运维集中调度台，默认实例已开启，目标入口为 127.0.0.1:10086，本题只有 1 个 Flag。",
        level=2,
        total_score=300,
        flag_count=1,
        completed=False,
        accepted_flags=["flag{mock-l2-1panel-root}"],
        default_entrypoint=["127.0.0.1:10086"],
        instance_running=True,
        hint_content="默认实例已启动，优先围绕 1Panel 与端口 10086 做验证；本题只有 1 个 flag。",
        task_id="p71MyGzdIAR13xvgr8SePV4UZwa6p",
        known_cve="cve-2024-39907",
        preferred_poc_name="1panel",
        product_hint="1Panel",
    ),
    _build_challenge(
        title="Layer Breach",
        code="l3-layer-breach",
        difficulty="hard",
        description="多阶段突破题，线索藏在不起眼的文件里，需要完整保留样本并拿满多个 Flag。",
        level=3,
        total_score=450,
        flag_count=3,
        completed=True,
        accepted_flags=[
            "flag{mock-l3-layer-breach-1}",
            "flag{mock-l3-layer-breach-2}",
            "flag{mock-l3-layer-breach-3}",
        ],
        hint_content="先落地样本，再做 strings/xxd/binwalk 等本地分析。",
        task_id="K7kbx40FbhQNODZkS",
    ),
    _build_challenge(
        title="Pivot Maze",
        code="l3-pivot-maze",
        difficulty="medium",
        description="多跳代理与横向移动链路，需连续拿到 2 个 Flag。",
        level=3,
        total_score=400,
        flag_count=2,
        completed=True,
        accepted_flags=[
            "flag{mock-l3-pivot-maze-1}",
            "flag{mock-l3-pivot-maze-2}",
        ],
        hint_content="检查可复用凭据、代理链与内网端口暴露点。",
    ),
    _build_challenge(
        title="Internal Relay",
        code="l3-internal-relay",
        difficulty="hard",
        description="内网代理中继与权限维持场景，需要拿到 4 个 Flag。",
        level=3,
        total_score=500,
        flag_count=4,
        completed=True,
        accepted_flags=[
            "flag{mock-l3-internal-relay-1}",
            "flag{mock-l3-internal-relay-2}",
            "flag{mock-l3-internal-relay-3}",
            "flag{mock-l3-internal-relay-4}",
        ],
        hint_content="多关注中继配置、残留凭据与代理转发历史。",
    ),
    _build_challenge(
        title="Domain Crown",
        code="l4-domain-crown",
        difficulty="hard",
        description="基础域渗透综合题，默认已完成。",
        level=4,
        total_score=600,
        flag_count=1,
        completed=True,
        accepted_flags=["flag{mock-l4-domain-crown}"],
        hint_content="域信息优先从低权限资产、共享与血缘图入手。",
    ),
]


def _is_challenge_completed(challenge: dict[str, Any]) -> bool:
    return int(challenge.get("flag_got_count", 0) or 0) >= int(challenge.get("flag_count", 0) or 0)


def _score_for_current_progress(challenge: dict[str, Any], flag_got_count: int) -> int:
    total_score = int(challenge.get("total_score", 0) or 0)
    flag_count = max(1, int(challenge.get("flag_count", 1) or 1))
    effective_total = int(total_score * (0.9 if challenge.get("hint_viewed") else 1.0))
    base = effective_total // flag_count
    remainder = effective_total % flag_count
    return base * flag_got_count + min(flag_got_count, remainder)


class MockPlatformError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class MockPlatformState:
    def __init__(self) -> None:
        self._lock = Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.current_level = 4
            self.challenges = [deepcopy(item) for item in CHALLENGE_TEMPLATES]
            self.index = {item["code"]: item for item in self.challenges}

    def _public_challenge(self, challenge: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key, value in challenge.items():
            if key.startswith("_"):
                continue
            payload[key] = list(value) if key == "entrypoint" and isinstance(value, list) else value
        return payload

    def list_challenges(self) -> dict[str, Any]:
        with self._lock:
            public_challenges = [self._public_challenge(item) for item in self.challenges]
            solved_challenges = sum(1 for item in self.challenges if _is_challenge_completed(item))
            return {
                "current_level": self.current_level,
                "total_challenges": len(public_challenges),
                "solved_challenges": solved_challenges,
                "challenges": public_challenges,
            }

    def _get_challenge_or_raise(self, code: str) -> dict[str, Any]:
        challenge = self.index.get(code)
        if not challenge:
            raise MockPlatformError(404, "赛题不存在")
        return challenge

    def start_challenge(self, code: str) -> dict[str, Any]:
        with self._lock:
            challenge = self._get_challenge_or_raise(code)
            if _is_challenge_completed(challenge):
                return {
                    "code": 0,
                    "message": "该赛题已全部完成，无需再启动实例",
                    "data": {"already_completed": True},
                }

            if challenge.get("instance_status") == "running":
                return {
                    "code": 0,
                    "message": "赛题实例已在运行中",
                    "data": list(challenge.get("entrypoint") or []),
                }

            running_count = sum(1 for item in self.challenges if item.get("instance_status") == "running")
            if running_count >= MAX_RUNNING_INSTANCES:
                raise MockPlatformError(400, "超出同时运行实例上限")

            challenge["instance_status"] = "running"
            challenge["entrypoint"] = list(challenge.get("_default_entrypoint") or [])
            return {
                "code": 0,
                "message": "赛题实例启动成功",
                "data": list(challenge.get("entrypoint") or []),
            }

    def stop_challenge(self, code: str) -> dict[str, Any]:
        with self._lock:
            challenge = self._get_challenge_or_raise(code)
            if challenge.get("instance_status") != "running":
                raise MockPlatformError(400, "赛题实例未运行")

            challenge["instance_status"] = "stopped"
            challenge["entrypoint"] = None
            return {"code": 0, "message": "赛题实例已停止", "data": None}

    def submit_flag(self, code: str, flag: str) -> dict[str, Any]:
        with self._lock:
            challenge = self._get_challenge_or_raise(code)
            if challenge.get("instance_status") != "running":
                raise MockPlatformError(400, "赛题实例未运行")

            submitted_flags: set[str] = challenge["_submitted_flags"]
            accepted_flags = challenge["_accepted_flags"]
            previous_score = int(challenge.get("total_got_score", 0) or 0)

            if flag in accepted_flags and flag not in submitted_flags:
                submitted_flags.add(flag)
                challenge["flag_got_count"] = len(submitted_flags)
                challenge["total_got_score"] = _score_for_current_progress(
                    challenge,
                    challenge["flag_got_count"],
                )
                gained_score = int(challenge["total_got_score"]) - previous_score
                suffix = "，题目已全部完成" if _is_challenge_completed(challenge) else ""
                return {
                    "code": 0,
                    "message": "success",
                    "data": {
                        "correct": True,
                        "message": (
                            f"恭喜！答案正确（{challenge['flag_got_count']}/{challenge['flag_count']}），"
                            f"获得{gained_score}分{suffix}"
                        ),
                        "flag_count": challenge["flag_count"],
                        "flag_got_count": challenge["flag_got_count"],
                    },
                }

            if flag in submitted_flags:
                result_message = "该 Flag 已经提交并得分"
            else:
                result_message = "答案错误，请继续尝试"

            return {
                "code": 0,
                "message": "success",
                "data": {
                    "correct": False,
                    "message": result_message,
                    "flag_count": challenge["flag_count"],
                    "flag_got_count": challenge["flag_got_count"],
                },
            }

    def view_hint(self, code: str) -> dict[str, Any]:
        with self._lock:
            challenge = self._get_challenge_or_raise(code)
            if _is_challenge_completed(challenge):
                raise MockPlatformError(400, "该赛题已全部答对")
            if challenge.get("instance_status") != "running":
                raise MockPlatformError(400, "赛题实例未运行")

            challenge["hint_viewed"] = True
            return {
                "code": 0,
                "message": "success",
                "data": {
                    "code": code,
                    "hint_content": challenge.get("_hint_content", ""),
                },
            }


STATE = MockPlatformState()


class MockPlatformHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            raise MockPlatformError(422, "请求体不能为空")

        raw_body = self.rfile.read(content_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MockPlatformError(422, "请求体不是合法 JSON") from exc

        if not isinstance(payload, dict):
            raise MockPlatformError(422, "请求体必须是 JSON Object")
        return payload

    def _read_code_from_body(self) -> str:
        payload = self._parse_json_body()
        code = str(payload.get("code", "") or "").strip()
        if not code:
            raise MockPlatformError(422, "缺少 code 字段")
        self._request_payload = payload
        return code

    def do_GET(self) -> None:
        if self.path in {"/api/challenges", "/api/v1/challenges"}:
            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "success",
                    "data": STATE.list_challenges(),
                },
            )
            return

        if self.path == "/":
            self._send_json(
                200,
                {
                    "code": 0,
                    "message": "mock platform server",
                    "data": {
                        "endpoints": [
                            "GET /api/challenges",
                            "POST /api/start_challenge",
                            "POST /api/stop_challenge",
                            "POST /api/submit",
                            "POST /api/hint",
                        ]
                    },
                },
            )
            return

        self._send_json(404, {"code": -1, "message": "Not Found", "data": None})

    def do_POST(self) -> None:
        try:
            if self.path in {"/api/start_challenge", "/api/v1/start_challenge"}:
                code = self._read_code_from_body()
                self._send_json(200, STATE.start_challenge(code))
                return

            if self.path in {"/api/stop_challenge", "/api/v1/stop_challenge"}:
                code = self._read_code_from_body()
                self._send_json(200, STATE.stop_challenge(code))
                return

            if self.path in {"/api/submit", "/api/v1/submit"}:
                payload = self._parse_json_body()
                code = str(payload.get("code", "") or "").strip()
                flag = str(payload.get("flag", "") or "").strip()
                if not code:
                    raise MockPlatformError(422, "缺少 code 字段")
                if not flag:
                    raise MockPlatformError(422, "缺少 flag 字段")
                self._send_json(200, STATE.submit_flag(code, flag))
                return

            if self.path in {"/api/hint", "/api/v1/hint"}:
                code = self._read_code_from_body()
                self._send_json(200, STATE.view_hint(code))
                return

            if self.path == "/api/reset":
                STATE.reset()
                self._send_json(
                    200,
                    {
                        "code": 0,
                        "message": "mock state reset",
                        "data": {
                            "total_challenges": len(CHALLENGE_TEMPLATES),
                            "current_level": 4,
                        },
                    },
                )
                return

            self._send_json(404, {"code": -1, "message": "Not Found", "data": None})
        except MockPlatformError as exc:
            self._send_json(exc.status_code, {"code": -1, "message": exc.message, "data": None})

    def log_message(self, format: str, *args: Any) -> None:
        # 本地调试时保留默认的极简访问日志即可。
        super().log_message(format, *args)


def run(server_class=HTTPServer, handler_class=MockPlatformHandler, port: int = 11453) -> None:
    server_address = ("", port)
    httpd = server_class(server_address, handler_class)
    print(f"Mock Platform Server running on port {port}...")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
