"""Local-first personal records: ingest evidence, review proposed facts,
and query deterministic projections with provenance."""

import argparse
import os
import sys
from pathlib import Path

from records import pipeline
from records.core import (
    NcdConfirmed,
    PolicyCorrected,
    PolicyFiled,
    current_policies,
    renewal_calendar,
    replay,
)
from records.review import queue

CLI_EPILOG = """examples:
  records ingest examples/motor_policy_schedule.txt
  records policies
  records ingest examples/motor_renewal_quote.txt
  records ask "how does this quote compare to my current policy?"
  records review
  records mcp

Data stays under ~/.personal-records by default. Set PERSONAL_RECORDS_HOME
to use another directory. Ingest, ask and eval require ANTHROPIC_API_KEY;
review, projection and MCP commands are local-only.
"""


def _make_llm():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "error: ANTHROPIC_API_KEY is not set.\n"
            "Ingestion uses the Anthropic API for extraction (bring your own key):\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        return None
    from records.extract.anthropic_client import AnthropicClient

    return AnthropicClient()


def _describe_event(event) -> str:
    name = type(event).__name__
    if isinstance(event, (PolicyFiled, PolicyCorrected)):
        return f"{name} entity={event.entity_id} ({len(event.fields)} fields, provider={event.provider or '?'})"
    if isinstance(event, NcdConfirmed):
        return f"{name} entity={event.entity_id}"
    return f"{name} {event.product} £{event.annual_premium:.2f}"


def _ingest(file: str) -> int:
    path = Path(file)
    if not path.is_file():
        print(f"error: no such file: {file}", file=sys.stderr)
        return 2
    llm = _make_llm()
    if llm is None:
        return 2

    result = pipeline.ingest(path, llm)
    doc_type = f" [{result.doc_type}]" if result.doc_type else ""
    if result.outcome == "duplicate":
        print(f"already ingested (duplicate): {result.doc_id}")
    elif result.outcome == "stored":
        print(f"stored as reference/evidence{doc_type}: {result.doc_id}")
    elif result.outcome == "accepted":
        print(f"accepted{doc_type}: {result.doc_id}")
        for event in result.events:
            print(f"  event: {_describe_event(event)}")
    else:
        print(f"routed to review{doc_type}: {result.doc_id}")
        for reason in result.review_reasons:
            print(f"  - {reason}")
        print("run 'records review' to inspect, confirm or reject.")
    return 0


def _review(confirm_id: str | None, reject_id: str | None) -> int:
    if confirm_id:
        try:
            events = pipeline.confirm(confirm_id)
        except (KeyError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"confirmed: {confirm_id}")
        if not events:
            print("  (no events — the stored document itself is the record)")
        for event in events:
            print(f"  event: {_describe_event(event)}")
        return 0
    if reject_id:
        try:
            queue.reject(reject_id)
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"rejected: {reject_id} (document kept as evidence; no events)")
        return 0

    pending = queue.list_pending()
    if not pending:
        print("review queue is empty.")
        return 0
    for item in pending:
        print(f"{item['doc_id']}  (queued {item['queued_at'][:10]})")
        for reason in item["reasons"]:
            print(f"  - {reason}")
        extraction = item.get("extraction")
        if extraction:
            for line in extraction.get("lines", []):
                premium = (line.get("annual_premium") or {}).get("value")
                print(f"  line: {line['product']}  premium={premium}")
            total = (extraction.get("stated_total") or {}).get("value")
            if total is not None:
                print(f"  stated_total: {total}")
            for name, entry in (extraction.get("fields") or {}).items():
                print(f"  field: {name} = {entry.get('value')}  (confidence {entry.get('confidence')})")
        print(f"  → records review --confirm {item['doc_id']}  |  --reject {item['doc_id']}")
    return 0


def _ask(question: str) -> int:
    llm = _make_llm()
    if llm is None:
        return 2
    from records.query import ask

    answer = ask(question, llm)
    print(answer.text)
    return 0


