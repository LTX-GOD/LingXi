from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ctf_kb.rag.retriever import SearchFilters
from ctf_kb.vector import factory as vector_factory


class RetrieverAndVectorTests(unittest.TestCase):
    def test_search_filters_support_year(self) -> None:
        filters = SearchFilters(category="crypto", difficulty="hard", year=2024, top_k=7)

        self.assertEqual(2024, filters.year)
        self.assertEqual(7, filters.limit())

    def test_qdrant_route_uses_shared_collection_for_long_tail_categories(self) -> None:
        shared = vector_factory.resolve_qdrant_bucket("reverse")
        direct = vector_factory.resolve_qdrant_bucket("web")

        self.assertEqual("shared", shared)
        self.assertEqual("web", direct)

    def test_factory_uses_configured_backend(self) -> None:
        with patch.object(vector_factory, "cfg", SimpleNamespace(vector_backend="milvus")):
            store = vector_factory.get_vector_store()
        self.assertEqual("ctf_kb.vector.milvus_store", store.__name__)


if __name__ == "__main__":
    unittest.main()
