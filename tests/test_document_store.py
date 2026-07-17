"""Document store: content-hash dedup, append-only metadata versions, fold."""

import json
import tempfile
import unittest
from pathlib import Path

from records.store import get_document, list_documents, put_document, update_document


class TestDocumentStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_put_and_get_round_trip(self):
        record, is_duplicate = put_document(b"hello", "policy.pdf", media_type="application/pdf", root=self.root)
        self.assertFalse(is_duplicate)
        self.assertEqual(record["file_name"], "policy.pdf")
        self.assertEqual(record["media_type"], "application/pdf")
        self.assertTrue(Path(record["storage_path"]).read_bytes() == b"hello")
        self.assertEqual(get_document(record["doc_id"], root=self.root), record)

    def test_duplicate_content_is_deduped(self):
        first, _ = put_document(b"same bytes", "a.pdf", root=self.root)
        second, is_duplicate = put_document(b"same bytes", "b.pdf", root=self.root)
        self.assertTrue(is_duplicate)
        self.assertEqual(second, first)
        self.assertEqual(len(list_documents(root=self.root)), 1)

    def test_update_appends_new_version_latest_wins(self):
        record, _ = put_document(b"v", "doc.pdf", root=self.root)
        updated = update_document(record["doc_id"], root=self.root, superseded_by="other-doc")
        self.assertEqual(updated["superseded_by"], "other-doc")
        self.assertEqual(get_document(record["doc_id"], root=self.root), updated)
        # Append-only: two lines in the log, original untouched.
        lines = (self.root / "documents.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIsNone(json.loads(lines[0])["superseded_by"])

    def test_update_unknown_doc_id_raises(self):
        with self.assertRaises(KeyError):
            update_document("nope", root=self.root)

    def test_list_documents_empty_store(self):
        self.assertEqual(list_documents(root=self.root), [])


if __name__ == "__main__":
    unittest.main()
