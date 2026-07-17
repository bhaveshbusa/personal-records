# Connect the read-only MCP server

The Phase 4 MCP server lets a local AI assistant read deterministic current-policy and renewal projections, compare renewal quotes, find record/review gaps, retrieve relevant policy-wording clauses, and inspect provenance. It does not ingest documents or expose confirmation, rejection, discard, event append, document update, or any other write operation.

Policy-wording retrieval returns evidence, not a coverage verdict. The hosting assistant may interpret only the clauses returned by the tool and must fail closed when the result says no wording or no relevant clause is available.

## Prerequisites

Install the project into its virtual environment and ingest records through the CLI first:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
records renewals
```

The MCP server does not call Anthropic and does not need `ANTHROPIC_API_KEY`. The assistant hosting the MCP connection supplies its own model.

## Client configuration

Add a stdio server entry to your MCP client's configuration. Use absolute paths because desktop clients commonly start outside the repository and may not expand `~`.

```json
{
  "mcpServers": {
    "personal-records": {
      "command": "/absolute/path/to/personal-records/.venv/bin/records",
      "args": ["mcp"],
      "env": {
        "PERSONAL_RECORDS_HOME": "/absolute/path/to/.personal-records"
      }
    }
  }
}
```

`PERSONAL_RECORDS_HOME` is optional. Omit `env` to use the default `~/.personal-records`; if the client uses a restricted environment, setting the absolute path explicitly is safer.

This `mcpServers` object is accepted by clients using the common desktop MCP configuration shape, including Claude Desktop. For another host, enter the same command, argument, environment variable, and `stdio` transport in its MCP-server settings.

Restart the MCP client after changing its configuration.

## Available tools

The exact registered tool set is:

- `list_records`
- `get_record`
- `get_renewals`
- `compare_quotes`
- `find_missing_info`
- `get_provenance`
- `search_policy_wording`

All `doc_id` and `entity_id` values are returned in full. Examples below use synthetic IDs for readability, but clients must pass values back exactly rather than abbreviating them.

### `list_records`

Takes no arguments. It wraps the deterministic `current_policies` projection and returns the latest active policy record for every entity. Each record includes:

- canonical `entity_id`;
- complete evidence `doc_id`;
- `doc_type`, state, validity dates, and provider where known;
- structured fields with field-level confidence and source snippets;
- record-level `trust` (`extracted` or `verified`).

The response shape is:

```json
{
  "found": true,
  "records": [
    {
      "entity_id": "SYN-42",
      "doc_id": "b9a4...complete-sha256-id...d71c",
      "doc_type": "policy_schedule",
      "state": "PolicyFiled",
      "fields": {
        "annual_premium": {
          "value": 350.0,
          "confidence": 0.96,
          "source_text": "Annual premium GBP 350.00",
          "source_page": 1
        }
      },
      "valid_from": "2025-10-14",
      "valid_to": "2026-10-14",
      "provider": "Synthetic Mutual",
      "trust": "extracted"
    }
  ],
  "sources": ["b9a4...complete-sha256-id...d71c"]
}
```

An empty projection is explicit: `{"found": false, "records": [], "sources": []}`.

### `get_record`

Takes one required `entity_id`. This is an exact lookup by the canonical key returned by `list_records`; it is not a `doc_id`, policy-number search, filename search, or fuzzy lookup. Using the projection key avoids ambiguous matches and keeps evidence identity separate from record identity.

On success it returns `identifier_type: "entity_id"`, the matched `record`, and its complete evidence ID in `sources`. An unknown identifier is a successful, explicit not-found response:

```json
{
  "found": false,
  "identifier_type": "entity_id",
  "entity_id": "UNKNOWN",
  "record": null,
  "sources": []
}
```

### `get_renewals`

Returns active renewal rows with product, premium, renewal date, status, trust, and the complete evidence `doc_id`. Its optional `product` argument accepts a value such as `motor` or `home`.

No match returns `{"found": false, "rows": [], "sources": []}`.

### `compare_quotes`

Takes an optional `product` filter. It wraps the existing deterministic quote-comparison query over `renewal_offers`; the MCP layer does not recalculate premiums. Each offer includes quoted premium, renewal date, quote `doc_id`, current-policy `doc_id` where paired, deterministic delta/percentage (or a reason it is not comparable), and the minimum trust across the evidence used.

```json
{
  "found": true,
  "offers": [
    {
      "entity_id": "SYN-42",
      "doc_id": "quote-complete-doc-id",
      "product": "motor",
      "quoted_premium": 378.9,
      "renewal_date": "2026-10-14",
      "current_policy_doc_id": "policy-complete-doc-id",
      "premium_change": {
        "comparable": true,
        "previous": 350.0,
        "latest": 378.9,
        "delta": 28.9,
        "pct_change": 8.3
      },
      "trust": "extracted"
    }
  ],
  "sources": ["quote-complete-doc-id", "policy-complete-doc-id"]
}
```

No matching live offer returns `{"found": false, "offers": [], "sources": []}`.

### `find_missing_info`

Takes no arguments. It composes the existing deterministic missing-information query with the pending review queue. `gaps` reports projected products missing a renewal date, including the complete evidence `doc_id` and trust. `pending_review` reports complete document IDs, deterministic review reasons, and queue timestamps; pending items have no accepted fact trust yet.

`found` says whether a gap or pending item exists. `record_set_empty` separately distinguishes a new store with no renewal records and no pending review items from a populated, complete store. `sources` is the de-duplicated set of complete evidence IDs returned in both result groups.

No gaps in a populated store returns `found: false`, `record_set_empty: false`, and empty result arrays. A brand-new store returns `found: false`, `record_set_empty: true`, and empty result arrays.

### `get_provenance`

Takes a complete `doc_id` returned by any other tool. It returns allowlisted document metadata and the typed domain events supported by that document. It deliberately excludes:

- raw document bytes or extracted full text;
- the local `storage_path`;
- every write-capable operation.

Field-level `source_text` snippets may appear inside supporting events because they are the evidence for individual extracted values.

An unknown ID returns `found: false`, echoes the `doc_id`, and returns `document: null` plus an empty `events` array.

### `search_policy_wording`

Takes one required free-text `question`. It selects the latest non-superseded `policy_wording` document using the same read-only resolution rule as the CLI coverage flow, then delegates to the existing deterministic BM25 index in strict mode. It makes no Anthropic/API call, requires no API key, and returns at most four relevant chunks.

Strict retrieval keeps the existing BM25 score floor and adds a substantive-query-term gate. Query boilerplate such as `policy`, `wording`, `clause(s)`, `relevant`, and `cover`/`covered`/`coverage` does not establish relevance. A multi-term substantive query requires at least two distinct substantive terms in every returned chunk; a useful one-substantive-term query requires one. This mode is MCP-only: the existing LLM-backed CLI coverage flow retains its BM25-only retrieval to avoid an unintended recall change.

Each matched clause contains only the citation evidence needed by the hosting assistant:

- complete `doc_id` and `chunk_id`;
- `section_ref`, `heading`, and `page` (`section_ref` and `heading` may be `null` for genuinely unsectioned evidence; citation metadata is never invented);
- exact indexed text in `clause_text`;
- deterministic BM25 `score`.

Success has this shape:

```json
{
  "found": true,
  "reason": null,
  "clauses": [
    {
      "doc_id": "wording-complete-doc-id",
      "chunk_id": "wording-complete-doc-id-003",
      "section_ref": "1.1",
      "heading": "Section 1 — Glass cover / Windscreen damage",
      "page": 4,
      "clause_text": "We will pay to repair or replace your windscreen if it is cracked.",
      "score": 3.42
    }
  ],
  "sources": ["wording-complete-doc-id"]
}
```

The tool fails closed with one of two explicit shapes:

```json
{"found": false, "reason": "no_wording_on_file", "clauses": [], "sources": []}
```

```json
{"found": false, "reason": "no_relevant_clause", "clauses": [], "sources": ["wording-complete-doc-id"]}
```

`sources` identifies the wording searched when one was selected. The result never contains a verdict, full document, filename, or local storage path. The hosting assistant must not supplement the returned evidence with general insurance knowledge or imply cover when the tool fails closed.

## Verify the connection

Ask the client:

> When does my car insurance renew? Use my personal-records tools.

The client should call `get_renewals` and answer from its structured result. Then ask:

> What document supports that answer?

The client should pass the returned `doc_id` to `get_provenance` and identify the supporting document/event.

For the slice 2a tools, also try:

> List my current policies, then get the motor record by its entity ID.

The client should call `list_records`, take the exact returned `entity_id`, and pass it to `get_record`.

> Compare my motor renewal quote with my current policy.

The client should call `compare_quotes` with `product: "motor"` and cite both complete evidence document IDs when the result is comparable.

> Is any information missing or waiting for review?

The client should call `find_missing_info` and distinguish an empty store from a populated store with no gaps.

For policy-wording evidence, ask:

> What clauses in my policy wording are relevant to a cracked windscreen?

The client should call `search_policy_wording` with the question, explain any interpretation only from the returned clauses, and retain their section/page/document citations. It must not claim a coverage verdict when the tool returns `no_wording_on_file` or `no_relevant_clause`.

If no tools appear, run the configured command directly in a terminal to catch installation or path errors:

```bash
/absolute/path/to/personal-records/.venv/bin/records mcp
```

No output is expected: the process waits for MCP messages on stdin. Stop it with `Ctrl-C`.
