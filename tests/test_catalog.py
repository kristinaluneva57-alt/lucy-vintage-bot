"""Команды и каталог библиотеки: /books, /fixmeta, /settitle, /dupes.

Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

from helpers import FakeChat, load_bot, texts

bot = load_bot()
import library_catalog  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402
import lucy_ai  # noqa: E402

ADMIN = 999
USER = 1

JET_COVER = "SHIRE ARCHAEOLOGY\nJET JEWELLERY\nAND ORNAMENTS\nHelen Muller"
JET_COPYRIGHT = (
    "Published in 1980 by Shire Publications Ltd, Princes Risborough.\n"
    "Copyright © Helen Muller 1980. All rights reserved.\n"
    "ISBN 0 85263 518 3\n"
    "British Library Cataloguing in Publication Data:\n"
    "Muller, Helen. Jet jewellery and ornaments.\n"
    "Printed in Great Britain."
)
CONTENT = (
    "Jet is a fossilised wood, the remains of trees similar to the monkey "
    "puzzle tree, compressed over millions of years. The finest jet comes "
    "from the cliffs around Whitby on the Yorkshire coast. "
) * 3


def add_document(conn, doc_id, title, pages, storage="books/browser/x.pdf",
                 language="eng", checksum=None, status="completed"):
    code = f"DOC-{doc_id:06d}"
    conn.execute(
        "INSERT INTO library_documents (id, document_code, title, storage_key, language, checksum) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (doc_id, code, title, storage, language, checksum),
    )
    for number, text in pages:
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) "
            "VALUES (?, ?, ?, ?)",
            (doc_id, number, text, library_importer.clean_ocr_text(text)),
        )
    conn.execute(
        "INSERT INTO library_import_jobs (document_id, status, last_completed_page) VALUES (?, ?, ?)",
        (doc_id, status, len(pages)),
    )
    return code


class CatalogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        lucy_ai.init_ai_usage_table(path)

        conn = sqlite3.connect(path)
        # Книга с распознаваемым титулом.
        add_document(conn, 2, "Book 1", [(1, JET_COVER), (2, JET_COPYRIGHT)] +
                     [(n, CONTENT) for n in range(3, 40)], checksum="aaa")
        # DOC-000005: 266 страниц, титульный лист пустой (скан без текста).
        add_document(conn, 5, "4f1c2e9b0a", [(1, ""), (2, "")] +
                     [(n, f"Page {n}. " + CONTENT) for n in range(3, 267)],
                     language="ita")
        # Служебная тестовая запись.
        add_document(conn, 9, "TEST IMPORTER DOCUMENT", [(1, "test")], storage="test://x")
        conn.commit()
        conn.close()

        self.admin = FakeChat(bot, ADMIN)
        self.user = FakeChat(bot, USER)
        self.env = mock.patch.dict(os.environ, {"LUCY_AI_ENABLED": "1", "OPENAI_API_KEY": "sk-test"})
        self.env.start()

        def no_ai(*args, **kwargs):
            raise AssertionError("команда не должна попадать в ИИ-чат")

        self.ai_patch = mock.patch.object(lucy_ai, "ask_openai", no_ai)
        self.ai_patch.start()

    def tearDown(self):
        self.ai_patch.stop()
        self.env.stop()

    def snapshot(self):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            pages = conn.execute(
                "SELECT document_id, pdf_page, original_text, cleaned_text FROM library_pages ORDER BY id"
            ).fetchall()
            documents = conn.execute(
                "SELECT id, document_code, title, storage_key FROM library_documents ORDER BY id"
            ).fetchall()
            return pages, documents
        finally:
            conn.close()

    # --- маршрутизация команд ---

    def test_parse_text_command(self):
        parse = bot.parse_text_command
        self.assertEqual(parse("/books"), ("books", []))
        self.assertEqual(parse("  /Books.  "), ("books", []))
        self.assertEqual(parse("/ books"), ("books", []))
        self.assertEqual(parse("/books@LucyVintageBot"), ("books", []))
        self.assertEqual(parse("/bооks"), ("books", []))  # кириллическая «о»
        self.assertEqual(parse("/settitle DOC-5 Название | Автор"),
                         ("settitle", ["DOC-5", "Название", "|", "Автор"]))
        self.assertIsNone(parse("книги"))

    async def test_books_text_variants_never_reach_ai(self):
        for variant in ("/books", " /books", "/ books", "/Books.", "/bооks", "/books@LucyVintageBot"):
            replies = await self.admin.say(variant)
            self.assertIn("КАТАЛОГ БИБЛИОТЕКИ", texts(replies)[0], variant)

    async def test_command_entity_fallback(self):
        message = types.SimpleNamespace(text="/BOOKS", chat=self.admin)

        async def reply_text(text, reply_markup=None, **kwargs):
            self.admin.sent.append(("text", text, reply_markup))

        message.reply_text = reply_text
        update = self.admin._update(message=message)
        start = len(self.admin.sent)
        await bot.command_fallback(update, self.admin.context)
        self.assertIn("КАТАЛОГ", texts(self.admin.sent[start:])[0])

    async def test_unknown_command_is_not_sent_to_ai(self):
        replies = await self.user.say("/книги")
        self.assertIn("Не знаю команду", texts(replies)[0])

    async def test_books_is_admin_only(self):
        replies = await self.user.say("/books")
        self.assertIn("только администратору", texts(replies)[0])

    async def test_command_during_anketa_is_not_saved_as_answer(self):
        await self.admin.press("box:add")
        replies = await self.admin.say("/books")
        self.assertIn("КАТАЛОГ", texts(replies)[0])

    # --- каталог ---

    async def test_catalog_lists_all_documents(self):
        replies = await self.admin.say("/books")
        catalog = "\n".join(texts(replies))
        self.assertIn("Всего документов: 3 · книг: 2", catalog)
        self.assertIn("DOC-000005 · Название не определено", catalog)
        self.assertIn("язык: итальянский", catalog)
        self.assertIn("страниц: 266", catalog)
        self.assertIn("готово", catalog)
        self.assertIn("пустых: 2", catalog)
        self.assertIn("файл: 4f1c2e9b0a", catalog)
        self.assertIn("Служебные записи (не книги): DOC-000009", catalog)
        self.assertIn("/fixmeta all", catalog)

    async def test_catalog_shows_detected_title_and_author(self):
        await self.admin.say("/fixmeta all")
        catalog = "\n".join(texts(await self.admin.say("/books")))
        self.assertIn("DOC-000002 · Jet Jewellery and Ornaments", catalog)
        self.assertIn("автор: Helen Muller", catalog)
        self.assertIn("DOC-000005 · Название не определено", catalog)

    # --- названия ---

    def test_detect_jet_title_and_author(self):
        detected = library_catalog.detect_metadata(
            [(1, JET_COVER), (2, JET_COPYRIGHT), (3, CONTENT)], "Book 1"
        )
        self.assertEqual(detected["title"], "Jet Jewellery and Ornaments")
        self.assertEqual(detected["author"], "Helen Muller")

    def test_unsure_title_is_not_invented(self):
        detected = library_catalog.detect_metadata(
            [(1, "Some Random Words Here"), (2, CONTENT)], "scan_0042"
        )
        self.assertIsNone(detected["title"])
        self.assertEqual(detected["title_candidate"], "Some Random Words Here")

    def test_publisher_is_not_author(self):
        detected = library_catalog.detect_metadata(
            [(1, "© 2005 Edizioni Polistampa\nAll rights reserved")], "x"
        )
        self.assertIsNone(detected["author"])

    def test_empty_title_pages_suggest_ocr(self):
        detected = library_catalog.detect_metadata([(1, ""), (2, ""), (3, CONTENT)], "x")
        self.assertIsNone(detected["title"])
        self.assertIn("ocr", detected["reason"])

    async def test_fixmeta_single_document(self):
        replies = await self.admin.say("/fixmeta DOC-000002")
        self.assertIn("✅ DOC-000002: Jet Jewellery and Ornaments", texts(replies)[0])
        row = library_catalog.get_document("DOC-000002")
        self.assertEqual(row[5:8], ("Jet Jewellery and Ornaments", "Helen Muller", "auto"))

    async def test_fixmeta_unsure_keeps_undefined(self):
        replies = await self.admin.say("/fixmeta DOC-000005")
        self.assertIn("Название не определено", texts(replies)[0])
        self.assertIsNone(library_catalog.get_document("DOC-000005")[5])

    async def test_fixmeta_ocr_reads_title_pages_without_touching_pages(self):
        before = self.snapshot()

        def fake_ocr(code, pages=3, dpi=400):
            self.assertEqual(code, "DOC-000005")
            return 5, [(1, "BIGIOTTERIA\nITALIANA\ndel Novecento\ndi Maria Rossi"),
                       (2, "Bigiotteria italiana del Novecento\n© 2005 Maria Rossi")]

        with mock.patch.object(library_catalog, "ocr_title_pages", fake_ocr):
            replies = await self.admin.say("/fixmeta DOC-000005 ocr")
            self.assertIn("Распознаю первые страницы", texts(replies)[0])
            replies = await self.admin.run_tasks()
        self.assertIn("Bigiotteria Italiana del Novecento", texts(replies)[0])
        self.assertIn("Maria Rossi", texts(replies)[0])
        self.assertEqual(self.snapshot(), before)  # страницы и документы не изменились

    async def test_settitle_manual_and_protected(self):
        replies = await self.admin.say("/settitle DOC-000005 Bigiotteria italiana | Maria Rossi")
        self.assertIn("✅ DOC-000005: Bigiotteria italiana", texts(replies)[0])
        catalog = "\n".join(texts(await self.admin.say("/books")))
        self.assertIn("DOC-000005 · Bigiotteria italiana ✍️", catalog)
        self.assertIn("автор: Maria Rossi", catalog)

        replies = await self.admin.say("/fixmeta DOC-000005")
        self.assertIn("задано вручную", texts(replies)[0])
        self.assertEqual(library_catalog.get_document("DOC-000005")[5], "Bigiotteria italiana")

        # force при неуверенном распознавании не стирает ручное название.
        await self.admin.say("/fixmeta DOC-000005 force")
        self.assertEqual(library_catalog.get_document("DOC-000005")[5], "Bigiotteria italiana")

    async def test_settitle_errors(self):
        self.assertIn("Формат", texts(await self.admin.say("/settitle"))[0])
        self.assertIn("не найден", texts(await self.admin.say("/settitle DOC-000777 X"))[0])
        self.assertIn("только администратору", texts(await self.user.say("/settitle DOC-000002 X"))[0])

    def test_search_uses_detected_title(self):
        library_catalog.set_manual_metadata("DOC-000002", "Jet Jewellery and Ornaments", "Helen Muller")
        import library_search

        plan = library_search.build_search_plan("гагат")
        results = library_search.search_pages(bot.DB_PATH, plan)
        self.assertEqual(results[0]["title"], "Jet Jewellery and Ornaments")

    # --- автоматически при загрузке ---

    def test_import_detects_title(self):
        def fake_download(key, path):
            with open(path, "wb") as file:
                file.write(b"NEW PDF " + key.encode())

        pages = {1: JET_COVER, 2: JET_COPYRIGHT, 3: CONTENT}
        fake_fitz_doc = types.SimpleNamespace(
            load_page=lambda i: types.SimpleNamespace(get_text=lambda *a, **k: pages[i + 1]),
            close=lambda: None,
        )
        with mock.patch.object(library_importer, "download_pdf_from_r2", fake_download), \
                mock.patch.object(library_importer, "inspect_pdf", lambda p: {"page_count": 3}), \
                mock.patch.object(library_importer.fitz, "open", lambda *a, **k: fake_fitz_doc, create=True):
            result = library_importer.import_pdf_with_ocr("books/telegram/new.pdf", "IMG_2041", "eng")
        self.assertEqual(result["meta_title"], "Jet Jewellery and Ornaments")
        self.assertEqual(result["meta_author"], "Helen Muller")
        self.assertIn("Название: Jet Jewellery and Ornaments", library_importer.metadata_lines(result))

    def test_metadata_lines_when_unknown(self):
        lines = library_importer.metadata_lines(
            {"document_code": "DOC-000007", "meta_title": None, "meta_author": None}
        )
        self.assertIn("Название не определено", lines)
        self.assertIn("/settitle DOC-000007", lines)

    # --- дубликаты ---

    async def test_duplicates_report_and_nothing_deleted(self):
        conn = sqlite3.connect(bot.DB_PATH)
        add_document(conn, 6, "copy", [(1, JET_COVER), (2, JET_COPYRIGHT)] +
                     [(n, CONTENT) for n in range(3, 40)], checksum="aaa")
        # Старая запись без хеша с тем же текстом — возможный дубль DOC-000005.
        add_document(conn, 7, "old scan", [(1, ""), (2, "")] +
                     [(n, f"Page {n}. " + CONTENT) for n in range(3, 267)])
        conn.commit()
        conn.close()
        before = self.snapshot()

        replies = await self.admin.say("/dupes")
        report = "\n".join(texts(replies))
        self.assertIn("Точные дубли", report)
        self.assertIn("DOC-000002", report)
        self.assertIn("DOC-000006", report)
        self.assertIn("Возможные совпадения", report)
        self.assertIn("DOC-000005", report)
        self.assertIn("DOC-000007", report)
        self.assertIn("/dupes hash", report)
        self.assertIn("Ничего не удаляется", report)
        self.assertEqual(self.snapshot(), before)

        catalog = "\n".join(texts(await self.admin.say("/books")))
        self.assertIn("Дубликаты: точных 1, возможных 1", catalog)

    async def test_dupes_hash_fills_only_empty_checksums(self):
        def fake_download(key, path):
            with open(path, "wb") as file:
                file.write(b"same pdf bytes")

        with mock.patch.object(library_importer, "download_pdf_from_r2", fake_download):
            await self.admin.say("/dupes hash")
            replies = await self.admin.run_tasks()
        self.assertIn("SHA-256 посчитан: 1", texts(replies)[0])
        conn = sqlite3.connect(bot.DB_PATH)
        checksums = dict(conn.execute("SELECT document_code, checksum FROM library_documents"))
        conn.close()
        self.assertEqual(checksums["DOC-000002"], "aaa")  # существующий хеш не тронут
        self.assertEqual(len(checksums["DOC-000005"]), 64)
        self.assertIsNone(checksums["DOC-000009"])  # служебная запись не трогается

    def test_migration_is_repeatable(self):
        library_db.init_library_db()
        library_db.init_library_db()
        self.assertEqual(len(library_catalog.catalog()), 3)


if __name__ == "__main__":
    unittest.main()
