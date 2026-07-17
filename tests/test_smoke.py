"""Smoke tests: package imports and CLI behave (offline, no keys)."""

import json
import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from records.cli.main import _build_parser, main
from records.extract import FakeLLMClient

EXAMPLES = Path(__file__).parent.parent / "examples"


class HomeSandbox(unittest.TestCase):
    """Point PERSONAL_RECORDS_HOME at a tmp dir for every CLI test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        os.environ["PERSONAL_RECORDS_HOME"] = str(self.home)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: os.environ.pop("PERSONAL_RECORDS_HOME", None))


class TestSmoke(HomeSandbox):
    def test_package_imports(self):
        import records
        import records.core
        import records.extract
        import records.pipeline
        import records.store
        import records.review
        import records.query
        import records.mcp  # noqa: F401

    def test_cli_no_args_shows_help(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([]), 0)
        help_text = output.getvalue()
        self.assertIn("records ingest examples/motor_policy_schedule.txt", help_text)
        self.assertIn("PERSONAL_RECORDS_HOME", help_text)
        self.assertIn("ANTHROPIC_API_KEY", help_text)

    def test_review_actions_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as raised, redirect_stderr(io.StringIO()):
            _build_parser().parse_args(["review", "--confirm", "one", "--reject", "two"])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_mcp_starts_read_only_server(self):
        with patch("records.mcp.run") as run:
            self.assertEqual(main(["mcp"]), 0)
        run.assert_called_once_with()

    def test_cli_ask_without_key_fails_helpfully(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        self.assertEqual(main(["ask", "when does my car insurance renew?"]), 2)

    def test_cli_ingest_without_key_fails_helpfully(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        doc = self.home / "sample.txt"
        doc.write_text("synthetic sample document")
        self.assertEqual(main(["ingest", str(doc)]), 2)

    def test_cli_ingest_missing_file_errors(self):
        self.assertEqual(main(["ingest", "/no/such/file.pdf"]), 2)

    def test_cli_review_and_renewals_empty_states(self):
        self.assertEqual(main(["review"]), 0)
        self.assertEqual(main(["renewals"]), 0)

    def test_cli_review_confirm_unknown_doc_errors(self):
        self.assertEqual(main(["review", "--confirm", "nope"]), 2)


class TestCliEndToEnd(HomeSandbox):
    """Full CLI walk with a fake LLM injected below the CLI layer."""

    def test_review_queue_walk(self):
        from records import pipeline

        doc_type = json.dumps(
            {"doc_type": "renewal_quote", "confidence": 0.95, "rationale": "renewal invitation"}
        )
        shape = json.dumps(
            {"line_count": 2, "renewal_status": "already_accepted", "unsure": False, "rationale": "bundle"}
        )
        extraction = json.dumps(
            {
                "lines": [
                    {"product": "motor", "annual_premium": {"value": 412.50, "confidence": 0.95, "source_text": "£412.50", "source_page": 1}},
                    {"product": "home", "annual_premium": {"value": 238.20, "confidence": 0.95, "source_text": "£238.20", "source_page": 1}},
                ],
                "stated_total": {"value": 650.70, "confidence": 0.95, "source_text": "£650.70", "source_page": 1},
            }
        )
        result = pipeline.ingest(
            EXAMPLES / "multicover_renewal_invitation.txt",
            FakeLLMClient([doc_type, shape, extraction]),
        )
        self.assertEqual(result.outcome, "review")
        self.assertEqual(main(["review"]), 0)  # lists the pending item
        self.assertEqual(main(["review", "--confirm", result.doc_id]), 0)
        self.assertEqual(main(["renewals"]), 0)  # calendar now renders rows

    def test_readme_happy_path_from_schedule_to_answer(self):
        doc_type_schedule = json.dumps(
            {"doc_type": "policy_schedule", "confidence": 0.95, "rationale": "schedule"}
        )
        schedule = json.dumps(
            {
                "fields": {
                    "policy_number": {"value": "SM-0000042", "confidence": 0.95, "source_text": "Policy number: SM-0000042", "source_page": 1},
                    "policy_end_date": {"value": "2026-10-14", "confidence": 0.95, "source_text": "to 14 October 2026", "source_page": 1},
                    "annual_premium": {"value": 352.40, "confidence": 0.95, "source_text": "£352.40", "source_page": 1},
                    "vehicle_registration": {"value": "XY19 ZAB", "confidence": 0.95, "source_text": "registration XY19 ZAB", "source_page": 1},
                    "provider": {"value": "SwiftSure Insurance Ltd", "confidence": 0.95, "source_text": "SwiftSure Insurance Ltd", "source_page": 1},
                }
            }
        )
        doc_type_quote = json.dumps(
            {"doc_type": "renewal_quote", "confidence": 0.95, "rationale": "renewal quote"}
        )
        shape = json.dumps(
            {"line_count": 1, "renewal_status": "proposed", "unsure": False, "rationale": "one motor line"}
        )
        quote = json.dumps(
            {
                "lines": [
                    {
                        "product": "motor",
                        "annual_premium": {"value": 378.90, "confidence": 0.95, "source_text": "£378.90", "source_page": 1},
                        "renewal_date": {"value": "2026-10-14", "confidence": 0.95, "source_text": "14 October 2026", "source_page": 1},
                    }
                ],
                "stated_total": {"value": 378.90, "confidence": 0.95, "source_text": "£378.90", "source_page": 1},
                "identifiers": {
                    "policy_number": {"value": "SM-0000042", "confidence": 0.95, "source_text": "SM-0000042", "source_page": 1}
                },
            }
        )
        intent = json.dumps({"intent": "quote_comparison", "product": "motor"})
        llm = FakeLLMClient(
            [doc_type_schedule, schedule, doc_type_quote, shape, quote, intent]
        )

        output = io.StringIO()
        with patch("records.cli.main._make_llm", return_value=llm), redirect_stdout(output):
            self.assertEqual(main(["ingest", str(EXAMPLES / "motor_policy_schedule.txt")]), 0)
            self.assertEqual(main(["policies"]), 0)
            self.assertEqual(main(["ingest", str(EXAMPLES / "motor_renewal_quote.txt")]), 0)
            self.assertEqual(main(["renewals"]), 0)
            self.assertEqual(
                main(["ask", "how does this quote compare to my current policy?"]), 0
            )

        rendered = output.getvalue()
        self.assertIn("accepted [policy_schedule]", rendered)
        self.assertIn("SwiftSure Insurance Ltd", rendered)
        self.assertIn("accepted [renewal_quote]", rendered)
        self.assertIn("quoted £378.90 vs current £352.40", rendered)
        self.assertEqual(llm.responses, [])


class TestReadmeContract(unittest.TestCase):
    def test_quickstart_only_advertises_live_commands(self):
        readme = (EXAMPLES.parent / "README.md").read_text()
        self.assertIn("records ingest examples/motor_policy_schedule.txt", readme)
        self.assertIn('records ask "how does this quote compare to my current policy?"', readme)
        self.assertIn("\nrecords mcp", readme)
        self.assertNotIn("MCP server is planned", readme)
        parser = _build_parser()
        for command in (
            "ingest",
            "review",
            "policies",
            "renewals",
            "ask",
            "discard",
            "eval",
            "mcp",
        ):
            self.assertIn(command, parser._subparsers._group_actions[0].choices)


if __name__ == "__main__":
    unittest.main()
