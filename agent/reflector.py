"""
Reflector — 失败归因 + 智能反思
================================
在 Agent 连续失败后进行 L1-L4 级别的失败归因分析，
提供结构化的改进建议，防止重复错误。
"""
import logging
from typing import Dict, List, Any, Optional
from agent.prompts import render_prompt_template
from memory.knowledge_gateway import build_knowledge_advisor_context

logger = logging.getLogger(__name__)


REFLECTOR_PROMPT = """你是一名渗透测试反思专家。你的任务是分析攻击历程，找出失败原因，并给出具体的改进建议。

## 分析框架（失败归因等级）

### L1 — 工具使用错误
- 命令参数错误 / 工具选择不当
- 解决: 修正命令参数

### L2 — 信息不足
- 侦察不充分，遗漏关键端口/服务/路径
- 解决: 扩大侦察范围

### L3 — 策略方向错误
- 攻击向量选择错误（例如一直尝试 SQL 注入，但漏洞是文件上传）
- 解决: 切换攻击方向

### L4 — 认知偏差
- LLM 幻觉 / 重复相同失败操作 / 忽视已有线索
- 解决: 重置策略 + 提供新的思路

## 输出格式（必须遵循）
```
失败等级: L1/L2/L3/L4
根因分析: [一句话总结失败原因]
关键遗漏: [列出忽略的信息/线索]
建议操作:
1. [具体命令或策略]
2. [具体命令或策略]
3. [具体命令或策略]
```

## 当前攻击状况
{challenge_info}

## 操作历史
{action_history}
"""


async def reflect_on_failure(
    challenge: Dict[str, Any],
    action_history: List[str],
    advisor_llm=None,
    consecutive_failures: int = 0,
    advisor_skill_context: str = "",
    model: Optional[str] = None,
) -> str:
    """
    对连续失败进行反思分析。

    Args:
        challenge: 题目信息
        action_history: 操作历史
        advisor_llm: 用于反思的 LLM
        consecutive_failures: 连续失败次数

    Returns:
        反思结果（结构化文本）
    """
    challenge_info = _format_challenge_brief(challenge)
    history_text = "\n".join(action_history[-18:]) if action_history else "无操作历史"
    knowledge_context = build_knowledge_advisor_context(
        challenge,
        recon_info="",
        action_history=action_history,
        consecutive_failures=consecutive_failures,
    )

    prompt = render_prompt_template(
        REFLECTOR_PROMPT,
        challenge_info=challenge_info,
        action_history=history_text,
    )

    skill_context_note = ""
    if advisor_skill_context:
        skill_context_note = (
            "\n\n## 本地技能摘要（辅助参考）\n"
            f"{advisor_skill_context}"
        )
    if knowledge_context:
        skill_context_note += f"\n\n{knowledge_context}"
    failure_note = ""
    if consecutive_failures >= 6:
        failure_note = "\n⚠️ 已连续失败 6+ 次，很可能为 L3 或 L4 级别问题。请大胆建议完全不同的攻击方向。"
    elif consecutive_failures >= 3:
        failure_note = "\n⚠️ 已连续失败 3+ 次，请重点检查是否存在策略方向错误。"

    try:
        from claude_code_sdk import query, ClaudeCodeOptions
        from claude_code_sdk.types import AssistantMessage as SDKAssistantMessage
        full_prompt = prompt + skill_context_note + failure_note
        parts = []
        async for msg in query(
            prompt=f"请分析当前攻击失败的原因并给出改进建议。{failure_note}",
            options=ClaudeCodeOptions(system_prompt=full_prompt, max_turns=1, permission_mode="bypassPermissions", model=model),
        ):
            if isinstance(msg, SDKAssistantMessage):
                for block in msg.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
        result = "".join(parts)
        logger.info(f"[Reflector] 反思完成 (连续失败: {consecutive_failures}): {result[:200]}...")
        return result
    except Exception as e:
        logger.warning(f"[Reflector] 反思失败: {e}")
        return f"反思分析暂时不可用 ({e})。建议: 尝试完全不同的攻击方向。"


