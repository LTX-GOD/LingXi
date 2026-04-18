"""
自动侦察模块
============
Agent 启动前自动收集目标信息。
"""

import logging
import os
import tempfile
import textwrap
from typing import List

from challenge_fingerprints import detect_product_fingerprints, fingerprint_attack_hints
from tools.shell import _execute, get_dddd2_command, get_runtime_python_command

logger = logging.getLogger(__name__)


def _format_fingerprint_sections(source_text: str) -> list[str]:
    labels = detect_product_fingerprints(source_text)
    if not labels:
        return []
    sections = [f"**产品指纹:** {', '.join(labels)}"]
    hints = fingerprint_attack_hints(labels, limit=4)
    if hints:
        sections.append("**指纹打法:**\n" + "\n".join(f"- {item}" for item in hints))
    return sections


def _format_command_result(stdout: str, stderr: str, exit_code: int) -> str:
    parts = [f"Exit Code: {exit_code}"]
    normalized_stdout = (stdout or "").strip()
    normalized_stderr = (stderr or "").strip()
    if normalized_stdout:
        parts.append(f"--- STDOUT ---\n{normalized_stdout}")
    if normalized_stderr:
        parts.append(f"--- STDERR ---\n{normalized_stderr}")
    return "\n\n".join(parts)


def _run_full_dddd2_scan(ip: str, timeout: int = 30) -> str:
    """启动时先对整机做一次完整 dddd2 侦察，并把原始结果直接交给模型。"""
    dddd2_result = _execute(
        f"{get_dddd2_command()} -t {ip} -Pn -npoc",
        timeout=timeout,
    )
    rendered = _format_command_result(
        dddd2_result.stdout,
        dddd2_result.stderr,
        dddd2_result.exit_code,
    )
    fingerprint_sections = _format_fingerprint_sections(rendered)
    sections: list[str] = []
    if fingerprint_sections:
        sections.extend(fingerprint_sections)
    sections.append(f"### 全量 dddd2 扫描\n```\n{rendered[:12000]}\n```")
    return "\n\n".join(sections)


def auto_recon(ip: str, ports: List[int], timeout: int = 30) -> str:
    """
    对目标进行自动侦察，返回格式化的结果。

    Args:
        ip: 目标 IP
        ports: 端口列表
        timeout: 超时
    """
    results = [_run_full_dddd2_scan(ip, timeout)]

    seen_ports: set[int] = set()
    for port in ports:
        if port in seen_ports:
            continue
        seen_ports.add(port)
        result = _recon_single_port(ip, port, timeout, include_dddd2_fallback=False)
        results.append(f"### 端口 {port}\n{result}")

    return "\n\n".join(results)


