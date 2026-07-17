"""Phase 2R.4 gate, end-to-end and offline: ingest the synthetic wording
twin → chunks indexed at the pipeline's stored hook → `ask("am I covered
for a cracked windscreen?")` answers with a wording citation. Retrieval is
deterministic BM25; the two LLM calls (intent, coverage answer) are canned.
"""

import json
import tempfile
import unittest
from pathlib import Path

from records import pipeline
from records.core import Field, PolicyFiled, append, replay
from records.extract import FakeLLMClient
from records.query import ask
from records.query.wording_chunker import chunks_path
from records.store import get_document

EXAMPLES = Path(__file__).parent.parent / "examples"

DOC_TYPE_WORDING = json.dumps(
    {"doc_type": "policy_wording", "confidence": 0.95, "rationale": "policy wording sections"}
)


def intent_json(intent, product=None):
    return json.dumps({"intent": intent, "product": product})


class WordingIngestHookTest(unittest.TestCase):
    """pipeline.ingest, at the 'stored' outcome: reference_text docs get
    their doc_type persisted and their text chunked into the index."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_policy_wording_is_stored_and_indexed(self):
        llm = FakeLLMClient([DOC_TYPE_WORDING])
        result = pipeline.ingest(EXAMPLES / "motor_policy_wording.txt", llm, root=self.root)
        self.assertEqual(result.outcome, "stored")
        self.assertEqual(result.doc_type, "policy_wording")
        self.assertEqual(len(llm.calls), 1)  # classification only — indexing is zero-LLM
        self.assertEqual(replay(root=self.root), [])  # reference text emits no events

        doc = get_document(result.doc_id, root=self.root)
        self.assertEqual(doc["doc_type"], "policy_wording")
        self.assertGreater(doc["chunk_count"], 0)

        lines = chunks_path(self.root).read_text().strip().splitlines()
        self.assertEqual(len(lines), doc["chunk_count"])
        self.assertTrue(all(json.loads(l)["doc_id"] == result.doc_id for l in lines))

    def test_duplicate_wording_ingest_does_not_reindex(self):
        pipeline.ingest(EXAMPLES / "motor_policy_wording.txt", FakeLLMClient([DOC_TYPE_WORDING]), root=self.root)
        count = len(chunks_path(self.root).read_text().strip().splitlines())
        again = pipeline.ingest(EXAMPLES / "motor_policy_wording.txt", FakeLLMClient([]), root=self.root)
        self.assertEqual(again.outcome, "duplicate")
        self.assertEqual(len(chunks_path(self.root).read_text().strip().splitlines()), count)


class CoverageAskGateTest(unittest.TestCase):
    """The slice gate: a coverage question answers from the ingested
    wording with a citation — and refuses honestly when it can't."""

    QUESTION = "am I covered for a cracked windscreen?"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        result = pipeline.ingest(
            EXAMPLES / "motor_policy_wording.txt", FakeLLMClient([DOC_TYPE_WORDING]), root=self.root
        )
        self.wording_doc_id = result.doc_id

    def _windscreen_chunk(self) -> dict:
        for line in chunks_path(self.root).read_text().strip().splitlines():
            chunk = json.loads(line)
            if chunk["section_ref"] == "1.1":
                return chunk
        raise AssertionError("windscreen chunk not indexed")

    def test_gate_cracked_windscreen_answers_with_citation(self):
        chunk = self._windscreen_chunk()
        quote = "cracked, chipped, or shattered"
        self.assertIn(quote, chunk["text"])  # quote must be verbatim or the gate refuses
        coverage_response = json.dumps(
            {
                "verdict": "covered",
                "answer": "Yes — windscreen glass is covered if cracked, chipped or shattered.",
                "citations": [{"chunk_id": chunk["chunk_id"], "section_ref": "1.1", "quote": quote}],
                "conditions": [],
            }
        )
        llm = FakeLLMClient([intent_json("coverage", "motor"), coverage_response])
        answer = ask(self.QUESTION, llm, root=self.root)

        self.assertEqual(answer.intent, "coverage")
        self.assertIn("Yes", answer.text)
        self.assertIn("1.1", answer.text)  # cited section is visible
        self.assertIn(quote, answer.text)  # and so is the verbatim quote
        self.assertIn(self.wording_doc_id[:12], answer.text)  # provenance to the document
        self.assertEqual(answer.sources, (self.wording_doc_id,))
        # The coverage LLM call saw the retrieved windscreen clause.
        self.assertIn(chunk["chunk_id"], llm.calls[1]["user_content"])

    def test_current_policy_facts_reach_the_coverage_prompt(self):
        append(
            PolicyFiled(
                doc_id="sched-1",
                doc_type="policy_schedule",
                entity_id="POL-77",
                provider="SwiftSure Insurance Ltd",
                fields={"cover_level": Field("comprehensive", 0.95, "Comprehensive")},
            ),
            root=self.root,
        )
        chunk = self._windscreen_chunk()
        coverage_response = json.dumps(
            {
                "verdict": "covered",
                "answer": "Yes.",
                "citations": [
                    {"chunk_id": chunk["chunk_id"], "section_ref": "1.1", "quote": "cracked, chipped, or shattered"}
                ],
                "conditions": [],
            }
        )
        llm = FakeLLMClient([intent_json("coverage", "motor"), coverage_response])
        ask(self.QUESTION, llm, root=self.root)
        sent = llm.calls[1]["user_content"]
        self.assertIn("SwiftSure Insurance Ltd", sent)
        self.assertIn("comprehensive", sent)

    def test_fabricated_citation_becomes_refusal_end_to_end(self):
        chunk = self._windscreen_chunk()
        fabricated = json.dumps(
            {
                "verdict": "covered",
                "answer": "Yes, definitely.",
                "citations": [{"chunk_id": chunk["chunk_id"], "section_ref": "1.1", "quote": "totally invented wording"}],
                "conditions": [],
            }
        )
        answer = ask(self.QUESTION, FakeLLMClient([intent_json("coverage"), fabricated]), root=self.root)
        self.assertIn("can't find this in your policy wording", answer.text)
        self.assertEqual(answer.sources, ())

    def test_unrelated_question_refuses_before_the_coverage_llm_call(self):
        # BM25 retrieves nothing for a jetski — one canned response (intent)
        # is all the fake has; a second LLM call would pop an empty list.
        llm = FakeLLMClient([intent_json("coverage")])
        answer = ask("am I covered for a jetski?", llm, root=self.root)
        self.assertIn("can't find this in your policy wording", answer.text)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(answer.sources, ())

    def test_telemetry_records_both_llm_calls(self):
        chunk = self._windscreen_chunk()
        coverage_response = json.dumps(
            {
                "verdict": "covered",
                "answer": "Yes.",
                "citations": [
                    {"chunk_id": chunk["chunk_id"], "section_ref": "1.1", "quote": "cracked, chipped, or shattered"}
                ],
                "conditions": [],
            }
        )
        ask(self.QUESTION, FakeLLMClient([intent_json("coverage"), coverage_response]), root=self.root)
        telemetry = (self.root / "telemetry.jsonl").read_text().strip().splitlines()
        ops = [json.loads(l)["operation"] for l in telemetry]
        # setUp's wording ingest logged classify_doc_type; the ask adds two.
        self.assertEqual(ops[-2:], ["classify_intent", "answer_coverage"])


class NoWordingOnFileTest(unittest.TestCase):
    def test_coverage_question_without_wording_explains_what_to_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = FakeLLMClient([json.dumps({"intent": "coverage", "product": "motor"})])
            answer = ask("am I covered for a cracked windscreen?", llm, root=root)
            self.assertEqual(answer.intent, "coverage")
            self.assertIn("No policy wording on file", answer.text)
            self.assertEqual(len(llm.calls), 1)  # no coverage LLM call without wording


if __name__ == "__main__":
    unittest.main()
