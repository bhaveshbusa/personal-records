"""Offline tests for the coverage answerer (ported from the old repo's
`test_coverage_answerer.py`, 2R.4): the deterministic citation trust-gate is
what these exist to prove — an LLM does not get to self-certify that its
evidence says what it claims. The LLM is always a FakeLLMClient here; the
old repo's injectable dict-returning callable became a JSON-string canned
response through the LLMClient port."""

import json
import unittest

from records.extract import FakeLLMClient
from records.query.coverage_answerer import (
    TRUST_INTERPRETED,
    VERDICT_CANNOT_DETERMINE,
    answer_coverage_question,
)

CHUNKS = [
    {
        "chunk_id": "doc-1.1", "doc_id": "doc", "wording_version": "doc", "section_ref": "1.1",
        "heading": "Section 1 — Windscreen / What is covered", "page": 1,
        "text": "We will pay for repair or replacement of your windscreen if it is chipped.",
    },
    {
        "chunk_id": "doc-1.2", "doc_id": "doc", "wording_version": "doc", "section_ref": "1.2",
        "heading": "Section 1 — Windscreen / What is not covered", "page": 1,
        "text": "We will not pay for glass damage that existed before your policy started.",
    },
]

POLICY = {"provider": "Aviva", "cover_level": "comprehensive"}


def _llm(response: dict) -> FakeLLMClient:
    return FakeLLMClient([json.dumps(response)])


class NoChunksTests(unittest.TestCase):
    def test_empty_chunks_refuses_without_calling_llm(self):
        llm = FakeLLMClient([])  # any call would pop from an empty list and raise
        out, response = answer_coverage_question("Am I covered for a jetski?", POLICY, [], llm)
        self.assertEqual(llm.calls, [])
        self.assertIsNone(response)
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["citations"], [])
        self.assertEqual(out["trust"], TRUST_INTERPRETED)
        self.assertEqual(out["refusal_reason"], "no_retrieval")


class ValidAnswerTests(unittest.TestCase):
    def test_valid_citation_passes_through(self):
        response = {
            "verdict": "covered",
            "answer": "Yes, windscreen chips are covered.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": [],
        }
        out, llm_response = answer_coverage_question(
            "Am I covered for a chipped windscreen?", POLICY, CHUNKS, _llm(response)
        )
        self.assertEqual(out["verdict"], "covered")
        self.assertEqual(out["citations"], response["citations"])
        self.assertEqual(out["trust"], TRUST_INTERPRETED)
        self.assertIsNone(out["refusal_reason"])
        self.assertIsNotNone(llm_response)  # caller gets the response for telemetry

    def test_conditions_pass_through(self):
        response = {
            "verdict": "conditional",
            "answer": "Depends on age.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": ["driver must be over 25"],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["conditions"], ["driver must be over 25"])

    def test_question_policy_and_chunks_reach_the_prompt(self):
        response = {
            "verdict": "covered",
            "answer": "Yes.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": [],
        }
        llm = _llm(response)
        answer_coverage_question("Am I covered for a chipped windscreen?", POLICY, CHUNKS, llm)
        sent = llm.calls[0]["user_content"]
        self.assertIn("chipped windscreen", sent)
        self.assertIn("Aviva", sent)
        self.assertIn("doc-1.1", sent)


class CitationTrustGateTests(unittest.TestCase):
    """The LLM proposes evidence; it doesn't get to self-certify it."""

    def test_fabricated_quote_is_caught_and_replaced_with_refusal(self):
        response = {
            "verdict": "covered",
            "answer": "Yes.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "this text is not in the chunk"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["citations"], [])
        self.assertEqual(out["refusal_reason"], "citation_check_failed")

    def test_citation_referencing_unknown_chunk_id_is_caught(self):
        response = {
            "verdict": "covered",
            "answer": "Yes.",
            "citations": [{"chunk_id": "doc-9.9", "section_ref": "9.9", "quote": "if it is chipped"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["refusal_reason"], "citation_check_failed")

    def test_one_bad_citation_among_several_rejects_the_whole_answer(self):
        response = {
            "verdict": "covered",
            "answer": "Yes.",
            "citations": [
                {"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"},
                {"chunk_id": "doc-1.2", "section_ref": "1.2", "quote": "fabricated text"},
            ],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["refusal_reason"], "citation_check_failed")

    def test_non_cannot_determine_verdict_with_no_citations_is_rejected(self):
        response = {"verdict": "covered", "answer": "Yes.", "citations": [], "conditions": []}
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["refusal_reason"], "citation_check_failed")

    def test_invalid_verdict_string_is_rejected(self):
        response = {
            "verdict": "probably",
            "answer": "Maybe.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["refusal_reason"], "invalid_verdict")

    def test_cannot_determine_verdict_forces_empty_citations_even_if_llm_sent_some(self):
        response = {
            "verdict": "cannot_determine",
            "answer": "Not addressed.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered for a jetski?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["citations"], [])
        self.assertEqual(out["refusal_reason"], "llm_cannot_determine")


class RefusalReasonTests(unittest.TestCase):
    """One test per distinguishable refusal cause — a fake LLM per failure
    mode, not just a re-check of the trust gate above."""

    def test_llm_cannot_determine_reason(self):
        response = {"verdict": "cannot_determine", "answer": "Not addressed.", "citations": [], "conditions": []}
        out, _ = answer_coverage_question("Am I covered for a jetski?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["refusal_reason"], "llm_cannot_determine")

    def test_invalid_verdict_reason(self):
        response = {
            "verdict": "definitely_maybe",
            "answer": "Unclear.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "if it is chipped"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["refusal_reason"], "invalid_verdict")

    def test_citation_check_failed_reason_for_fabricated_quote(self):
        response = {
            "verdict": "covered",
            "answer": "Yes.",
            "citations": [{"chunk_id": "doc-1.1", "section_ref": "1.1", "quote": "a fabricated, paraphrased quote"}],
            "conditions": [],
        }
        out, _ = answer_coverage_question("Am I covered?", POLICY, CHUNKS, _llm(response))
        self.assertEqual(out["refusal_reason"], "citation_check_failed")

    def test_no_retrieval_reason(self):
        out, _ = answer_coverage_question("Am I covered for a jetski?", POLICY, [], FakeLLMClient([]))
        self.assertEqual(out["refusal_reason"], "no_retrieval")

    def test_unparseable_response_reason(self):
        # New failure mode with the LLMClient port: the model rambles
        # instead of emitting JSON — same honest refusal, distinguishable.
        llm = FakeLLMClient(["I think you're probably covered, generally speaking..."])
        out, response = answer_coverage_question("Am I covered?", POLICY, CHUNKS, llm)
        self.assertEqual(out["verdict"], VERDICT_CANNOT_DETERMINE)
        self.assertEqual(out["refusal_reason"], "unparseable_response")
        self.assertIsNotNone(response)  # the call happened; telemetry still owed


if __name__ == "__main__":
    unittest.main()
