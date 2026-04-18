"""
Ling-Xi Console — Rich 终端美化
===============================
使用 Rich 库提供精美的终端输出。

设计风格:
- 赛博朋克渐变色 (紫→蓝→青)
- Panel 框架包裹关键信息
- Table 展示结构化数据
- Status 动画展示进度
"""
import sys
import logging
from typing import Optional, TextIO

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.columns import Columns
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskID
from rich.markup import escape
from rich.theme import Theme

logger = logging.getLogger(__name__)

# ─── Ling-Xi 主题 ───
LINGXI_THEME = Theme({
    "lingxi.title": "bold bright_cyan",
    "lingxi.success": "bold green",
    "lingxi.error": "bold red",
    "lingxi.warning": "bold yellow",
    "lingxi.info": "bold blue",
    "lingxi.dim": "dim white",
    "lingxi.highlight": "bold magenta",
    "lingxi.zone": "bold bright_yellow",
    "lingxi.flag": "bold bright_green on black",
})

_console: Optional[Console] = None


def get_console() -> Console:
    global _console
    if _console is None:
        _console = Console(theme=LINGXI_THEME)
    return _console


def init_console_with_log(log_file: TextIO) -> Console:
    """初始化支持文件日志的 Console"""

    class DualWriter:
        def __init__(self, *targets):
            self.targets = targets

        def write(self, text):
            for t in self.targets:
                try:
                    t.write(text)
                    t.flush()
                except (BrokenPipeError, ValueError, OSError):
                    continue

        def flush(self):
            for t in self.targets:
                try:
                    t.flush()
                except (BrokenPipeError, ValueError, OSError):
                    continue

    writer = DualWriter(sys.stdout, log_file)
    is_tty = getattr(sys.stdout, 'isatty', lambda: False)()

    global _console
    _console = Console(file=writer, theme=LINGXI_THEME, force_terminal=is_tty)
    return _console


# ─── Ling-Xi Banner ───

BANNER = r"""
[bright_cyan]
  ██╗     ██╗███╗   ██╗ ██████╗       ██╗  ██╗██╗
  ██║     ██║████╗  ██║██╔════╝       ╚██╗██╔╝██║
  ██║     ██║██╔██╗ ██║██║  ███╗█████╗ ╚███╔╝ ██║
  ██║     ██║██║╚██╗██║██║   ██║╚════╝ ██╔██╗ ██║
  ███████╗██║██║ ╚████║╚██████╔╝      ██╔╝ ██╗██║
  ╚══════╝╚═╝╚═╝  ╚═══╝ ╚═════╝       ╚═╝  ╚═╝╚═╝
[/bright_cyan]
[dim]   Autonomous Penetration Testing Intelligence[/dim]
"""


def print_banner():
    """打印 Ling-Xi 启动 Banner"""
    c = get_console()
    c.print(BANNER)
    c.print()


def print_config_table(config):
    """打印配置信息表格"""
    c = get_console()
    table = Table(title="⚙️  Ling-Xi Configuration", border_style="bright_cyan", show_header=True, header_style="bold bright_cyan")
    table.add_column("Setting", style="dim", width=25)
    table.add_column("Value", style="white")

    table.add_row("Platform URL", config.platform.base_url or "[dim]not set[/dim]")
    if getattr(config.platform, "api_base_url", "") and config.platform.api_base_url != config.platform.base_url:
        table.add_row("Platform API", config.platform.api_base_url)
    table.add_row("Forum URL", config.forum.server_host or "[dim]not set[/dim]")
    table.add_row("Forum Enabled", "✅" if config.forum.enabled else "❌")
    table.add_row("Sliver MCP", "✅" if config.sliver.enabled else "❌")
    if getattr(config.sliver, "client_config_path", ""):
        table.add_row("Sliver Config", config.sliver.client_config_path)
    table.add_row("Main LLM", f"[bold]{config.llm.main_provider}[/bold]")
    table.add_row("Advisor LLM", f"[bold]{config.llm.advisor_provider}[/bold]")
    if config.llm.forum_llm_model:
        table.add_row("Forum LLM", f"[bold]{config.llm.forum_llm_provider}[/bold] / {config.llm.forum_llm_model}")
    table.add_row("Docker Container", config.docker.container_name)
    table.add_row("Docker Enabled", "✅" if config.docker.enabled else "❌")
    table.add_row("Max Attempts/Challenge", str(config.agent.max_attempts))
    table.add_row("Max Concurrent Tasks", str(config.agent.max_concurrent_tasks))
    table.add_row("Task Timeout", f"{config.agent.single_task_timeout}s")
    table.add_row("Advisor Interval", f"Every {config.agent.advisor_consultation_interval} rounds")

    c.print(table)
    c.print()


def print_zone_status(zones_data: list):
    """
    打印赛区状态表格

    Args:
        zones_data: [(zone_name, unlocked, solved, total, score)]
    """
    c = get_console()
    table = Table(title="🏰 Zone Status", border_style="bright_yellow", show_header=True, header_style="bold bright_yellow")
    table.add_column("Zone", style="white", width=20)
    table.add_column("Status", justify="center", width=8)
    table.add_column("Progress", justify="center", width=12)
    table.add_column("Score", justify="right", width=8)

    for name, unlocked, solved, total, score in zones_data:
        lock = "[green]🔓[/green]" if unlocked else "[red]🔒[/red]"
        if total > 0:
            pct = int(solved / total * 100)
            progress = f"[{'green' if pct == 100 else 'yellow'}]{solved}/{total} ({pct}%)[/]"
        else:
            progress = "[dim]—[/dim]"
        table.add_row(name, lock, progress, str(score))

    c.print(table)
    c.print()


