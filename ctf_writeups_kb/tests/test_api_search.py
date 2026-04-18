from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
CTF_WRITEUPS_SRC = Path(__file__).resolve().parents[1] / "src"
for candidate in (str(ROOT), str(CTF_WRITEUPS_SRC)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

try:
    import fastapi  # noqa: F401
except ModuleNotFoundError:
    fastapi_stub = types.ModuleType("fastapi")

    class _DummyFastAPI:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

    class _DummyHTTPException(Exception):
        def __init__(self, status_code: int, detail: str):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    def _dummy_query(default=None, **kwargs):
        return default

    fastapi_stub.FastAPI = _DummyFastAPI
    fastapi_stub.HTTPException = _DummyHTTPException
    fastapi_stub.Query = _dummy_query
    sys.modules["fastapi"] = fastapi_stub

try:
    import pydantic  # noqa: F401
except ModuleNotFoundError:
    pydantic_stub = types.ModuleType("pydantic")

    class _DummyBaseModel:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    pydantic_stub.BaseModel = _DummyBaseModel
    sys.modules["pydantic"] = pydantic_stub

from ctf_kb.api import app as api_module
from ctf_kb.models import SearchHit
from memory.knowledge_store import (
    KNOWLEDGE_BUCKET_MAIN,
    KnowledgeRecord,
    KnowledgeSearchHit,
)


class ApiSearchTests(unittest.TestCase):
    def test_search_passes_year_filter(self) -> None:
        hit = SearchHit(
            chunk_id="c1",
            writeup_id="w1",
            event="DEFCON CTF Quals",
            task="baby-note",
            title="Baby Note",
            url="https://example.com/writeup",
            chunk_index=0,
            score=0.99,
            content="demo",
            category="pwn",
            difficulty="hard",
            year=2024,
        )
        with patch.object(api_module, "retrieve_filtered", return_value=[hit]) as retrieve_filtered:
            response = api_module.search(
                q="baby-note",
                top_k=3,
                event=None,
                task=None,
                category="pwn",
                difficulty="hard",
                year=2024,
            )

        filters = retrieve_filtered.call_args.args[1]
        self.assertEqual(2024, filters.year)
        self.assertEqual(2024, response["results"][0]["year"])

    def test_search_reads_local_experience_bucket(self) -> None:
        record = KnowledgeRecord(
            record_id="rec-1",
            created_at="2026-04-12T00:00:00",
            bucket=KNOWLEDGE_BUCKET_MAIN,
            source_type="main_battle",
            outcome_type="success",
            scope_key="scope-1",
            challenge_code="web-100",
            zone="z1",
            category="web",
            summary="use default credentials to reach admin panel",
            confidence=0.92,
            quality_score=0.88,
            verification_state="verified",
        )
        hit = KnowledgeSearchHit(record=record, score=12.0)
        with patch.object(api_module, "search_knowledge_records", return_value=[hit]) as search_records:
            response = api_module.search(
                q="default credential admin",
                top_k=3,
                bucket=KNOWLEDGE_BUCKET_MAIN,
                source_type="main_battle",
                outcome_type="success",
            )

        self.assertEqual(KNOWLEDGE_BUCKET_MAIN, response["results"][0]["source"])
        self.assertEqual("main_battle", response["results"][0]["source_type"])
        self.assertEqual("success", response["results"][0]["outcome_type"])
        self.assertEqual("web-100", response["results"][0]["challenge_code"])
        search_records.assert_called_once()

    def test_experience_ingest_persists_local_record(self) -> None:
        payload = api_module.ExperienceIngestRequest(
            bucket=KNOWLEDGE_BUCKET_MAIN,
            record={
                "record_id": "rec-2",
                "created_at": "2026-04-12T00:00:00",
                "bucket": KNOWLEDGE_BUCKET_MAIN,
                "source_type": "main_battle",
                "outcome_type": "success",
                "scope_key": "scope-2",
                "challenge_code": "web-200",
                "summary": "summary",
            },
        )
        with patch.object(api_module, "get_knowledge_store") as get_store:
            response = api_module.experience_ingest(payload)

        self.assertEqual("ok", response["status"])
        self.assertEqual("rec-2", response["record_id"])
        get_store.return_value.ingest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
