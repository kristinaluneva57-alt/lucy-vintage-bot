"""/lost и /covers: книги, которые лежат в R2, но не попали в каталог.

Регрессия: книга сохранялась в R2, распознавание шло в фоне в очереди;
при перезапуске Railway очередь пропадала — PDF оставался в R2 без записи
в каталоге и без отчёта. /lost находит такие PDF, /covers показывает обложки.

Запуск: python -m unittest discover -s tests
"""

import asyncio
import datetime
import os
import shutil
import tempfile
import unittest

from helpers import load_bot

bot = load_bot()
import library_db  # noqa: E402
import library_importer  # noqa: E402
import lost_books  # noqa: E402


def _item(key, day):
    return {"key": key, "size": 5 * 1024 * 1024, "uploaded": datetime.datetime(2026, 11, day, 20, 0)}


class LostBooksTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lucy_lost_")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        library_db.DB_PATH = os.path.join(self.dir, "test.db")
        library_db.init_library_db()

    def test_finds_pdfs_missing_from_catalog_newest_first(self):
        library_importer.register_document(
            title="Есть", storage_key="books/telegram/a.pdf", language="eng", checksum="a"
        )
        items = [
            _item("books/telegram/a.pdf", 10),
            _item("books/telegram/b.pdf", 11),
            _item("books/browser/c.pdf", 12),
        ]
        result = lost_books.find_lost(items, lost_books.known_storage_keys())
        self.assertEqual(result["r2_total"], 3)
        self.assertEqual([i["key"] for i in result["lost"]],
                         ["books/browser/c.pdf", "books/telegram/b.pdf"])

    def test_lookup_by_short_id_fits_telegram_button(self):
        key = "books/telegram/" + "f" * 32 + ".pdf"
        item = lost_books.lost_by_id(lost_books.short_id(key), [_item(key, 12)], set())
        self.assertEqual(item["key"], key)
        callback = f"lost:i:{item['id']}:eng"
        self.assertLessEqual(len(callback.encode()), 64)
        self.assertIsNone(lost_books.lost_by_id("nope", [_item(key, 12)], set()))

    def test_summary_says_files_are_safe(self):
        result = lost_books.find_lost([_item("books/telegram/b.pdf", 11)], set())
        text = bot.lost_summary_text(result, 0)
        self.assertIn("НЕ в библиотеке: 1", text)
        self.assertIn("Файлы целы", text)
        self.assertIn("Потерявшихся нет", bot.lost_summary_text(lost_books.find_lost([], set()), 0))

    def test_describe_shows_upload_route(self):
        self.assertIn("через /bigbook", lost_books.describe(_item("books/browser/x.pdf", 12)))
        self.assertIn("через чат", lost_books.describe(_item("books/telegram/x.pdf", 12)))

    def test_commands_registered_as_text_commands(self):
        commands = bot.text_commands()
        self.assertIs(commands["lost"], bot.lost_command)
        self.assertIs(commands["covers"], bot.covers_command)


if __name__ == "__main__":
    unittest.main()
