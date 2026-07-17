"""Offline tests for the wording chunker (ported from the old repo's
`test_wording_chunker.py`, 2R.4): section-aware structure/lineage on the
synthetic wording twin, paragraph-block fallback when no headings exist, and
the synchronous index_wording write path that replaced the old
indexing_pending drain. Pure stdlib, no LLM, no pypdf."""

import json
import tempfile
import unittest
from pathlib import Path

from records.query.wording_chunker import (
    MAX_CHUNK_WORDS,
    chunk_wording,
    chunks_path,
    index_wording,
)

FIXTURE_PATH = Path(__file__).parent.parent / "examples" / "motor_policy_wording.txt"


def _load_fixture() -> str:
    return FIXTURE_PATH.read_text(encoding="utf-8")


class ChunkWordingStructureTests(unittest.TestCase):
    def setUp(self):
        self.chunks = chunk_wording(_load_fixture(), "doc-abc", "doc-abc")

    def _find(self, section_ref):
        return next(c for c in self.chunks if c["section_ref"] == section_ref)

    def test_every_chunk_carries_doc_id_and_wording_version(self):
        self.assertTrue(self.chunks)
        for c in self.chunks:
            self.assertEqual(c["doc_id"], "doc-abc")
            self.assertEqual(c["wording_version"], "doc-abc")

    def test_chunk_ids_are_unique(self):
        ids = [c["chunk_id"] for c in self.chunks]
        self.assertEqual(len(ids), len(set(ids)))

    def test_covered_and_not_covered_are_separate_chunks(self):
        covered = self._find("1.1")
        not_covered = self._find("1.2")
        self.assertNotEqual(covered["text"], not_covered["text"])
        self.assertIn("windscreen", covered["text"].lower())
        self.assertNotIn("aftermarket tinting", covered["text"].lower())
        self.assertIn("aftermarket tinting", not_covered["text"].lower())

    def test_heading_carries_parent_section_lineage(self):
        covered = self._find("1.1")
        self.assertIn("Section 1", covered["heading"])
        self.assertIn("Windscreen", covered["heading"])
        self.assertIn("What is covered", covered["heading"])

    def test_driving_other_cars_section_present_and_distinct_from_business_use(self):
        doc = self._find("2.1")
        business = self._find("3.1")
        self.assertIn("driving other cars", doc["heading"].lower())
        self.assertIn("business use", business["heading"].lower())
        self.assertNotEqual(doc["section_ref"], business["section_ref"])

    def test_all_eight_sections_represented(self):
        refs = {c["section_ref"] for c in self.chunks if c["section_ref"]}
        section_numbers = {ref.split(".")[0] for ref in refs}
        self.assertEqual(section_numbers, {str(n) for n in range(1, 9)})

    def test_no_chunk_exceeds_max_word_target(self):
        for c in self.chunks:
            self.assertLessEqual(len(c["text"].split()), MAX_CHUNK_WORDS)

    def test_preamble_before_first_heading_has_no_section_ref(self):
        preamble = next(c for c in self.chunks if c["section_ref"] is None)
        self.assertIn("policy wording", preamble["text"].lower())


class ChunkWordingFallbackTests(unittest.TestCase):
    def test_no_headings_falls_back_to_paragraph_grouping(self):
        text = "Short intro paragraph.\n\n" + ("word " * 150) + "\n\n" + ("foo " * 150)
        chunks = chunk_wording(text, "doc-x", "doc-x")
        self.assertTrue(chunks)
        for c in chunks:
            self.assertIsNone(c["section_ref"])
            self.assertIsNone(c["heading"])

    def test_short_paragraphs_are_merged_toward_target(self):
        text = "\n\n".join(f"Paragraph number {i} with a handful of words in it." for i in range(10))
        chunks = chunk_wording(text, "doc-y", "doc-y")
        # ten ~10-word paragraphs (~100 words) should merge into one chunk, not ten.
        self.assertLess(len(chunks), 10)

    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(chunk_wording("", "doc-z", "doc-z"), [])

    def test_single_giant_paragraph_with_no_blank_lines_is_split_under_max_words(self):
        # PDF-extracted text often has no blank lines at all —
        # `_split_paragraphs` alone then returns one multi-thousand-word blob.
        text = ". ".join(f"Sentence number {i} says something about cover" for i in range(300)) + "."
        chunks = chunk_wording(text, "doc-giant", "doc-giant")
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text"].split()), MAX_CHUNK_WORDS)

    def test_dense_text_with_no_sentence_boundaries_is_hard_split(self):
        text = "word " * 1000
        chunks = chunk_wording(text, "doc-dense", "doc-dense")
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c["text"].split()), MAX_CHUNK_WORDS)


