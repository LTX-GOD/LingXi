from __future__ import annotations

import asyncio
import sys
import types
import unittest

langchain_core_module = types.ModuleType("langchain_core")
language_models_module = types.ModuleType("langchain_core.language_models")
language_models_module.BaseChatModel = object
messages_module = types.ModuleType("langchain_core.messages")
messages_module.HumanMessage = type("HumanMessage", (), {"__init__": lambda self, content: setattr(self, "content", content)})
langchain_openai_module = types.ModuleType("langchain_openai")
langchain_openai_module.ChatOpenAI = object
sys.modules.setdefault("langchain_core", langchain_core_module)
sys.modules.setdefault("langchain_core.language_models", language_models_module)
sys.modules.setdefault("langchain_core.messages", messages_module)
sys.modules.setdefault("langchain_openai", langchain_openai_module)

from llm.provider import _EndpointGate, _FAIL_THRESHOLD, _ThrottledRunnable


class _StubRunnable:
    def __init__(self, responses):
        self._responses = list(responses)
        self.invoke_calls = 0
        self.ainvoke_calls = 0

    async def ainvoke(self, *args, **kwargs):
        self.ainvoke_calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def invoke(self, *args, **kwargs):
        self.invoke_calls += 1
        if not self._responses:
            return {"ok": True}
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def bind_tools(self, *args, **kwargs):
        return self


class LLMFailoverTests(unittest.TestCase):
    def test_auth_error_switches_to_fallback_without_probe(self) -> None:
        primary = _ThrottledRunnable(
            _StubRunnable([Exception("HTTP 401 unauthorized")]),
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:primary",
        )
        fallback = _ThrottledRunnable(
            _StubRunnable([{"ok": True}]),
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:fallback",
        )
        primary._fallback = fallback

        result = asyncio.run(primary.ainvoke(["hello"]))

        self.assertEqual({"ok": True}, result)
        self.assertTrue(primary._using_fallback)
        self.assertFalse(primary._probe_enabled)
        self.assertIsNone(primary._probe_thread)

    def test_transport_error_reaches_threshold_then_switches_to_fallback(self) -> None:
        primary = _ThrottledRunnable(
            _StubRunnable([Exception("HTTP 524 timeout")] * _FAIL_THRESHOLD),
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:primary",
        )
        fallback = _ThrottledRunnable(
            _StubRunnable([{"ok": True}]),
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:fallback",
        )
        primary._fallback = fallback

        for _ in range(_FAIL_THRESHOLD - 1):
            with self.assertRaises(Exception):
                asyncio.run(primary.ainvoke(["hello"]))

        result = asyncio.run(primary.ainvoke(["hello"]))

        self.assertEqual({"ok": True}, result)
        self.assertTrue(primary._using_fallback)
        self.assertTrue(primary._probe_enabled)

    def test_fallback_transport_error_retries_once_and_stays_on_fallback(self) -> None:
        primary = _ThrottledRunnable(
            _StubRunnable([]),
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:primary",
        )
        fallback_runnable = _StubRunnable(
            [Exception("HTTP 524 timeout"), {"ok": True}]
        )
        fallback = _ThrottledRunnable(
            fallback_runnable,
            _EndpointGate(concurrency=1, min_interval=0),
            label="main:fallback",
        )
        primary._fallback = fallback
        primary._using_fallback = True
        primary._probe_enabled = False

        result = asyncio.run(primary.ainvoke(["hello"]))

        self.assertEqual({"ok": True}, result)
        self.assertTrue(primary._using_fallback)
        self.assertEqual(2, fallback_runnable.ainvoke_calls)


if __name__ == "__main__":
    unittest.main()
