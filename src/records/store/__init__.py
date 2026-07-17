"""Evidence layer: document store and event log (JSONL)."""

from records.store.document_store import (
    get_document,
    list_documents,
    put_document,
    update_document,
)

__all__ = ["get_document", "list_documents", "put_document", "update_document"]
