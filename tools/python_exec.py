"""
Python PoC 执行工具
==================
在 Docker 或本地执行 Python 脚本。
用于多步漏洞利用、构造 payload、发送请求、解析响应。
"""
import asyncio
import os
import shlex
import tempfile
import logging
from langchain_core.tools import tool
from log_utils import describe_python_script
from tools.shell import (
    _execute,
    _truncate_output,
    get_runtime_python_command,
    run_shell_io,
    validate_execution_text,
)

logger = logging.getLogger(__name__)

SCRIPT_DIR = "/tmp/agent-scripts"


def _execute_python_impl(code: str, timeout: int = 60) -> str:
    """
    执行 Python 脚本（用于编写 exploit / PoC）。

    在隔离的执行环境中运行 Python 代码。
    适合用于多步漏洞利用：构造 payload → 发送请求 → 解析响应。

    **可用库：**
    - requests: HTTP 请求
    - pwntools: 二进制利用
    - pycryptodome: 加密/解密
    - beautifulsoup4: HTML 解析
    - socket, struct, base64, hashlib 等标准库

    **示例：**
    ```python
    import requests

    # SQL 注入检测
    url = "http://target/login"
    payloads = ["' OR 1=1--", "admin'--", "1' UNION SELECT 1,2,3--"]
    for p in payloads:
        r = requests.post(url, data={"username": p, "password": "x"})
        print(f"Payload: {p} -> Status: {r.status_code}, Len: {len(r.text)}")
    ```

    **注意：**
    - 代码会被写入临时文件再执行
    - 使用 print() 输出结果
    - 默认超时 120 秒

    Args:
        code: 完整的 Python 脚本代码
        timeout: 超时时间（秒），默认 120
    """
    if not code or not code.strip():
        return "错误：代码不能为空"

    policy_error = validate_execution_text(code, source="Python 代码", is_python=True)
    if policy_error:
        logger.warning("[Python] 拦截脚本: %s", policy_error)
        return f"错误：{policy_error}"

    logger.debug("[Python] 执行 Python 脚本: %s", describe_python_script(code))

    # 写入临时文件
    try:
        os.makedirs(SCRIPT_DIR, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.py', prefix='poc_', delete=False, dir=SCRIPT_DIR
        ) as f:
            f.write(code)
            script_path = f.name
    except Exception as e:
        return f"写入脚本失败: {e}"

    try:
        # 执行（通过统一执行器，支持 Docker / 本地）
        result = _execute(
            f"{get_runtime_python_command()} {shlex.quote(script_path)}",
            timeout=timeout,
        )

        output = f"Exit Code: {result.exit_code}\n"
        if result.stdout:
            output += f"\n--- OUTPUT ---\n{_truncate_output(result.stdout)}\n"
        if result.stderr:
            output += f"\n--- STDERR ---\n{_truncate_output(result.stderr)}\n"

        return output
    finally:
        # 清理临时文件
        try:
            os.unlink(script_path)
        except OSError:
            pass


@tool
async def execute_python(code: str, timeout: int = 60) -> str:
    """在 Kali 容器中执行 Python 脚本。

    适用场景（必须主动调用）：
    - 复杂 HTTP 请求：requests.Session() 处理 Cookie/JWT/多步认证
    - PoC/exploit 脚本：反序列化、SSTI、XXE、条件竞争
    - payload 构造：编码转换、加解密、JWT 伪造
    - 数据解析：响应体提取、正则匹配、JSON 处理
    - 当 curl shell quoting 失败时，立即切换到此工具

    示例：
        import requests
        r = requests.get('http://target/api', headers={'Authorization': 'Bearer xxx'})
        print(r.status_code, r.text[:500])
    """
    return await run_shell_io(_execute_python_impl, code, timeout)
