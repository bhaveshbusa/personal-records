"""Phase 4 structured read-only MCP protocol and trust boundary."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from records.core import Field, PolicyFiled, RenewalProposed, append
from records.mcp import create_server
from records.mcp.server import SERVER_INSTRUCTIONS
from records.query.wording_chunker import index_wording
from records.review import queue
from records.store import put_document, update_document

EXAMPLES = Path(__file__).parent.parent / "examples"

REGISTERED_TOOLS = {
    "list_records",
    "get_record",
    "get_renewals",
    "compare_quotes",
    "find_missing_info",
    "get_provenance",
    "search_policy_wording",
}
WRITE_TOOL_NAMES = {
    "ingest",
    "confirm",
    "reject",
    "discard",
    "append_event",
    "update_document",
    "delete_document",
}


class McpProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        document, _ = put_document(
            b"Synthetic renewal quote: motor premium GBP 378.90",
            "synthetic-renewal.txt",
            media_type="text/plain",
            root=self.root,
        )
        self.doc_id = document["doc_id"]
        update_document(self.doc_id, doc_type="renewal_quote", root=self.root)

        policy_document, _ = put_document(
            b"Synthetic policy schedule: policy SYN-42, premium GBP 350.00",
            "synthetic-policy.txt",
            media_type="text/plain",
            root=self.root,
        )
        self.policy_doc_id = policy_document["doc_id"]
        self.entity_id = "SYN-42"
        append(
            PolicyFiled(
                doc_id=self.policy_doc_id,
                doc_type="policy_schedule",
                entity_id=self.entity_id,
                fields={
                    "policy_number": Field("SYN-42", 0.99, "policy SYN-42", 1),
                    "annual_premium": Field(350.00, 0.96, "premium GBP 350.00", 1),
                },
                provider="Synthetic Mutual",
            ),
            root=self.root,
        )
        append(
            RenewalProposed(
                doc_id=self.doc_id,
                product="motor",
                annual_premium=378.90,
                provenance=Field(378.90, 0.95, "premium GBP 378.90", 1),
                renewal_date="2026-10-14",
                entity_id=self.entity_id,
            ),
            root=self.root,
        )

        append(
            RenewalProposed(
                doc_id="missing-date-evidence",
                product="home",
                annual_premium=210.00,
                provenance=Field(210.00, 0.93, "premium GBP 210.00", 1),
                renewal_date=None,
            ),
            root=self.root,
        )
        self.pending_doc_id = "pending-review-evidence"
        queue.add(
            self.pending_doc_id,
            ["shape: unsure"],
            None,
            root=self.root,
        )

    async def test_protocol_lists_exact_read_tool_set(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            response = await session.list_tools()

        names = {tool.name for tool in response.tools}
        self.assertEqual(names, REGISTERED_TOOLS)
        self.assertTrue(names.isdisjoint(WRITE_TOOL_NAMES))

    async def test_server_and_wording_tool_instructions_keep_interpretation_in_host(self):
        self.assertIn("evidence is not a coverage verdict", SERVER_INSTRUCTIONS)
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            response = await session.list_tools()

        wording_tool = next(
            tool for tool in response.tools if tool.name == "search_policy_wording"
        )
        self.assertIn("evidence", wording_tool.description)
        self.assertIn("not a coverage verdict", wording_tool.description)

    async def test_protocol_rejects_every_write_capability_name(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=False
        ) as session:
            results = [await session.call_tool(name, {}) for name in WRITE_TOOL_NAMES]

        self.assertTrue(all(result.isError for result in results))

    def _index_wording(self, text: str, file_name: str = "synthetic-wording.txt") -> str:
        document, _ = put_document(
            text.encode(), file_name, media_type="text/plain", root=self.root
        )
        update_document(
            document["doc_id"],
            doc_type="policy_wording",
            chunk_count=len(index_wording(text, document["doc_id"], root=self.root)),
            root=self.root,
        )
        return document["doc_id"]

    async def test_list_records_and_get_record_use_current_policy_entity_id(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            listed = await session.call_tool("list_records", {})
            fetched = await session.call_tool("get_record", {"entity_id": self.entity_id})

        list_payload = listed.structuredContent
        self.assertTrue(list_payload["found"])
        self.assertEqual(list_payload["sources"], [self.policy_doc_id])
        record = list_payload["records"][0]
        self.assertEqual(record["entity_id"], self.entity_id)
        self.assertEqual(record["doc_id"], self.policy_doc_id)
        self.assertEqual(record["trust"], "extracted")
        self.assertEqual(record["fields"]["annual_premium"]["value"], 350.00)

        get_payload = fetched.structuredContent
        self.assertEqual(get_payload["identifier_type"], "entity_id")
        self.assertEqual(get_payload["entity_id"], self.entity_id)
        self.assertEqual(get_payload["record"], record)
        self.assertEqual(get_payload["sources"], [self.policy_doc_id])

    async def test_get_record_unknown_entity_is_explicit_not_found(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("get_record", {"entity_id": "UNKNOWN"})

        self.assertEqual(
            result.structuredContent,
            {
                "found": False,
                "identifier_type": "entity_id",
                "entity_id": "UNKNOWN",
                "record": None,
                "sources": [],
            },
        )

    async def test_empty_store_responses_are_explicit(self):
        empty_root = self.root / "empty-store"
        async with create_connected_server_and_client_session(
            create_server(root=empty_root), raise_exceptions=True
        ) as session:
            records = await session.call_tool("list_records", {})
            missing = await session.call_tool("find_missing_info", {})

        self.assertEqual(
            records.structuredContent,
            {"found": False, "records": [], "sources": []},
        )
        self.assertEqual(
            missing.structuredContent,
            {
                "found": False,
                "record_set_empty": True,
                "gaps": [],
                "pending_review": [],
                "sources": [],
            },
        )

    async def test_get_renewals_returns_structured_projection_and_full_source_id(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("get_renewals", {"product": " MOTOR "})

        payload = result.structuredContent
        self.assertIsNotNone(payload)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["rows"][0]["state"], "RenewalProposed")
        self.assertEqual(payload["rows"][0]["annual_premium"], 378.90)
        self.assertEqual(payload["rows"][0]["doc_id"], self.doc_id)
        self.assertEqual(payload["sources"], [self.doc_id])

    async def test_compare_quotes_reuses_projection_math_with_both_evidence_ids(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("compare_quotes", {"product": " MOTOR "})

        payload = result.structuredContent
        self.assertTrue(payload["found"])
        offer = payload["offers"][0]
        self.assertEqual(offer["doc_id"], self.doc_id)
        self.assertEqual(offer["current_policy_doc_id"], self.policy_doc_id)
        self.assertEqual(offer["premium_change"]["delta"], 28.90)
        self.assertEqual(offer["premium_change"]["pct_change"], 8.3)
        self.assertEqual(offer["trust"], "extracted")
        self.assertEqual(set(payload["sources"]), {self.doc_id, self.policy_doc_id})

    async def test_compare_quotes_empty_filter_is_explicit(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("compare_quotes", {"product": "boat"})

        self.assertEqual(
            result.structuredContent,
            {"found": False, "offers": [], "sources": []},
        )

    async def test_find_missing_info_combines_gap_logic_and_pending_review(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("find_missing_info", {})

        payload = result.structuredContent
        self.assertTrue(payload["found"])
        self.assertFalse(payload["record_set_empty"])
        self.assertEqual(payload["gaps"][0]["doc_id"], "missing-date-evidence")
        self.assertEqual(payload["gaps"][0]["trust"], "extracted")
        self.assertEqual(payload["pending_review"][0]["doc_id"], self.pending_doc_id)
        self.assertEqual(
            set(payload["sources"]), {"missing-date-evidence", self.pending_doc_id}
        )

    async def test_get_provenance_exposes_metadata_and_events_but_not_raw_storage(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("get_provenance", {"doc_id": self.doc_id})

        payload = result.structuredContent
        self.assertIsNotNone(payload)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["document"]["file_name"], "synthetic-renewal.txt")
        self.assertEqual(payload["document"]["doc_type"], "renewal_quote")
        self.assertNotIn("storage_path", payload["document"])
        self.assertNotIn("content", payload["document"])
        self.assertEqual(payload["events"][0]["event_type"], "RenewalProposed")
        self.assertEqual(payload["events"][0]["data"]["doc_id"], self.doc_id)

    async def test_get_provenance_unknown_id_is_explicit_not_found(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool("get_provenance", {"doc_id": "unknown"})

        self.assertEqual(
            result.structuredContent,
            {"found": False, "doc_id": "unknown", "document": None, "events": []},
        )

    async def test_search_policy_wording_returns_bounded_exact_clause_evidence(self):
        wording_text = """POLICY WORDING

