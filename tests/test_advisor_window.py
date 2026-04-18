from __future__ import annotations

import ast
import unittest
from pathlib import Path


class HumanMessage:
    def __init__(self, content: str):
        self.content = content


class AIMessage:
    def __init__(self, content: str):
        self.content = content


class ToolMessage:
    def __init__(self, content: str, tool_call_id: str, name: str):
        self.content = content
        self.tool_call_id = tool_call_id
        self.name = name


def _load_graph_helpers():
    module = ast.parse(Path("agent/graph.py").read_text(encoding="utf-8"))
    selected: list[ast.AST] = []
    wanted = {
        "_normalize_llm_content",
        "_truncate_tool_text",
        "_extract_latest_main_agent_decision",
        "_extract_latest_tool_result",
    }
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            selected.append(node)

    namespace = {
        "AIMessage": AIMessage,
        "ToolMessage": ToolMessage,
        "Any": object,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "graph_helpers", "exec"), namespace)
    return (
        namespace["_extract_latest_main_agent_decision"],
        namespace["_extract_latest_tool_result"],
    )


_extract_latest_main_agent_decision, _extract_latest_tool_result = _load_graph_helpers()


class AdvisorWindowTests(unittest.TestCase):
    def test_extract_latest_main_agent_decision_prefers_latest_ai_message(self) -> None:
        state = {
            "messages": [
                HumanMessage(content="task"),
                AIMessage(content="old decision"),
                ToolMessage(content="tool output", tool_call_id="1", name="execute_command"),
                AIMessage(content="new decision"),
            ]
        }

        result = _extract_latest_main_agent_decision(state)

        self.assertIn("new decision", result)
        self.assertNotIn("old decision", result)

    def test_extract_latest_tool_result_prefers_latest_tool_message(self) -> None:
        state = {
            "messages": [
                ToolMessage(content="first output", tool_call_id="1", name="execute_command"),
                AIMessage(content="decision"),
                ToolMessage(content="second output", tool_call_id="2", name="execute_python"),
            ]
        }

        result = _extract_latest_tool_result(state)

        self.assertIn("execute_python", result)
        self.assertIn("second output", result)
        self.assertNotIn("first output", result)


if __name__ == "__main__":
    unittest.main()
