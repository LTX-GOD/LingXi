"""
知识库问答 Agent：在线走生成式回答，离线时退化为抽取式总结或本地 LLM。
"""
from __future__ import annotations

from typing import Any

from ctf_kb.config import cfg
from ctf_kb.rag.retriever import SearchFilters, format_hits, retrieve, retrieve_filtered

SYSTEM_PROMPT = """\
你是一个专业的 CTF 安全专家 Agent。你拥有一个真实 CTF writeup 知识库，
里面收录了来自 CTFTime 与战队博客的多类别题解（Web / Pwn / Crypto / Misc / Reverse / Forensics / OSINT）。

解题策略：
1. 遇到技术问题，优先检索相关 writeup；
2. 如果能识别题目类别，要优先使用同类别结果；
3. 如果知道赛事名或题目名，可进一步过滤；
4. 基于检索到的真实案例，给出分析和关键 payload / 利用步骤；
5. 回答简洁、技术性强，区分"通用技巧"与"当前题目具体利用链"；
6. 给出引用来源（赛事 / 题目 / URL）。
"""


def _guess_category(user_input: str) -> str | None:
    lowered = user_input.lower()
    mapping = {
        "web": ("web", "xss", "sqli", "ssti", "ssrf", "csrf", "php"),
        "pwn": ("pwn", "rop", "heap", "ret2libc", "format string", "pwntools"),
        "crypto": ("crypto", "rsa", "xor", "aes", "lfsr", "lattice"),
        "reverse": ("reverse", "rev", "ghidra", "ida", "angr"),
        "forensics": ("forensics", "pcap", "memory dump", "volatility", "wireshark"),
        "osint": ("osint", "google dork", "metadata leak"),
        "misc": ("misc", "jail", "pyjail", "stego", "automation"),
    }
    for category, keywords in mapping.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return None


def _handle_tool(name: str, inputs: dict[str, Any]) -> str:
    if name == "search_writeups":
        hits = retrieve(
            inputs["query"],
            top_k=inputs.get("top_k", cfg.top_k),
            category=inputs.get("category"),
            difficulty=inputs.get("difficulty"),
            year=inputs.get("year"),
        )
        return format_hits(hits)

    if name == "filter_search_writeups":
        hits = retrieve_filtered(
            inputs["query"],
            SearchFilters(
                event=inputs.get("event"),
                task=inputs.get("task"),
                category=inputs.get("category"),
                difficulty=inputs.get("difficulty"),
                year=inputs.get("year"),
                top_k=inputs.get("top_k", cfg.top_k),
            ),
        )
        return format_hits(hits)

    return f"[未知工具: {name}]"


def _retrieve_hits(user_input: str):
    guessed_category = _guess_category(user_input)
    return retrieve_filtered(
        user_input,
        SearchFilters(
            category=guessed_category,
            top_k=cfg.top_k,
        ),
    )


def _build_local_kb_context(user_input: str) -> str:
    guessed_category = _guess_category(user_input)
    primary = _handle_tool(
        "search_writeups",
        {"query": user_input, "top_k": cfg.top_k, "category": guessed_category},
    )
    filtered = _handle_tool(
        "filter_search_writeups",
        {
            "query": user_input,
            "top_k": max(2, min(cfg.top_k, 3)),
            "category": guessed_category,
        },
    )
    return (
        f"## 各大 CTF WP 检索结果 (category={guessed_category or 'auto-all'})\n"
        f"{primary}\n\n"
        "## 各大 CTF WP 补充检索\n"
        f"{filtered}"
    ).strip()


def _get_shared_llm():
    from config import load_config
    from llm.provider import create_llm_from_config

    app_cfg = load_config()
    role = (cfg.llm_role or "advisor").strip().lower()
    if role not in {"advisor", "main"}:
        role = "advisor"
    return create_llm_from_config(app_cfg.llm, role=role)


def _has_local_llm() -> bool:
    return bool(str(getattr(cfg, "local_llm_base_url", "") or "").strip() and str(getattr(cfg, "local_llm_model", "") or "").strip())


def _get_local_llm():
    from llm.provider import create_openai

    return create_openai(
        getattr(cfg, "local_llm_base_url", ""),
        getattr(cfg, "local_llm_api_key", "offline"),
        getattr(cfg, "local_llm_model", ""),
    )


def _render_extractive_answer(user_input: str, hits) -> str:
    if not hits:
        return (
            f"问题：{user_input}\n"
            "本地知识库里没有找到足够接近的 writeup 证据。"
        )

    lines = [f"问题：{user_input}", "本地离线模式已启用，以下是基于知识库命中的抽取式总结：", ""]
    for index, hit in enumerate(hits[: max(1, min(cfg.top_k, 3))], 1):
        lines.append(
            f"{index}. {hit.event} / {hit.task or hit.title} / {hit.category} / {hit.difficulty} / {hit.year}"
        )
        lines.append(f"   关键片段：{hit.content[:220]}")
        lines.append(f"   来源：{hit.url}")
    return "\n".join(lines).strip()


def _invoke_llm(user_input: str, llm, kb_context: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    messages = [
        SystemMessage(content=f"{SYSTEM_PROMPT}\n\n{kb_context}"),
        HumanMessage(
            content=(
                "请基于上面的本地知识库检索结果回答。\n"
                "如果知识库证据不足，要明确指出不足点，不要编造。\n\n"
                f"用户问题：{user_input}"
            )
        ),
    ]
    response = llm.invoke(messages)
    return str(getattr(response, "content", "") or "")


def run_agent(user_input: str, stream_print: bool = True) -> str:
    hits = _retrieve_hits(user_input)
    offline_mode = bool(getattr(cfg, "offline_mode", False))
    if offline_mode and not _has_local_llm():
        answer = _render_extractive_answer(user_input, hits)
        if stream_print:
            print(answer)
        return answer

    kb_context = _build_local_kb_context(user_input)
    llm = _get_local_llm() if offline_mode and _has_local_llm() else _get_shared_llm()
    final_answer = _invoke_llm(user_input, llm, kb_context)
    if stream_print:
        print(final_answer)
    return final_answer


def chat_loop() -> None:
    print("CTF Multi-Category Writeup Agent  (输入 q/quit 退出)\n")
    while True:
        try:
            q = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break
        if not q:
            continue
        if q.lower() in ("q", "quit", "exit"):
            break
        run_agent(q, stream_print=True)
        print()