def _recon_single_port(
    ip: str,
    port: int,
    timeout: int = 30,
    *,
    include_dddd2_fallback: bool = True,
) -> str:
    """侦察单个端口"""
    sections = []
    http_probe_succeeded = False
    fingerprint_sections: list[str] = []

    # 1. HTTP 首页获取与智能提纯 (避免被超大JS/CSS污染 Context)
    url = f"http://{ip}:{port}"
    with tempfile.TemporaryDirectory(prefix=f"recon_{ip.replace('.', '_')}_{port}_") as temp_dir:
        headers_path = os.path.join(temp_dir, "headers.txt")
        body_raw_path = os.path.join(temp_dir, "body_raw.txt")
        body_clean_path = os.path.join(temp_dir, "body_clean.txt")
        extractor_path = os.path.join(temp_dir, "extract.py")
        py_extractor = textwrap.dedent(
            f"""
            import json
            import re
            from pathlib import Path
            try:
                from bs4 import BeautifulSoup
                headers = Path({headers_path!r}).read_text(encoding="utf-8", errors="ignore")
                body = Path({body_raw_path!r}).read_text(encoding="utf-8", errors="ignore")
                lowered_headers = headers.lower()
                stripped = body.lstrip()

                signals = []
                extra_lines = []

                if "application/json" in lowered_headers:
                    signals.append("json-content-type")

                if stripped.startswith(("{{", "[")):
                    signals.append("json-body")
                    try:
                        parsed = json.loads(body)
                        if isinstance(parsed, dict):
                            keys = list(parsed.keys())[:12]
                            extra_lines.append(f"**JSON Keys:** {{', '.join(keys) if keys else '(empty object)'}}")
                        elif isinstance(parsed, list):
                            extra_lines.append(f"**JSON Shape:** list(len={{len(parsed)}})")
                            if parsed and isinstance(parsed[0], dict):
                                keys = list(parsed[0].keys())[:12]
                                extra_lines.append(f"**JSON Item Keys:** {{', '.join(keys) if keys else '(empty object)'}}")
                    except Exception as json_err:
                        extra_lines.append(f"**JSON Parse:** failed ({{json_err}})")

                soup = BeautifulSoup(body, "html.parser")
                for tag in soup(["script", "style", "svg", "noscript"]):
                    tag.extract()

                title = soup.title.string.strip() if soup.title and soup.title.string else "No Title"
                text = soup.get_text(separator=" ", strip=True)
                links = [a.get("href") for a in soup.find_all("a", href=True)][:10]

                forms = []
                login_like = False
                for form in soup.find_all("form")[:4]:
                    method = (form.get("method") or "GET").upper()
                    action = form.get("action") or "/"
                    field_desc = []
                    has_password = False
                    has_user = False
                    for field in form.find_all(["input", "textarea", "select"])[:8]:
                        name = field.get("name") or field.get("id") or field.get("type") or "field"
                        field_type = (field.get("type") or "text").lower()
                        if field_type == "password":
                            has_password = True
                        if re.search(r"(user|email|login|account|name)", name, re.I):
                            has_user = True
                        field_desc.append(f"{{name}}:{{field_type}}")
                    button_text = " ".join(
                        node.get_text(" ", strip=True) for node in form.find_all(["button"])
                    )
                    form_summary = f"action={{action}} method={{method}} fields={{', '.join(field_desc) if field_desc else '(none)'}}"
                    if button_text:
                        form_summary += f" buttons={{button_text[:80]}}"
                    forms.append(form_summary)
                    if has_password or has_user or re.search(r"(login|sign in|log in|signin|auth)", form_summary, re.I):
                        login_like = True

                if forms:
                    extra_lines.append("**Forms:**")
                    extra_lines.extend(f"- {{item}}" for item in forms)
                if login_like:
                    signals.append("login-form")
                if any(token in lowered_headers for token in ("set-cookie:", "www-authenticate:")):
                    signals.append("auth-signal")

                if signals:
                    extra_lines.insert(0, f"**Signals:** {{', '.join(signals)}}")

                print(f"**Title:** {{title}}")
                print(f"**Links:** {{', '.join(links)}}")
                print(f"**Text Preview:** {{text[:2000]}}")
                for line in extra_lines:
                    print(line)
            except Exception as e:
                print(f"HTML Parse Error: {{e}}")
            """
        )
        with open(extractor_path, "w", encoding="utf-8") as fh:
            fh.write(py_extractor)

        curl_result = _execute(
            f"curl -sS -L --max-time {timeout} -D {headers_path!r} -o {body_raw_path!r} '{url}' && "
            f"{get_runtime_python_command()} {extractor_path!r} > {body_clean_path!r} && "
            f"echo '---HEADERS---' && cat {headers_path!r} && "
            f"echo '---BODY---' && cat {body_clean_path!r}",
            timeout=timeout + 5,
        )

        if curl_result.exit_code == 0 and curl_result.stdout:
            http_probe_succeeded = True
            output = curl_result.stdout

            # 分离 headers 和 body
            if "---HEADERS---" in output:
                parts = output.split("---HEADERS---", 1)
                rest = parts[1] if len(parts) > 1 else ""

                if "---BODY---" in rest:
                    headers_part, body_part = rest.split("---BODY---", 1)
                    combined_text = f"{headers_part.strip()}\n{body_part.strip()}"
                    fingerprint_sections = _format_fingerprint_sections(combined_text)
                    sections.append(f"**响应头:**\n```\n{headers_part.strip()[:1000]}\n```")
                    sections.append(
                        f"**内容提纯 (关键文本):**\n```\n{body_part.strip()}\n```"
                    )
                else:
                    sections.append(f"**响应:**\n```\n{rest.strip()[:1500]}\n```")
            else:
                sections.append(f"**输出:**\n```\n{output[:1500]}\n```")
        else:
            sections.append(f"⚠️ HTTP 请求失败: {curl_result.stderr[:500]}")

    # 2. 轻量端口探测（仅在 HTTP 基线失败时兜底）
    #    启动前不要同步跑 -sV 这种重版本探测，避免把主流程卡死在前置侦察阶段。
    #    如果后续确实需要深度指纹识别，让主攻手显式调用 execute_command 跑完整 nmap。
    if include_dddd2_fallback and not http_probe_succeeded:
        dddd2_result = _execute(
            f"{get_dddd2_command()} -t {ip}:{port} -Pn -npoc 2>/dev/null | head -30",
            timeout=timeout,
        )
        if dddd2_result.exit_code == 0 and dddd2_result.stdout.strip():
            if not fingerprint_sections:
                fingerprint_sections = _format_fingerprint_sections(dddd2_result.stdout.strip())
            sections.append(
                f"**dddd2 探测:**\n```\n{dddd2_result.stdout.strip()[:2000]}\n```"
            )

    if fingerprint_sections:
        sections = fingerprint_sections + sections

    return "\n".join(sections) if sections else "⚠️ 侦察失败"
