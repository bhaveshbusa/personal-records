"""Document store — the immutable evidence layer, separate from facts.

Documents are evidence for domain events, not records themselves. A document
never mutates: `put_document` writes the file exactly once, keyed by its
content hash (dedup for free). Every later change — classification, review
outcome, supersedence — is a new metadata version appended to the log and
folded by "latest write per doc_id wins".

Storage is local JSONL (metadata) + local files (content) under the data
directory (`records.config.data_dir()`). `put_document`/`update_document` are
the only write paths.

The store intentionally records evidence identity and metadata only. Domain
facts, entity linking, and query projections are owned by their respective
layers.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from records.config import data_dir


def _store_path(root: Path | None) -> Path:
    return (root or data_dir()) / "documents.jsonl"


def _files_dir(root: Path | None) -> Path:
    return (root or data_dir()) / "documents"


def _append(record: dict, store_path: Path) -> dict:
    store_path.parent.mkdir(parents=True, exist_ok=True)
    with open(store_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return record


def _fold(store_path: Path) -> dict[str, dict]:
    """Latest metadata version per doc_id, replayed oldest-first."""
    if not store_path.exists():
        return {}
    latest: dict[str, dict] = {}
    with open(store_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            latest[record["doc_id"]] = record
    return latest


def put_document(
    file_bytes: bytes,
    file_name: str,
    *,
    media_type: str = "application/octet-stream",
    root: Path | None = None,
) -> tuple[dict, bool]:
    """Write a new document. Existing hash -> return the existing record
    unchanged with is_duplicate=True; nothing is written (content-addressed
    dedup for free)."""
    doc_id = hashlib.sha256(file_bytes).hexdigest()
    existing = get_document(doc_id, root=root)
    if existing is not None:
        return existing, True

    files_dir = _files_dir(root)
    storage_path = files_dir / f"{doc_id}{Path(file_name).suffix}"
    files_dir.mkdir(parents=True, exist_ok=True)
    storage_path.write_bytes(file_bytes)

    record = {
        "doc_id": doc_id,
        "file_name": file_name,
        "media_type": media_type,
        "storage_path": str(storage_path),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
        "superseded_by": None,
    }
    _append(record, _store_path(root))
    return record, False


def update_document(doc_id: str, *, root: Path | None = None, **fields) -> dict:
    """Append a new metadata version for an existing doc_id. The underlying
    file is never touched."""
    current = get_document(doc_id, root=root)
    if current is None:
        raise KeyError(f"unknown doc_id: {doc_id}")
    updated = {**current, **fields}
    _append(updated, _store_path(root))
    return updated


def get_document(doc_id: str, *, root: Path | None = None) -> dict | None:
    return _fold(_store_path(root)).get(doc_id)


def list_documents(*, root: Path | None = None) -> list[dict]:
    return list(_fold(_store_path(root)).values())
