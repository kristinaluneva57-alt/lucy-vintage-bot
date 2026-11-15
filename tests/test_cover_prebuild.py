"""Обложки: рисуются сразу при загрузке книги, /covers не виснет на огромной книге.

Регрессия: /covers для книги без кеша качал весь PDF (до 250 МБ) и на
огромной книге вставал навсегда — остальные обложки не приходили.

Запуск: python -m unittest discover -s tests
"""

import asyncio
import unittest
from unittest import mock

from helpers import load_bot

bot = load_bot()
import book_thumbnails  # noqa: E402
import library_importer  # noqa: E402


class CoverPrebuildTest(unittest.TestCase):
    def test_prebuild_uses_already_downloaded_pdf(self):
        calls = []

        def fake_render(code, key, pages=3, local_path=None):
            calls.append(local_path)
            return 1, []

        def no_download(*args):
            raise AssertionError("PDF не должен скачиваться второй раз")

        with mock.patch.object(book_thumbnails, "load_info", lambda *a: None), \
                mock.patch.object(book_thumbnails, "render_pages", fake_render), \
                mock.patch.object(book_thumbnails, "r2_put", lambda *a: True), \
                mock.patch.object(book_thumbnails.page_images, "pdf_path", no_download):
            library_importer._cover_safely("DOC-000001", "books/browser/a.pdf", "/tmp/book.pdf")
        self.assertEqual(calls, ["/tmp/book.pdf"])

    def test_prebuild_skips_when_cover_cached(self):
        with mock.patch.object(book_thumbnails, "load_info", lambda *a: {"pages": {}}), \
                mock.patch.object(book_thumbnails, "build_preview",
                                  side_effect=AssertionError("кеш есть — не рисуем")):
            library_importer._cover_safely("DOC-000001", "books/a.pdf", "/tmp/book.pdf")

    def test_prebuild_never_breaks_import(self):
        with mock.patch.object(book_thumbnails, "load_info", side_effect=RuntimeError("r2 down")):
            library_importer._cover_safely("DOC-000001", "books/a.pdf", "/tmp/book.pdf")

    def test_covers_move_on_when_one_cover_is_slow(self):
        documents = [
            {"code": "DOC-000001", "storage_key": "books/a.pdf", "meta_title": "Huge",
             "meta_author": "", "meta_source": "", "pages": 1, "status": "completed",
             "last_page": 1, "file_title": "huge"},
            {"code": "DOC-000002", "storage_key": "books/b.pdf", "meta_title": "Small",
             "meta_author": "", "meta_source": "", "pages": 1, "status": "completed",
             "last_page": 1, "file_title": "small"},
        ]

        def preview(code, key, page):
            if code == "DOC-000001":
                import time
                time.sleep(0.5)
            return b"jpeg", {}, "cache"

        class Message:
            def __init__(self):
                self.texts, self.photos = [], []

            async def reply_text(self, text, reply_markup=None):
                self.texts.append(text)

            async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
                self.photos.append(caption)

        message = Message()
        with mock.patch.object(bot.library_catalog, "count_books", lambda: 2), \
                mock.patch.object(bot.library_catalog, "books_page", lambda p, n: documents), \
                mock.patch.object(bot.book_thumbnails, "get_preview", preview), \
                mock.patch.object(bot, "COVER_TIMEOUT", 0.1):
            asyncio.run(bot.send_covers_page(message, 0))
        self.assertTrue(any("Обложка ещё рисуется" in t and "Huge" in t for t in message.texts))
        self.assertTrue(any("Small" in c for c in message.photos))

    def test_covers_continue_when_telegram_rejects_one_photo(self):
        documents = [
            {"code": f"DOC-00000{i}", "storage_key": f"books/{i}.pdf", "meta_title": f"Book {i}",
             "meta_author": "", "meta_source": "", "pages": 1, "status": "completed",
             "last_page": 1, "file_title": f"b{i}"}
            for i in (1, 2, 3)
        ]

        class Message:
            def __init__(self):
                self.texts, self.photos = [], []

            async def reply_text(self, text, reply_markup=None):
                self.texts.append(text)

            async def reply_photo(self, photo, caption=None, reply_markup=None, **kwargs):
                if "Book 2" in caption:
                    raise RuntimeError("Bad Request: PHOTO_INVALID_DIMENSIONS")
                self.photos.append(caption)

        message = Message()
        with mock.patch.object(bot.library_catalog, "count_books", lambda: 3), \
                mock.patch.object(bot.library_catalog, "books_page", lambda p, n: documents), \
                mock.patch.object(bot.book_thumbnails, "get_preview",
                                  lambda c, k, p: (b"jpeg", {}, "cache")):
            asyncio.run(bot.send_covers_page(message, 0))
        self.assertEqual(len(message.photos), 2)
        self.assertTrue(any("не получилось отправить" in t and "Book 2" in t for t in message.texts))
        self.assertTrue(any("Book 3" in c for c in message.photos))

    def test_timeout_is_not_reported_as_failure(self):
        class TimedOut(Exception):
            pass

        class Message:
            texts = []

            async def reply_photo(self, **kwargs):
                raise TimedOut("Timed out")

        self.assertTrue(asyncio.run(bot._send_cover(Message(), b"jpeg", "Book")))


if __name__ == "__main__":
    unittest.main()