class ChunkWordingHeadingRegexTests(unittest.TestCase):
    """Real IPID documents use unnumbered question headings ("What is
    insured?") instead of "Section N" — detection extends to match (ported
    old-repo realfix.2(c) regression)."""

    def test_ipid_style_question_headings_are_detected(self):
        text = (
            "What is this type of insurance?\n"
            + ("This is a motor insurance product. " * 30) + "\n"
            "What is insured?\n"
            + ("Damage to your vehicle is covered. " * 30) + "\n"
            "What is not insured?\n"
            + ("Wear and tear is not covered. " * 30)
        )
        chunks = chunk_wording(text, "doc-ipid", "doc-ipid")
        headings = [c["heading"] for c in chunks]
        refs = [c["section_ref"] for c in chunks]
        self.assertIn("What is insured?", headings)
        self.assertIn("What is not insured?", headings)
        self.assertTrue(all(ref is not None for ref in refs))

    def test_front_matter_before_first_numbered_section_is_chunked_under_max_words(self):
        # Un-sectioned front matter must still end up in some chunk
        # (section_ref=None is fine — silently dropping it isn't), and that
        # chunk must still respect MAX_CHUNK_WORDS like any other.
        preamble = ". ".join(f"Welcome paragraph {i} about your policy" for i in range(200)) + "."
        text = preamble + "\nSection 1: Liability to other people\n" + ("Cover details. " * 20)
        chunks = chunk_wording(text, "doc-front", "doc-front")
        preamble_chunks = [c for c in chunks if c["section_ref"] is None]
        self.assertTrue(preamble_chunks)
        recovered = " ".join(c["text"] for c in preamble_chunks)
        self.assertIn("Welcome paragraph 0", recovered)
        self.assertIn("Welcome paragraph 199", recovered)
        for c in preamble_chunks:
            self.assertLessEqual(len(c["text"].split()), MAX_CHUNK_WORDS)


class IndexWordingTests(unittest.TestCase):
    """The synchronous write path `pipeline.ingest` calls for reference_text
    documents (the old async indexing_pending drain has no equivalent here —
    classification happens at ingest, so indexing does too)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_chunks_are_appended_to_chunk_store(self):
        chunks = index_wording(_load_fixture(), "doc-abc", root=self.root)
        self.assertGreater(len(chunks), 0)
        lines = chunks_path(self.root).read_text().strip().splitlines()
        self.assertEqual(len(lines), len(chunks))
        first = json.loads(lines[0])
        self.assertEqual(first["doc_id"], "doc-abc")
        self.assertIn("section_ref", first)
        self.assertIn("text", first)

    def test_reindexing_same_version_is_a_no_op(self):
        first = index_wording(_load_fixture(), "doc-abc", root=self.root)
        again = index_wording(_load_fixture(), "doc-abc", root=self.root)
        self.assertEqual(again, [])
        lines = chunks_path(self.root).read_text().strip().splitlines()
        self.assertEqual(len(lines), len(first))  # no duplicate chunks

    def test_empty_text_writes_nothing(self):
        self.assertEqual(index_wording("", "doc-empty", root=self.root), [])
        self.assertFalse(chunks_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
