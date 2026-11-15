"""Очередь распознавания книг.

Регрессия: книга, загруженная через /bigbook, пока Люся читала другую,
получала отказ — PDF оставался в R2 непрочитанным. После перезапуска
Railway чтение тоже пропадало. Теперь очередь хранится в базе.

Запуск: python -m unittest discover -s tests
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from helpers import load_bot

load_bot()
import big_book_upload  # noqa: E402
import import_queue  # noqa: E402
import library_db  # noqa: E402


class ImportQueueTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lucy_queue_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        library_db.DB_PATH = os.path.join(self.dir, "test.db")
        library_db.init_library_db()

    def test_same_book_is_not_queued_twice(self):
        first, added = import_queue.enqueue("books/browser/a.pdf", "A", "eng")
        again, added_again = import_queue.enqueue("books/browser/a.pdf", "A", "eng")
        self.assertTrue(added)
        self.assertFalse(added_again)
        self.assertEqual(first, again)

    def test_books_go_strictly_in_order(self):
        a, _ = import_queue.enqueue("books/a.pdf", "A", "eng")
        b, _ = import_queue.enqueue("books/b.pdf", "B", "deu")
        self.assertEqual(import_queue.position(a), 0)
        self.assertEqual(import_queue.position(b), 1)
        self.assertEqual(import_queue.claim_next()["title"], "A")
        self.assertEqual(import_queue.position(b), 1)  # A читается — B ждёт
        import_queue.finish(a)
        self.assertEqual(import_queue.position(b), 0)
        self.assertEqual(import_queue.claim_next()["title"], "B")
        self.assertIsNone(import_queue.claim_next())

    def test_interrupted_book_returns_to_queue_after_restart(self):
        import_queue.enqueue("books/a.pdf", "A", "eng")
        import_queue.claim_next()  # читалась, и тут Railway перезапустился
        self.assertIsNone(import_queue.claim_next())
        self.assertEqual(import_queue.requeue_interrupted(), 1)
        self.assertEqual(import_queue.claim_next()["title"], "A")

    def test_worker_reads_every_queued_book_and_records_errors(self):
        read = []

        def fake_import(storage_key, title, language):
            read.append(title)
            if title == "Bad":
                raise RuntimeError("broken pdf")
            return {"document_code": "DOC-1", "created": True}

        import_queue.enqueue("books/a.pdf", "A", "eng")
        import_queue.enqueue("books/bad.pdf", "Bad", "eng")
        import_queue.enqueue("books/c.pdf", "C", "fra")

        class SyncThread:
            def __init__(self, target, args=(), daemon=None):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        reports = []
        with mock.patch.object(big_book_upload, "import_pdf_with_ocr", fake_import), \
                mock.patch.object(big_book_upload, "book_report", lambda *a, **k: "ok"), \
                mock.patch.object(big_book_upload, "notify_admin", reports.append), \
                mock.patch.object(big_book_upload.threading, "Thread", SyncThread):
            big_book_upload.kick_queue()

        self.assertEqual(read, ["A", "Bad", "C"])
        self.assertEqual(import_queue.pending(), [])
        self.assertFalse(big_book_upload.import_lock.locked())
        self.assertTrue(any("Ошибка обработки книги «Bad»" in r for r in reports))


if __name__ == "__main__":
    unittest.main()
