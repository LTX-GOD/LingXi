from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctf_kb.llm import claude_agent
from ctf_kb.models import SearchHit


class OfflineChatTests(unittest.TestCase):
    def test_run_agent_uses_extractive_answer_without_local_llm(self) -> None:
        fake_cfg = SimpleNamespace(
            offline_mode=True,
            top_k=3,
            llm_role="advisor",
            offline_answer_mode="auto",
            local_llm_base_url="",
            local_llm_model="",
        )
        hits = [
            SearchHit(
                chunk_id="chunk-1",
                writeup_id="writeup-1",
                event="DEFCON CTF Quals",
                task="baby-note",
                title="Baby Note",
                url="https://example.com/writeup",
                chunk_index=0,
                score=0.97,
                content="关键步骤：泄露 libc，伪造 tcache，最后 getshell。",
                category="pwn",
                difficulty="hard",
                year=2024,
            )
        ]

        with patch.object(claude_agent, "cfg", fake_cfg), patch.object(
            claude_agent, "retrieve_filtered", return_value=hits
        ), patch.object(claude_agent, "_get_shared_llm") as get_llm:
            answer = claude_agent.run_agent("怎么打 baby-note", stream_print=False)

        get_llm.assert_not_called()
        self.assertIn("DEFCON CTF Quals", answer)
        self.assertIn("https://example.com/writeup", answer)
        self.assertIn("关键步骤", answer)


if __name__ == "__main__":
    unittest.main()

