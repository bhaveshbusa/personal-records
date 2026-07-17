"""Offline tests for BM25 wording retrieval (ported from the old repo's
`test_wording_index.py`, 2R.4): ranking on the synthetic wording twin, the
refusal-triggering empty result, version filtering, and k limiting. Pure
stdlib, no LLM, no embeddings."""

import tempfile
import unittest
from pathlib import Path

from records.query.wording_chunker import _append_chunks, chunk_wording, chunks_path
from records.query.wording_index import search

FIXTURE_PATH = Path(__file__).parent.parent / "examples" / "motor_policy_wording.txt"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


class WordingIndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        path = chunks_path(self.root)
        _append_chunks(chunk_wording(_load_fixture(), "wv-1", "wv-1"), path)
        # A second wording version present in the same file, to prove
        # search() only ever ranks within the requested version.
        _append_chunks(chunk_wording(_load_fixture(), "wv-2", "wv-2"), path)

    def test_windscreen_chip_ranks_windscreen_section_first(self):
        results = search("windscreen chip", "wv-1", root=self.root)
        self.assertTrue(results)
        top_chunk, top_score = results[0]
        self.assertEqual(top_chunk["section_ref"], "1.1")
        self.assertGreater(top_score, 0)

    def test_driving_other_cars_ranks_doc_section_first(self):
        results = search("driving other cars", "wv-1", root=self.root)
        self.assertTrue(results)
        top_chunk, _ = results[0]
        self.assertTrue(top_chunk["section_ref"].startswith("2."))

    def test_unrelated_query_returns_empty_list(self):
        results = search("am I covered for a jetski", "wv-1", root=self.root)
        self.assertEqual(results, [])

    def test_strict_search_requires_two_distinct_substantive_terms(self):
        results = search(
            "What policy-wording clauses cover jetski mooring damage?",
            "wv-1",
            root=self.root,
            strict=True,
        )
        self.assertEqual(results, [])

    def test_strict_search_preserves_one_substantive_term_queries(self):
        results = search("windscreen", "wv-1", root=self.root, strict=True)
        self.assertTrue(results)
        self.assertIn("windscreen", results[0][0]["heading"].lower())

    def test_strict_search_rejects_boilerplate_only_queries(self):
        results = search(
            "What policy wording clauses are relevant to coverage?",
            "wv-1",
            root=self.root,
            strict=True,
        )
        self.assertEqual(results, [])

    def test_non_strict_search_retains_existing_bm25_only_behaviour(self):
        results = search(
            "What policy-wording clauses cover jetski mooring damage?",
            "wv-1",
            root=self.root,
        )
        self.assertTrue(results)
        self.assertIsNone(results[0][0]["section_ref"])

    def test_empty_query_returns_empty_list(self):
        results = search("", "wv-1", root=self.root)
        self.assertEqual(results, [])

    def test_results_are_scoped_to_requested_wording_version(self):
        results = search("windscreen chip", "wv-2", root=self.root)
        self.assertTrue(results)
        for chunk, _ in results:
            self.assertEqual(chunk["wording_version"], "wv-2")

    def test_unknown_wording_version_returns_empty_list(self):
        results = search("windscreen chip", "no-such-version", root=self.root)
        self.assertEqual(results, [])

    def test_k_limits_result_count(self):
        results = search("cover excess claim", "wv-1", k=2, root=self.root, min_score_floor=0)
        self.assertLessEqual(len(results), 2)

    def test_results_are_sorted_descending_by_score(self):
        results = search("no claims discount protection", "wv-1", root=self.root)
        scores = [score for _, score in results]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_missing_chunks_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as empty_root:
            results = search("windscreen chip", "wv-1", root=Path(empty_root))
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
