"""Read-only MCP server for local personal-record queries."""

from records.mcp.server import (
    compare_quotes,
    create_server,
    find_missing_info,
    get_provenance,
    get_record,
    get_renewals,
    list_records,
    run,
    search_policy_wording,
)

__all__ = [
    "compare_quotes",
    "create_server",
    "find_missing_info",
    "get_provenance",
    "get_record",
    "get_renewals",
    "list_records",
    "run",
    "search_policy_wording",
]