def _format_challenge_brief(challenge: dict) -> str:
    code = challenge.get("code", "unknown")
    entrypoint = challenge.get("entrypoint") or []
    target_str = ", ".join(entrypoint) if entrypoint else "未启动"
    return f"题目: {code} | 目标: {target_str}"


SUCCESS_REFLECTION_PROMPT = """你是一名资深 CTF 安全顾问，负责总结解题经验。

## 任务
基于刚才成功的解题过程，提炼出可复用的解题思路和经验教训。

## 输出要求
用简洁的语言（200-400字）总结：

1. **关键解题思路**（3-5条）
   - 发现了什么漏洞或突破口
   - 采用了什么攻击方法
   - 关键的绕过或利用技巧

2. **踩过的坑**（1-3条，如果有）
   - 哪些尝试失败了
   - 为什么失败
   - 如何调整后成功

3. **成功的关键**（1-2条）
   - 最终成功的决定性因素
   - 值得记住的经验

## 格式要求
- 使用简洁的陈述句，不要技术细节（如具体命令、参数）
- 聚焦"做了什么"和"为什么有效"，而不是"怎么做的"
- 每条思路控制在20-30字

## 示例输出
```
关键解题思路：
- 发现robots.txt泄露隐藏路径/admin
- 登录面存在SQL注入，使用OR 1=1绕过
- 通过GraphQL introspection发现隐藏的flag字段

踩过的坑：
- 初期尝试弱口令失败，实际需要SQL注入
- 直接查询flag字段被过滤，需要通过mutation添加prescription后查询

成功的关键：
- 识别出GraphQL接口并使用introspection获取完整schema
```

## 当前题目
{challenge_info}

## 解题过程摘要
{action_summary}

## 获得的Flag
{flag_info}
"""


async def reflect_on_success(
    challenge: Dict[str, Any],
    result: Dict[str, Any],
    advisor_llm=None,
    model: Optional[str] = None,
) -> str:
    """
    对成功的解题过程进行反思总结。

    Args:
        challenge: 题目信息
        result: 解题结果（包含action_history等）
        advisor_llm: 用于反思的 LLM

    Returns:
        反思总结文本
    """
    try:
        challenge_info = _format_challenge_brief(challenge)

        # 提取关键操作历史（最后20条）
        action_history = list(result.get("action_history", []) or [])[-20:]
        action_summary = "\n".join(f"- {line}" for line in action_history) if action_history else "无详细历史"

        # 提取成功的flag
        flag = result.get("flag", "")
        scored_flags = result.get("scored_flags", [])
        if scored_flags:
            flag_info = f"获得Flag: {', '.join(scored_flags)}"
        elif flag:
            flag_info = f"获得Flag: {flag}"
        else:
            flag_info = "成功完成题目"

        prompt = render_prompt_template(
            SUCCESS_REFLECTION_PROMPT,
            challenge_info=challenge_info,
            action_summary=action_summary,
            flag_info=flag_info,
        )

        from claude_code_sdk import query, ClaudeCodeOptions
        from claude_code_sdk.types import AssistantMessage as SDKAssistantMessage
        reflection_parts = []
        async for msg in query(
            prompt="请根据以上信息，总结这道题的解题思路和经验。",
            options=ClaudeCodeOptions(system_prompt=prompt, max_turns=1, permission_mode="bypassPermissions", model=model),
        ):
            if isinstance(msg, SDKAssistantMessage):
                for block in msg.content:
                    if hasattr(block, "text"):
                        reflection_parts.append(block.text)
        reflection = "".join(reflection_parts).strip()

        logger.info(
            "[Reflection] 成功反思完成: challenge=%s length=%d",
            challenge.get("code", "unknown"),
            len(reflection),
        )

        return reflection

    except Exception as exc:
        logger.warning("[Reflection] 成功反思失败: %s", exc)
        # 失败时返回简单总结
        return f"成功解决题目，获得Flag"
