"""/importstatus: видно, что держит очередь загрузки книг, и зависшую можно сбросить.

Регрессия: замок import_lock жил только в памяти, без статуса и без сброса —
Люся отвечала «уже обрабатывает другую большую книгу», а какую и движется ли
она, узнать было нельзя (только Restart в Railway).

Запуск: python -m unittest discover -s tests
"""

import os
import shutil
import tempfile
import time
import unittest

from helpers import load_bot

bot = load_bot()
import big_book_upload  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402


def _buttons(keyboard):
    return [b.callback_data for row in keyboard.inline_keyboard for b in row]


class ImportStatusTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lucy_impstatus_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        library_db.DB_PATH = os.path.join(self.dir, "test.db")
        library_db.init_library_db()
        big_book_upload.force_release()
        self.addCleanup(big_book_upload.force_release)

    def _processing_book(self, title, page, minutes_ago):
        doc_id, code, _ = library_importer.register_document(
            title=title, storage_key=f"k/{title}", language="eng", checksum=title
        )
        library_importer.update_import_progress(doc_id, page, status="processing")
        stamp = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - minutes_ago * 60)
        )
        with library_db.get_connection() as conn:
            conn.execute(
                "UPDATE library_import_jobs SET updated_at = ? WHERE document_id = ?",
                (stamp, doc_id),
            )
        return code

    def _start(self, title):
        self.assertTrue(big_book_upload.import_lock.acquire(blocking=False))
        return big_book_upload._begin_job(title, "book.pdf", "u1")

    def test_free_queue(self):
        text, keyboard = bot.import_status_view()
        self.assertIn("Очередь свободна", text)
        self.assertNotIn("imp:reset", _buttons(keyboard))

    def test_running_book_shows_progress_without_reset(self):
        code = self._processing_book("Bakelite", 42, minutes_ago=2)
        self._start("Bakelite")
        text, keyboard = bot.import_status_view()
        self.assertIn("«Bakelite»", text)
        self.assertIn("Готово страниц OCR: 42", text)
        self.assertIn(code, text)
        self.assertNotIn("imp:reset", _buttons(keyboard))

    def test_stuck_book_offers_reset_and_reset_frees_queue(self):
        self._processing_book("Coro", 10, minutes_ago=40)
        generation = self._start("Coro")
        big_book_upload.current_job["started"] = time.time() - 3600
        text, keyboard = bot.import_status_view()
        self.assertIn("зависла", text)
        self.assertIn("imp:reset", _buttons(keyboard))

        self.assertTrue(big_book_upload.force_release())
        self.assertFalse(big_book_upload.import_lock.locked())

        # Новая загрузка после сброса; старый поток завершается позже —
        # он не должен снять замок новой книги.
        self._start("Trifari")
        self.assertFalse(big_book_upload._finish_job(generation))
        self.assertTrue(big_book_upload.import_lock.locked())

    def test_finish_releases_lock(self):
        generation = self._start("Sarah")
        self.assertTrue(big_book_upload._finish_job(generation))
        self.assertFalse(big_book_upload.import_lock.locked())
        self.assertEqual(big_book_upload.current_job, {})

    def test_interrupted_book_listed_when_queue_free(self):
        code = self._processing_book("Weiss", 77, minutes_ago=300)
        text, _ = bot.import_status_view()
        self.assertIn("Очередь свободна", text)
        self.assertIn("Недогружены", text)
        self.assertIn(f"{code} — Weiss, остановилась на стр. 77", text)

    def test_command_is_known_in_text_router(self):
        self.assertIs(bot.text_commands()["importstatus"], bot.importstatus_command)


if __name__ == "__main__":
    unittest.main()