def print_challenge_start(module_id: str, challenge_name: str, difficulty: str, points: int, target: str):
    """打印题目开始攻击"""
    c = get_console()
    diff_color = {"easy": "green", "medium": "yellow", "hard": "red"}.get(difficulty, "white")
    title_block = (
        f"[bold white]Module:[/bold white] {module_id}\n"
        f"[bold white]Challenge:[/bold white] {challenge_name}\n"
        if challenge_name and challenge_name != module_id
        else f"[bold white]Module:[/bold white] {module_id}\n"
    )
    c.print(Panel(
        f"{title_block}"
        f"[bold white]Difficulty:[/bold white] [{diff_color}]{difficulty}[/{diff_color}]\n"
        f"[bold white]Points:[/bold white] {points}\n"
        f"[bold white]Target:[/bold white] {target}",
        title="[bold bright_cyan]🎯 ATTACKING[/bold bright_cyan]",
        border_style="bright_cyan",
        width=84,
    ))


def print_challenge_result(
    module_id: str,
    challenge_name: str,
    success: bool,
    attempts: int,
    elapsed: float,
    flag: str = "",
    payloads: Optional[list[str]] = None,
    action_summary: str = "",
    action_history: Optional[list[str]] = None,
    cleanup_status: str = "",
):
    """打印题目结果"""
    c = get_console()
    payload_lines = payloads[-5:] if payloads else []
    payload_block = "\n".join(
        f"  - {escape(item[:140])}" for item in payload_lines
    ) if payload_lines else "  - —"
    history_lines = action_history[-5:] if action_history else []
    history_block = "\n".join(
        f"  - {escape(item[:160])}" for item in history_lines
    ) if history_lines else "  - —"
    name_block = (
        f"[bold white]Module:[/bold white] {module_id}\n"
        f"[bold white]Challenge:[/bold white] {challenge_name}\n"
        if challenge_name and challenge_name != module_id
        else f"[bold white]Module:[/bold white] {module_id}\n"
    )

    if success:
        cleanup_line = (
            f"\n[bold green]Cleanup:[/bold green]   {escape(cleanup_status)}"
            if cleanup_status
            else ""
        )
        c.print(Panel(
            f"{name_block}"
            f"[bold green]Attempts:[/bold green]  {attempts}\n"
            f"[bold green]Time:[/bold green]      {elapsed:.0f}s\n"
            f"[bold green]Flag:[/bold green]      {escape(flag[:120]) if flag else '—'}"
            f"\n[bold green]Summary:[/bold green]   {escape(action_summary[:240]) if action_summary else '—'}"
            f"{cleanup_line}\n"
            f"[bold green]Actions:[/bold green]\n{history_block}\n"
            f"[bold green]Payloads:[/bold green]\n{payload_block}",
            title="[bold green]🎉 SOLVED[/bold green]",
            border_style="green",
            width=104,
        ))
    else:
        c.print(Panel(
            f"{name_block}"
            f"[bold red]Attempts:[/bold red]  {attempts}\n"
            f"[bold red]Time:[/bold red]      {elapsed:.0f}s\n"
            f"[bold red]Summary:[/bold red]   {escape(action_summary[:240]) if action_summary else '—'}\n"
            f"[bold red]Actions:[/bold red]\n{history_block}\n"
            f"[bold red]Payloads:[/bold red]\n{payload_block}",
            title="[bold red]❌ FAILED[/bold red]",
            border_style="red",
            width=104,
        ))


def print_advisor_suggestion(suggestion: str):
    """打印顾问建议"""
    c = get_console()
    # 截断过长的建议
    display = suggestion[:800] + "..." if len(suggestion) > 800 else suggestion
    c.print(Panel(
        escape(display),
        title="[bold magenta]🧠 Advisor Suggestion[/bold magenta]",
        border_style="magenta",
        width=80,
    ))


def print_tool_execution(module_id: str, tool_name: str, args_preview: str):
    """打印工具执行信息"""
    import logging as _logging
    c = get_console()
    icon = {"execute_command": "🖥️", "execute_python": "🐍", "submit_flag": "🚩"}.get(tool_name, "🔧")
    c.print(
        f"  ({escape(module_id)}) {icon} [bold]{tool_name}[/bold] "
        f"[dim]→ {escape(args_preview[:160])}[/dim]"
    )
    _logging.getLogger("console").debug("[Tool] %s | %s | %s", module_id, tool_name, args_preview[:200])


def print_flag_detected(flags: list):
    """打印自动检测到的 Flag"""
    c = get_console()
    c.print(Panel(
        "\n".join(f"[bold bright_green]{escape(f)}[/bold bright_green]" for f in flags),
        title="[bold bright_green]🚨 FLAG DETECTED[/bold bright_green]",
        border_style="bright_green",
        width=60,
    ))


def print_final_report(total_solved: int, total_score: int, elapsed: float):
    """打印最终报告"""
    c = get_console()
    c.print()
    c.print(Panel(
        f"[bold white]Total Solved:[/bold white]  {total_solved}\n"
        f"[bold white]Total Score:[/bold white]   {total_score}\n"
        f"[bold white]Runtime:[/bold white]       {elapsed:.0f}s",
        title="[bold bright_cyan]📊 Ling-Xi — Final Report[/bold bright_cyan]",
        border_style="bright_cyan",
        width=60,
    ))
    c.print()


def sanitize_text(text: str, max_len: int = 10000) -> str:
    """清理文本中的 Rich 标记字符"""
    if not text:
        return ""
    text = str(text)
    text = text.replace("[", "\\[").replace("]", "\\]")
    text = "".join(c for c in text if c.isprintable() or c in "\n\t\r")
    if len(text) > max_len:
        text = text[:max_len] + "\n\n... (truncated)"
    return text