def _renewals() -> int:
    calendar = renewal_calendar(replay())
    if not calendar:
        print("no confirmed renewals on record yet.")
        return 0
    for row in calendar:
        days = f"{row['days_left']}d" if row["days_left"] is not None else "?"
        print(
            f"{row['product']:<10} {row['status']:<9} renews {row['renewal_date'] or 'unknown':<12} "
            f"({days})  £{row['annual_premium']:.2f}  [{row['state']}]  evidence: {row['doc_id'][:12]}…"
        )
    return 0


def _policies() -> int:
    rows = current_policies(replay())
    if not rows:
        print("no current policy records yet (file a policy_schedule first).")
        return 0
    for row in rows:
        premium = row["fields"].get("annual_premium")
        premium_txt = f"£{premium.value:.2f}" if premium and isinstance(premium.value, (int, float)) else "?"
        print(
            f"{row['entity_id']:<20} {row['provider'] or '?':<24} {premium_txt:<10} "
            f"valid to {row['valid_to'] or '?'}  [{row['state']}]  evidence: {row['doc_id'][:12]}…"
        )
    return 0


def _eval(cases_path: str | None, out_path: str | None) -> int:
    from records import evals

    cases_file = Path(cases_path) if cases_path else evals.DEFAULT_CASES_PATH
    if not cases_file.is_file():
        print(
            f"error: no eval manifest at {cases_file} — run from the repo root, "
            "or pass --cases path/to/cases.json",
            file=sys.stderr,
        )
        return 2
    llm = _make_llm()
    if llm is None:
        return 2

    cases = evals.load_cases(cases_file)
    print(f"Running {len(cases)} eval cases through the pipeline…\n")
    scores = evals.run_evals(cases, llm)
    print(evals.format_report(scores))
    results_path = Path(out_path) if out_path else cases_file.parent / "results.csv"
    evals.write_csv(scores, results_path)
    print(f"  Results written to {results_path}")
    return 0 if all(s.passed for s in scores) else 1


def _discard(doc_id: str, reason: str) -> int:
    pipeline.discard(doc_id, reason=reason)
    print(f"discarded: {doc_id} — its facts are retracted from every projection")
    print("(the document and the full event history are kept; this is an event-sourced correction)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="records",
        description=__doc__,
        epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    ingest = sub.add_parser("ingest", help="Ingest a document through the pipeline")
    ingest.add_argument("file", help="Path to the document (PDF or text)")

    review = sub.add_parser("review", help="Work the human-review queue")
    review_action = review.add_mutually_exclusive_group()
    review_action.add_argument(
        "--confirm", metavar="DOC_ID", help="Confirm a queued extraction; emits events"
    )
    review_action.add_argument(
        "--reject", metavar="DOC_ID", help="Reject a queued extraction; no events"
    )

    ask = sub.add_parser("ask", help="Ask a question about your records")
    ask.add_argument("question", help='e.g. "when does my car insurance renew?"')

    sub.add_parser("renewals", help="Show the renewal calendar")

    sub.add_parser("policies", help="Show current policy records (latest filed state per entity)")

    discard = sub.add_parser("discard", help="Retract a document's facts from every projection")
    discard.add_argument("doc_id", help="The document id whose facts should be retracted")
    discard.add_argument("--reason", default="", help="Why (kept in the event log)")

    evals = sub.add_parser("eval", help="Run the eval set; report per-stage accuracy")
    evals.add_argument("--cases", help="Eval manifest (default: evals/cases.json)")
    evals.add_argument("--out", help="Results CSV (default: next to the manifest)")

    sub.add_parser("mcp", help="Start the read-only MCP server over stdio")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "ingest":
        return _ingest(args.file)
    if args.command == "review":
        return _review(args.confirm, args.reject)
    if args.command == "ask":
        return _ask(args.question)
    if args.command == "renewals":
        return _renewals()
    if args.command == "policies":
        return _policies()
    if args.command == "discard":
        return _discard(args.doc_id, args.reason)
    if args.command == "eval":
        return _eval(args.cases, args.out)
    if args.command == "mcp":
        from records.mcp import run

        run()
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2  # argparse.error exits; keeps the return type explicit


if __name__ == "__main__":
    sys.exit(main())
