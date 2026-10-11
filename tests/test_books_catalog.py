"""Визуальный каталог библиотеки: /books с пагинацией, карточка книги
с миниатюрой первой страницы PDF и /bookcheck.

Проверяется: миниатюра из PDF конкретного DOC-ID (одинаковые имена файлов
не путаются), несовпадение названия и обложки, пустая первая страница,
тысячи книг, повторный просмотр без повторной обработки PDF, ошибки R2,
ничего в базе не меняется.

Запуск: python -m unittest discover -s tests
"""

import io
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from helpers import FakeChat, buttons, load_bot, photos, texts

bot = load_bot()
import book_thumbnails  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402

ADMIN, USER = 999, 1


def page_jpeg(text=None, colour=(30, 30, 30), blank=False):
    image = Image.new("RGB", (600, 800), "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 60, 540, 300), fill=colour)  # «обложка»
        if text:
            draw.text((80, 400), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class FakeStorage:
    """R2: PDF по storage_key и кэш миниатюр."""

    def __init__(self):
        self.pdfs = {}       # storage_key -> [(jpeg, text layer)]
        self.objects = {}    # кэш миниатюр
        self.renders = []    # какие PDF «скачивались и рендерились»
        self.fail_read = False
        self.fail_write = False
        self.gets = 0

    def render_pages(self, document_code, storage_key, pages=3):
        if self.fail_read or storage_key not in self.pdfs:
            raise ConnectionError("AccessDenied https://r2.example/secret?X-Amz-Signature=abc")
        self.renders.append(storage_key)
        source = self.pdfs[storage_key]
        return len(source), [(n, data, text) for n, (data, text) in enumerate(source[:pages], 1)]

    def get(self, key):
        self.gets += 1
        return None if self.fail_read else self.objects.get(key)

    def put(self, key, data, content_type):
        if self.fail_write:
            return False
        self.objects[key] = data
        return True


def add_doc(conn, doc_id, file_title, meta_title=None, author=None, pages=3, status="completed",
            ocr=None, storage=None):
    code = f"DOC-{doc_id:06d}"
    conn.execute(
        "INSERT INTO library_documents (id, document_code, title, meta_title, meta_author, meta_source, "
        "storage_key, language) VALUES (?, ?, ?, ?, ?, ?, ?, 'eng')",
        (doc_id, code, file_title, meta_title, author, "auto" if meta_title else None,
         storage or f"books/browser/{doc_id:06d}-uuid.pdf"),
    )
    for number in range(1, pages + 1):
        text = (ocr or {}).get(number, f"Page {number} text")
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) VALUES (?, ?, ?, ?)",
            (doc_id, number, text, text),
        )
    conn.execute(
        "INSERT INTO library_import_jobs (document_id, status, last_completed_page) VALUES (?, ?, ?)",
        (doc_id, status, pages),
    )
    return code


class BooksCatalogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_books_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        library_db.init_library_db()
        self.storage = FakeStorage()
        for patch in (
            mock.patch.object(book_thumbnails, "render_pages", self.storage.render_pages),
            mock.patch.object(book_thumbnails, "r2_get", self.storage.get),
            mock.patch.object(book_thumbnails, "r2_put", self.storage.put),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.admin = FakeChat(bot, ADMIN)
        self.user = FakeChat(bot, USER)

        conn = sqlite3.connect(path)
        # Две разные книги с одинаковым именем файла «Book 5».
        add_doc(conn, 5, "Book 5", "Costume Jewelry", "Judith Miller")
        add_doc(conn, 7, "Book 5", "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors",
                "Monica Lynn Clements", pages=182,
                ocr={1: "SARAH COVENTRY JEWELRY\nAn Unauthorized Guide for Collectors"})
        # Служебная запись — не книга.
        conn.execute("INSERT INTO library_documents (id, document_code, title, storage_key) "
                     "VALUES (9, 'DOC-000009', 'jet test', 'manual')")
        conn.commit()
        conn.close()
        self.storage.pdfs["books/browser/000005-uuid.pdf"] = [
            (page_jpeg("COSTUME JEWELRY", (0, 0, 160)), "COSTUME JEWELRY\nJudith Miller"),
            (page_jpeg("Contents"), "Contents"),
        ]
        self.storage.pdfs["books/browser/000007-uuid.pdf"] = [
            (page_jpeg("SARAH COVENTRY", (160, 0, 0)), "SARAH COVENTRY JEWELRY\nAn Unauthorized Guide"),
            (page_jpeg("Copyright"), "Copyright 2005"),
        ]

    def snapshot(self):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return [conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("library_documents", "library_pages", "library_import_jobs")]
        finally:
            conn.close()

    # --- /books: список ---

    async def test_books_paged_list(self):
        replies = await self.admin.say("/books")
        text = texts(replies)[0]
        self.assertIn("📚 КАТАЛОГ БИБЛИОТЕКИ · стр. 1 из 1", text)
        self.assertIn("Всего документов: 3 · книг: 2", text)
        self.assertIn("✅ DOC-000007 · Sarah Coventry Jewelry: An Unauthorized Guide for Collectors", text)
        self.assertIn("автор: Monica Lynn Clements", text)
        self.assertIn("страниц: 182 · готово", text)
        self.assertIn("файл: Book 5", text)
        self.assertIn("Служебные записи (не книги): 1", text)
        self.assertEqual(buttons(replies), ["bk:d:5:0:1:b", "bk:d:7:0:1:b"])
        self.assertEqual(self.storage.renders, [])  # список не трогает PDF
        self.assertEqual(self.storage.gets, 0)

    async def test_full_text_list_kept(self):
        text = "\n".join(texts(await self.admin.say("/books all")))
        self.assertIn("Всего документов: 3 · книг: 2", text)
        self.assertIn("Служебные записи (не книги): DOC-000009", text)

    async def test_only_admin(self):
        self.assertIn("только администратору", texts(await self.user.say("/books"))[0])
        self.assertIn("только администратору", texts(await self.user.say("/bookcheck"))[0])
        self.assertIn("только администратору", texts(await self.user.press("bk:d:7:0:1:b"))[0])
        self.assertEqual(self.storage.renders, [])

    # --- карточка книги ---

    async def test_same_file_names_get_their_own_pdf(self):
        first = await self.admin.press("bk:d:5:0:1:b")
        second = await self.admin.press("bk:d:7:0:1:b")
        self.assertEqual(self.storage.renders, ["books/browser/000005-uuid.pdf", "books/browser/000007-uuid.pdf"])
        cover5, cover7 = photos(first)[0], photos(second)[0]
        self.assertNotEqual(cover5, cover7)
        # Цвет «обложки» — синий у DOC-000005, красный у DOC-000007.
        self.assertGreater(Image.open(io.BytesIO(cover5)).getpixel((100, 100))[2], 100)
        self.assertGreater(Image.open(io.BytesIO(cover7)).getpixel((100, 100))[0], 100)
        caption = texts(second)[-1]
        self.assertIn("📗 DOC-000007 · стр. 1 PDF", caption)
        self.assertIn("Название: Sarah Coventry Jewelry", caption)
        self.assertIn("Автор: Monica Lynn Clements", caption)
        self.assertIn("Страниц (OCR): 182 · OCR: ✅ готово", caption)
        self.assertIn("Файл: Book 5", caption)
        keys = [k for k in self.storage.objects if k.endswith("p0001.jpg")]
        self.assertEqual(len(keys), 2)
        self.assertTrue(all(k.startswith(("thumbnails/DOC-000005/", "thumbnails/DOC-000007/")) for k in keys))

    async def test_card_buttons(self):
        replies = await self.admin.press("bk:d:5:0:1:b")
        self.assertEqual(buttons(replies), ["bk:d:5:0:2:b", "bk:d:7:0:1:b", "bk:p:0:n"])
        replies = await self.admin.press("bk:d:7:0:1:b")
        self.assertEqual(buttons(replies), ["bk:d:7:0:2:b", "bk:d:5:0:1:b", "bk:p:0:n"])
        back = await self.admin.press("bk:p:0:n")
        self.assertIn("КАТАЛОГ БИБЛИОТЕКИ", texts(back)[0])

    async def test_open_by_code(self):
        replies = await self.admin.say("/books DOC-000007")
        self.assertIn("📗 DOC-000007", texts(replies)[-1])

    async def test_repeat_view_uses_cache(self):
        first = await self.admin.press("bk:d:7:0:1:b")
        self.assertIn("Готовлю миниатюру DOC-000007", texts(first)[0])
        again = await self.admin.press("bk:d:7:0:1:b")
        page2 = await self.admin.press("bk:d:7:0:2:b")  # стр. 2 сделана в том же проходе
        self.assertEqual(self.storage.renders, ["books/browser/000007-uuid.pdf"])  # PDF — один раз
        self.assertNotIn("Готовлю", "\n".join(texts(again)))
        self.assertEqual(photos(again)[0], photos(first)[0])
        self.assertIn("стр. 2 PDF", texts(page2)[-1])
        # «Перезапуск» бота: кэш в R2 остаётся — снова без рендера.
        await FakeChat(bot, ADMIN).press("bk:d:7:0:1:b")
        self.assertEqual(len(self.storage.renders), 1)

    # --- /bookcheck ---

    async def test_bookcheck_shows_real_pdf_data(self):
        replies = await self.admin.say("/bookcheck DOC-000007")
        intro, card = texts(replies)[0], texts(replies)[-1]
        self.assertIn("ПРОВЕРКА КНИГ ПО PDF", intro)
        self.assertIn("Ничего не переименовывается автоматически", intro)
        self.assertIn("Страниц в PDF: 2", card)  # из настоящего PDF, а не из базы
        self.assertIn("Текст стр. 1 (текстовый слой PDF):", card)
        self.assertIn("«SARAH COVENTRY JEWELRY»", card)
        self.assertNotIn("не найдено в тексте", card)
        self.assertEqual(len(photos(replies)), 1)
        self.assertIn("bk:d:5:0:1:c", buttons(replies))  # навигация в режиме проверки

    async def test_bookcheck_title_mismatch_is_reported_not_fixed(self):
        # В базе — Sarah Coventry, а в PDF на самом деле Bakelite.
        self.storage.pdfs["books/browser/000007-uuid.pdf"] = [
            (page_jpeg("BAKELITE"), "BAKELITE JEWELRY\nGood Better Best\nDonna Wasserstrom"),
            (page_jpeg("Leslie Pina"), "Leslie Piña"),
        ]
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("UPDATE library_pages SET original_text = 'BAKELITE JEWELRY' WHERE document_id = 7 AND pdf_page = 1")
        conn.commit()
        conn.close()
        before = self.snapshot()
        replies = await self.admin.say("/bookcheck DOC-000007")
        card = texts(replies)[-1]
        self.assertIn("«BAKELITE JEWELRY»", card)
        self.assertIn("Название из каталога не найдено в тексте первых страниц", card)
        self.assertIn("/settitle DOC-000007 Название | Автор", card)
        self.assertEqual(self.snapshot(), before)  # ничего не переименовано

    async def test_blank_first_page_offers_second(self):
        self.storage.pdfs["books/browser/000005-uuid.pdf"] = [
            (page_jpeg(blank=True), ""),
            (page_jpeg("COSTUME JEWELRY"), "COSTUME JEWELRY"),
        ]
        replies = await self.admin.say("/bookcheck DOC-000005")
        card = texts(replies)[-1]
        self.assertIn("Стр. 1 пустая", card)
        self.assertIn("bk:d:5:0:2:c", buttons(replies))
        second = await self.admin.press("bk:d:5:0:2:c")
        self.assertIn("«COSTUME JEWELRY»", texts(second)[-1])
        self.assertEqual(len(self.storage.renders), 1)

    async def test_scanned_cover_without_text_layer_uses_library_ocr(self):
        self.storage.pdfs["books/browser/000007-uuid.pdf"] = [
            (page_jpeg(None, (200, 120, 0)), ""),  # обложка-картинка, текстового слоя нет
        ]
        replies = await self.admin.say("/bookcheck DOC-000007")
        card = texts(replies)[-1]
        self.assertIn("Текст стр. 1 (OCR библиотеки):", card)
        self.assertIn("«SARAH COVENTRY JEWELRY»", card)
        self.assertIn("нет текстового слоя", card)
        self.assertNotIn("Стр. 1 пустая", card)

    # --- ошибки R2 ---

    async def test_r2_read_error_gives_card_without_thumbnail(self):
        self.storage.fail_read = True
        before = self.snapshot()
        replies = await self.admin.press("bk:d:7:0:1:b")
        text = "\n".join(texts(replies))
        self.assertIn("Миниатюра недоступна: не удалось прочитать PDF из хранилища", text)
        self.assertIn("Название: Sarah Coventry Jewelry", text)
        self.assertNotIn("X-Amz", text)
        self.assertNotIn("r2.example", text)
        self.assertIn("bk:p:0:n", buttons(replies))
        self.assertEqual(self.snapshot(), before)

    async def test_r2_write_error_still_shows_thumbnail(self):
        self.storage.fail_write = True
        replies = await self.admin.press("bk:d:7:0:1:b")
        self.assertEqual(len(photos(replies)), 1)
        self.assertEqual(self.storage.objects, {})

    async def test_missing_page_and_stale_button(self):
        self.storage.pdfs["books/browser/000005-uuid.pdf"] = [(page_jpeg("ONE"), "ONE PAGE")]
        replies = await self.admin.press("bk:d:5:0:3:b")
        self.assertIn("нет страницы 3", texts(replies)[-1])
        self.assertIn("устарела", texts(await self.admin.press("bk:d:x"))[0])
        self.assertIn("не найден", texts(await self.admin.press("bk:d:404:0:1:b"))[0])

    async def test_nothing_changes_in_library(self):
        before = self.snapshot()
        await self.admin.say("/books")
        await self.admin.press("bk:d:5:0:1:b")
        await self.admin.say("/bookcheck")
        await self.admin.press("bk:d:7:0:2:c")
        self.assertEqual(self.snapshot(), before)


class TitleCheckTest(unittest.TestCase):
    def test_generic_words_do_not_count(self):
        found = book_thumbnails.title_found
        self.assertFalse(found("Costume Jewelry", "BAKELITE JEWELRY", "Good Better Best"))
        self.assertTrue(found("Costume Jewelry", "COSTUME JEWELRY", "Judith Miller"))
        self.assertTrue(found("Sarah Coventry Jewelry: An Unauthorized Guide for Collectors",
                              "SARAH COVENTRY JEWELRY"))
        self.assertFalse(found("Sarah Coventry Jewelry: An Unauthorized Guide for Collectors",
                               "BAKELITE JEWELRY", "An Unauthorized Guide for Collectors"))
        self.assertIsNone(found("", "anything"))


class LargeLibraryTest(unittest.IsolatedAsyncioTestCase):
    """2500 книг: список листается быстро и не трогает PDF/R2."""

    @classmethod
    def setUpClass(cls):
        cls.path = os.path.join(tempfile.mkdtemp(prefix="lucy_books_big_"), "test.db")
        library_db.DB_PATH = cls.path
        library_db.init_library_db()
        conn = sqlite3.connect(cls.path)
        for doc_id in range(1, 2501):
            add_doc(conn, doc_id, f"book{doc_id}", f"Book number {doc_id}", pages=2)
        conn.commit()
        conn.close()

    def setUp(self):
        bot.DB_PATH = self.path
        library_db.DB_PATH = self.path
        self.admin = FakeChat(bot, ADMIN)
        self.renders = []
        patch = mock.patch.object(book_thumbnails, "render_pages",
                                  lambda *a, **k: self.renders.append(a) or (0, []))
        patch.start()
        self.addCleanup(patch.stop)

    async def test_pagination(self):
        started = time.monotonic()
        replies = await self.admin.say("/books")
        text = texts(replies)[0]
        self.assertIn("стр. 1 из 250", text)
        data = buttons(replies)
        self.assertEqual(len([d for d in data if d.startswith("bk:d:")]), 10)
        self.assertEqual(data[-1], "bk:p:1:e")
        self.assertNotIn("bk:p:-1:e", data)

        page = await self.admin.press("bk:p:1:e")  # листается в том же сообщении
        self.assertIn("стр. 2 из 250", texts(page)[0])
        self.assertIn("DOC-000011", texts(page)[0])
        self.assertEqual(buttons(page)[-2:], ["bk:p:0:e", "bk:p:2:e"])

        last = await self.admin.say("/books 250")
        self.assertIn("стр. 250 из 250", texts(last)[0])
        self.assertIn("DOC-002500", texts(last)[0])
        self.assertEqual(buttons(last)[-1], "bk:p:248:e")  # только «Предыдущая»

        beyond = await self.admin.say("/books 9999")
        self.assertIn("стр. 250 из 250", texts(beyond)[0])
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(self.renders, [])

    async def test_card_back_returns_to_its_page(self):
        with mock.patch.object(book_thumbnails, "get_preview",
                               side_effect=book_thumbnails.PreviewUnavailable("тест")):
            replies = await self.admin.say("/books DOC-001234")
        self.assertIn("bk:p:123:n", buttons(replies))  # книга 1234 — на стр. 124 списка


if __name__ == "__main__":
    unittest.main()
