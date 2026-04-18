from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

from memory.knowledge_store import (
    KNOWLEDGE_BUCKET_FORUM,
    KNOWLEDGE_BUCKET_MAIN,
    KNOWLEDGE_BUCKET_EXTERNAL,
    KnowledgeStore,
    bucket_display_name,
    normalize_bucket,
)
from memory.knowledge_writeback import (
    build_knowledge_candidate,
    enqueue_knowledge_writeback,
    process_pending_knowledge_queue,
)
from memory.store import MemoryStore


class MemoryKnowledgeTests(unittest.TestCase):
    def test_bucket_aliases_normalize_to_three_modules(self) -> None:
        self.assertEqual(KNOWLEDGE_BUCKET_MAIN, normalize_bucket("lingxi_main_experience"))
        self.assertEqual(KNOWLEDGE_BUCKET_MAIN, normalize_bucket("main"))
        self.assertEqual(KNOWLEDGE_BUCKET_FORUM, normalize_bucket("lingxi_forum_experience"))
        self.assertEqual(KNOWLEDGE_BUCKET_FORUM, normalize_bucket("forum"))
        self.assertEqual(KNOWLEDGE_BUCKET_EXTERNAL, normalize_bucket("tou_external_writeups"))
        self.assertEqual(KNOWLEDGE_BUCKET_EXTERNAL, normalize_bucket("external"))
        self.assertEqual("主战场记忆", bucket_display_name(KNOWLEDGE_BUCKET_MAIN))
        self.assertEqual("论坛记忆", bucket_display_name(KNOWLEDGE_BUCKET_FORUM))
        self.assertEqual("各大 CTF WP", bucket_display_name(KNOWLEDGE_BUCKET_EXTERNAL))

    def test_build_knowledge_candidate_accepts_success_and_skips_timeout_failure(self) -> None:
        challenge = {
            "code": "web-100",
            "display_code": "web-100",
            "entrypoint": ["127.0.0.1:8080"],
            "category": "web",
        }
        success_result = {
            "success": True,
            "flag": "flag{real_flag}",
            "scored_flags": ["flag{real_flag}"],
            "payloads": ["admin:admin@127.0.0.1"],
            "action_history": [
                "发现 /login 可访问，默认口令 admin:admin 成功",
                "登录后进入 admin 页面并读取 flag",
            ],
            "final_strategy": "try default credential then browse admin",
        }
        candidate = build_knowledge_candidate(
            challenge,
            success_result,
            zone="z1",
            scope_key="scope-1",
        )
        self.assertIsNotNone(candidate)
        self.assertEqual("success", candidate.outcome_type)
        self.assertEqual(KNOWLEDGE_BUCKET_MAIN, candidate.bucket)
        self.assertIn("flag{real_flag}", candidate.verified_flags)
        self.assertEqual([], candidate.credentials)

        timeout_result = {
            "success": False,
            "error": "timeout",
            "action_history": ["等待超时，没有稳定响应"],
        }
        skipped = build_knowledge_candidate(
            challenge,
            timeout_result,
            zone="z1",
            scope_key="scope-2",
        )
        self.assertIsNone(skipped)

    def test_build_knowledge_candidate_does_not_extract_credentials_from_unstructured_text(self) -> None:
        challenge = {
            "code": "web-101",
            "display_code": "web-101",
            "entrypoint": ["127.0.0.1:8080"],
            "category": "web",
        }
        result = {
            "success": True,
            "flag": "flag{real_flag}",
            "scored_flags": ["flag{real_flag}"],
            "payloads": ["admin:admin@127.0.0.1"],
            "action_history": [
                "发现 /login 可访问，默认口令 admin:admin 成功",
                "username=admin password=admin",
            ],
            "recon_info_excerpt": "guest:guest@target.example",
        }

        candidate = build_knowledge_candidate(
            challenge,
            result,
            zone="z1",
            scope_key="scope-unstructured",
            memory_context="历史记录提到 root:toor@172.16.0.10",
        )

        self.assertIsNotNone(candidate)
        self.assertEqual([], candidate.credentials)

    def test_build_knowledge_candidate_accepts_explicit_structured_credentials_only(self) -> None:
        challenge = {
            "code": "web-102",
            "display_code": "web-102",
            "entrypoint": ["127.0.0.1:8080"],
            "category": "web",
        }
        result = {
            "success": True,
            "flag": "flag{real_flag}",
            "scored_flags": ["flag{real_flag}"],
            "action_history": ["发现后台接口并完成验证"],
            "credentials": [
                {
                    "host": "127.0.0.1",
                    "username": "admin",
                    "password": "admin",
                    "service": "http",
                },
                {
                    "host": "127.0.0.1",
                    "username": "admin",
                    "password": "admin",
                    "service": "http",
                },
            ],
        }

        candidate = build_knowledge_candidate(
            challenge,
            result,
            zone="z1",
            scope_key="scope-structured",
        )

        self.assertIsNotNone(candidate)
        self.assertEqual(
            [
                {
                    "host": "127.0.0.1",
                    "username": "admin",
                    "password": "admin",
                    "service": "http",
                }
            ],
            candidate.credentials,
        )

    def test_memory_store_shared_data_is_bucket_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(
                path=str(Path(tmp) / "memory.json"),
                wp_dir=str(Path(tmp) / "wp"),
            )
            store.add_discovery("z1", "main discovery", bucket=KNOWLEDGE_BUCKET_MAIN)
            store.add_discovery("z1", "forum discovery", bucket=KNOWLEDGE_BUCKET_FORUM)
            store.add_credential(
                "main-host",
                "alice",
                "main-pass",
                bucket=KNOWLEDGE_BUCKET_MAIN,
                zone="z1",
            )
            store.add_credential(
                "forum-host",
                "bob",
                "forum-pass",
                bucket=KNOWLEDGE_BUCKET_FORUM,
                zone="z1",
            )

            main_context = store.get_context_for_challenge(
                "web-100",
                "z1",
                knowledge_bucket=KNOWLEDGE_BUCKET_MAIN,
                challenge={"code": "web-100", "display_code": "web-100"},
            )
            forum_context = store.get_context_for_challenge(
                "forum-2",
                "z1",
                knowledge_bucket=KNOWLEDGE_BUCKET_FORUM,
                challenge={"code": "forum-2", "display_code": "forum-2", "forum_task": True},
            )

            self.assertIn("main discovery", main_context)
            self.assertNotIn("forum discovery", main_context)
            self.assertIn("main-pass", main_context)
            self.assertNotIn("forum-pass", main_context)

            self.assertIn("forum discovery", forum_context)
            self.assertNotIn("main discovery", forum_context)
            self.assertIn("forum-pass", forum_context)
            self.assertNotIn("main-pass", forum_context)

    def test_record_writeup_persists_prompt_and_runtime_traces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(
                path=str(Path(tmp) / "memory.json"),
                wp_dir=str(Path(tmp) / "wp"),
            )
            challenge = {
                "code": "web-201",
                "display_code": "web-201",
                "entrypoint": ["target-a.example:8080"],
                "category": "web",
                "difficulty": "easy",
                "total_score": 100,
            }
            result = {
                "success": True,
                "flag": "flag{wp-demo}",
                "attempts": 4,
                "elapsed": 12.5,
                "payloads": ["execute_python | requests.Session()"],
                "action_history": ["发现 /docs", "登录后台成功"],
                "final_strategy": "check docs then login",
                "thought_summary": "先看 docs，再登录后台拿 flag",
                "decision_history": ["先确认 /docs 是否暴露接口", "拿到接口后再登录后台"],
                "advisor_call_count": 1,
                "advisor_history": ["advisor#1 reason=no_tool_rounds=2 suggestion=先查看 /docs"],
                "advisor_summary": "advisor#1 reason=no_tool_rounds=2 suggestion=先查看 /docs",
                "knowledge_call_count": 1,
                "knowledge_history": ["kb#1 reason=no_tool_rounds=2 sources=main_memory hit=yes"],
                "knowledge_summary": "kb#1 reason=no_tool_rounds=2 sources=main_memory hit=yes",
                "system_prompt_excerpt": "system prompt payload",
                "initial_prompt_excerpt": "user prompt payload",
                "skill_context_excerpt": "skill excerpt",
            }

            store.record_writeup(
                challenge,
                result,
                zone="z1",
                scope_key="web-201",
                strategy_description="default",
                memory_context="memory excerpt",
            )

            markdown_path = Path(store._get_wp_markdown_path("web-201", scope_key="web-201"))
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("## Prompt Payloads", markdown)
            self.assertIn("## Thought History", markdown)
            self.assertIn("## Advisor Trace", markdown)
            self.assertIn("## Knowledge Trace", markdown)
            self.assertIn("Advisor Calls", markdown)
            self.assertIn("Knowledge Calls", markdown)

    def test_queue_processing_ingests_record_and_updates_shared_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_file = Path(tmp) / "queue.jsonl"
            queue_state = Path(tmp) / "queue_state.json"
            knowledge_root = Path(tmp) / "knowledge"
            memory_store = MemoryStore(
                path=str(Path(tmp) / "memory.json"),
                wp_dir=str(Path(tmp) / "wp"),
            )
            challenge = {
                "code": "web-300",
                "display_code": "web-300",
                "entrypoint": ["target-b.example:8080"],
                "category": "web",
            }
            result = {
                "success": True,
                "flag": "flag{queue_flag}",
                "scored_flags": ["flag{queue_flag}"],
                "payloads": ["admin:admin@target-b.example"],
                "action_history": [
                    "发现 /docs 暴露 OpenAPI 文档",
                    "使用 admin:admin 登录成功并读取 flag",
                ],
                "final_strategy": "check docs then login with default credential",
            }

            knowledge_store = KnowledgeStore(root=knowledge_root)
            knowledge_events: list[dict] = []
            knowledge_service_module = ModuleType("memory.knowledge_service")
            knowledge_service_module.knowledge_service_enabled = Mock(return_value=True)
            knowledge_service_module.ingest_knowledge_record = Mock(
                side_effect=RuntimeError("mirror down")
            )
            with patch("memory.knowledge_writeback._QUEUE_FILE", queue_file), patch(
                "memory.knowledge_writeback._QUEUE_STATE_FILE",
                queue_state,
            ), patch(
                "memory.knowledge_writeback.get_knowledge_store",
                return_value=knowledge_store,
            ), patch.dict(
                sys.modules,
                {"memory.knowledge_service": knowledge_service_module},
            ), patch(
                "memory.knowledge_writeback._emit_knowledge_updated",
                side_effect=lambda record: knowledge_events.append(
                    {
                        "bucket": record.bucket,
                        "challenge_code": record.challenge_code,
                        "record_id": record.record_id,
                    }
                ),
            ):
                queued = enqueue_knowledge_writeback(
                    challenge,
                    result,
                    zone="z1",
                    scope_key="scope-queue",
                )
                self.assertIsNotNone(queued)
                processed = process_pending_knowledge_queue(memory_store=memory_store)

            self.assertEqual(1, processed)
            stored = knowledge_store.load_bucket(KNOWLEDGE_BUCKET_MAIN)
            self.assertEqual(1, len(stored))
            self.assertTrue(
                any("docs" in item.lower() or "openapi" in item.lower() for item in stored[0].discoveries)
            )
            main_discoveries = memory_store.get_zone_discoveries(
                "z1",
                bucket=KNOWLEDGE_BUCKET_MAIN,
            )
            self.assertTrue(any("docs" in item.lower() for item in main_discoveries))
            self.assertEqual([], memory_store.get_credentials(bucket=KNOWLEDGE_BUCKET_MAIN, zone="z1"))
            self.assertTrue(queue_state.exists())
            self.assertEqual(1, json.loads(queue_state.read_text(encoding="utf-8"))["last_processed_line"])
            self.assertEqual(1, len(knowledge_events))
            self.assertEqual(KNOWLEDGE_BUCKET_MAIN, knowledge_events[0]["bucket"])

    def test_queue_processing_keeps_item_pending_when_local_ingest_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_file = Path(tmp) / "queue.jsonl"
            queue_state = Path(tmp) / "queue_state.json"
            memory_store = MemoryStore(
                path=str(Path(tmp) / "memory.json"),
                wp_dir=str(Path(tmp) / "wp"),
            )
            challenge = {
                "code": "web-301",
                "display_code": "web-301",
                "entrypoint": ["target-c.example:8080"],
                "category": "web",
            }
            result = {
                "success": True,
                "flag": "flag{queue_flag_2}",
                "scored_flags": ["flag{queue_flag_2}"],
                "action_history": ["发现 /admin 暴露后台"],
            }

            broken_store = Mock()
            broken_store.ingest.side_effect = RuntimeError("disk full")
            knowledge_service_module = ModuleType("memory.knowledge_service")
            knowledge_service_module.knowledge_service_enabled = Mock(return_value=True)
            knowledge_service_module.ingest_knowledge_record = Mock()
            with patch("memory.knowledge_writeback._QUEUE_FILE", queue_file), patch(
                "memory.knowledge_writeback._QUEUE_STATE_FILE",
                queue_state,
            ), patch(
                "memory.knowledge_writeback.get_knowledge_store",
                return_value=broken_store,
            ), patch.dict(
                sys.modules,
                {"memory.knowledge_service": knowledge_service_module},
            ), patch(
                "memory.knowledge_writeback._emit_knowledge_updated"
            ) as event_mock:
                queued = enqueue_knowledge_writeback(
                    challenge,
                    result,
                    zone="z1",
                    scope_key="scope-queue-2",
                )
                self.assertIsNotNone(queued)
                processed = process_pending_knowledge_queue(memory_store=memory_store)

            self.assertEqual(0, processed)
            self.assertFalse(queue_state.exists())
            self.assertFalse(memory_store.get_zone_discoveries("z1", bucket=KNOWLEDGE_BUCKET_MAIN))
            knowledge_service_module.ingest_knowledge_record.assert_not_called()
            event_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