Section 1 — Glass cover
1.1 Windscreen damage
We will pay to repair or replace your windscreen if it is cracked, chipped, or shattered.

1.2 Glass exclusions
We will not pay for scratches that do not affect visibility.
"""
        wording_doc_id = self._index_wording(wording_text)

        old_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self.addCleanup(
            lambda: os.environ.__setitem__("ANTHROPIC_API_KEY", old_key)
            if old_key is not None
            else None
        )
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording", {"question": "cracked windscreen"}
            )

        payload = result.structuredContent
        self.assertTrue(payload["found"])
        self.assertIsNone(payload["reason"])
        self.assertEqual(payload["sources"], [wording_doc_id])
        self.assertLessEqual(len(payload["clauses"]), 4)
        clause = payload["clauses"][0]
        self.assertEqual(
            set(clause),
            {
                "doc_id",
                "chunk_id",
                "section_ref",
                "heading",
                "page",
                "clause_text",
                "score",
            },
        )
        self.assertEqual(clause["doc_id"], wording_doc_id)
        self.assertTrue(clause["chunk_id"].startswith(f"{wording_doc_id}-"))
        self.assertEqual(clause["section_ref"], "1.1")
        self.assertIn("Windscreen damage", clause["heading"])
        self.assertEqual(clause["page"], 1)
        self.assertEqual(
            clause["clause_text"],
            "We will pay to repair or replace your windscreen if it is cracked, chipped, or shattered.",
        )
        self.assertGreater(clause["score"], 0.0)
        self.assertNotIn("verdict", payload)
        self.assertNotIn("storage_path", repr(payload))
        self.assertNotIn("file_name", repr(payload))

    async def test_search_policy_wording_fails_closed_without_wording(self):
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording", {"question": "cracked windscreen"}
            )

        self.assertEqual(
            result.structuredContent,
            {
                "found": False,
                "reason": "no_wording_on_file",
                "clauses": [],
                "sources": [],
            },
        )

    async def test_search_policy_wording_fails_closed_without_relevant_clause(self):
        wording_doc_id = self._index_wording(
            "Section 1 — Windscreens\n1.1 Glass\nWe repair cracked windscreens."
        )
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording", {"question": "jetski mooring damage"}
            )

        self.assertEqual(
            result.structuredContent,
            {
                "found": False,
                "reason": "no_relevant_clause",
                "clauses": [],
                "sources": [wording_doc_id],
            },
        )

    async def test_full_wording_fixture_returns_only_substantive_windscreen_evidence(self):
        wording_doc_id = self._index_wording(
            (EXAMPLES / "motor_policy_wording.txt").read_text(encoding="utf-8")
        )
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording",
                {
                    "question": "What policy-wording clauses are relevant to a cracked windscreen?"
                },
            )

        payload = result.structuredContent
        self.assertTrue(payload["found"])
        self.assertEqual(payload["sources"], [wording_doc_id])
        self.assertTrue(payload["clauses"])
        self.assertTrue(
            any(clause["section_ref"] == "1.1" for clause in payload["clauses"])
        )
        self.assertTrue(
            all(
                "motor insurance policy wording"
                not in clause["clause_text"].lower()
                for clause in payload["clauses"]
            )
        )

    async def test_full_wording_fixture_rejects_exact_jetski_question(self):
        wording_doc_id = self._index_wording(
            (EXAMPLES / "motor_policy_wording.txt").read_text(encoding="utf-8")
        )
        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording",
                {
                    "question": "What policy-wording clauses cover jetski mooring damage?"
                },
            )

        self.assertEqual(
            result.structuredContent,
            {
                "found": False,
                "reason": "no_relevant_clause",
                "clauses": [],
                "sources": [wording_doc_id],
            },
        )

    async def test_search_policy_wording_uses_latest_non_superseded_document(self):
        superseded_doc_id = self._index_wording(
            "Section 1 — Bicycles\n1.1 Theft\nWe cover bicycle theft.",
            "old-wording.txt",
        )
        current_doc_id = self._index_wording(
            "Section 1 — Bicycles\n1.1 Theft\nWe exclude bicycle theft.",
            "current-wording.txt",
        )
        update_document(
            superseded_doc_id, superseded_by=current_doc_id, root=self.root
        )

        async with create_connected_server_and_client_session(
            create_server(root=self.root), raise_exceptions=True
        ) as session:
            result = await session.call_tool(
                "search_policy_wording", {"question": "bicycle theft"}
            )

        payload = result.structuredContent
        self.assertTrue(payload["found"])
        self.assertEqual(payload["sources"], [current_doc_id])
        self.assertTrue(
            all(clause["doc_id"] == current_doc_id for clause in payload["clauses"])
        )
        self.assertIn("exclude bicycle theft", payload["clauses"][0]["clause_text"])

    async def test_records_mcp_cli_serves_tools_over_stdio(self):
        env = os.environ.copy()
        env["PERSONAL_RECORDS_HOME"] = str(self.root)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "records.cli.main", "mcp"],
            env=env,
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("get_renewals", {"product": "motor"})

        self.assertEqual({tool.name for tool in tools.tools}, REGISTERED_TOOLS)
        self.assertEqual(result.structuredContent["sources"], [self.doc_id])


if __name__ == "__main__":
    unittest.main()
