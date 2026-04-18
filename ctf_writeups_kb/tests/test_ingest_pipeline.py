from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ctf_kb.rag import ingest as ingest_module


class IngestPipelineTests(unittest.TestCase):
    def test_iter_raw_records_skips_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            raw_path = Path(tmp_dir) / "writeups.jsonl"
            raw_path.write_text(
                "\n".join(
                    [
                        json.dumps({"writeup_id": "ok-1", "content": "hello"}),
                        "{not json}",
                        json.dumps({"content": "missing id"}),
                    ]
                ),
                encoding="utf-8",
            )

            rows = list(ingest_module.iter_raw_records(raw_path))

        self.assertEqual(1, len(rows))
        self.assertEqual("ok-1", rows[0]["writeup_id"])

    def test_normalize_record_prefers_explicit_metadata_and_builds_slim_chunks(self) -> None:
        record = ingest_module.normalize_record(
            {
                "writeup_id": "demo-1",
                "event": "DEFCON CTF Quals 2024",
                "task": "baby-note",
                "title": "Baby Note",
                "content": "intro\n\n" + ("A" * 4000),
                "category": "crypto",
                "difficulty": "hard",
                "year": 2024,
                "team": "blue-water",
                "tags": ["heap", "glibc"],
                "techniques": ["heap exploitation"],
                "tools": ["pwntools"],
            }
        )

        chunks = ingest_module.build_chunks(record)

        self.assertEqual("crypto", record.category)
        self.assertEqual("hard", record.difficulty)
        self.assertEqual(2024, record.year)
        self.assertGreaterEqual(len(chunks), 1)
        self.assertLessEqual(len(chunks), ingest_module.max_chunks_per_writeup())
        self.assertTrue(all(chunk.tags == "[]" for chunk in chunks))
        self.assertTrue(all(chunk.techniques == "[]" for chunk in chunks))
        self.assertTrue(all(chunk.tools == "[]" for chunk in chunks))
        self.assertTrue(all(chunk.team == "" for chunk in chunks))

    def test_dedupe_record_prefers_writeup_id_then_source_url(self) -> None:
        seen_ids: set[str] = set()
        seen_urls: set[str] = set()
        seen_hashes: set[str] = set()
        first = ingest_module.normalize_record(
            {
                "writeup_id": "dup-1",
                "url": "https://example.com/a",
                "content": "payload",
            }
        )
        second = ingest_module.normalize_record(
            {
                "writeup_id": "dup-1",
                "url": "https://example.com/b",
                "content": "payload changed",
            }
        )

        self.assertTrue(ingest_module.dedupe_record(first, seen_ids, seen_urls, seen_hashes))
        self.assertFalse(ingest_module.dedupe_record(second, seen_ids, seen_urls, seen_hashes))


if __name__ == "__main__":
    unittest.main()

