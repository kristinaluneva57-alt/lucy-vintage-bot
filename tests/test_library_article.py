"""Досье для статьи по книгам (/article).

Две книги: Sarah Coventry (много страниц о фирме + выходные данные)
и Jet Jewellery. OpenAI подменяется поддельным клиентом — настоящих
запросов нет.

Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

import helpers
from helpers import FakeChat, load_bot, texts

bot = load_bot()
import library_article  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402
import lucy_ai  # noqa: E402

openai_module = sys.modules["openai"]

COVENTRY = "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors"
JET = "Jet Jewellery and Ornaments"

COPYRIGHT = (
    "Sarah Coventry Jewelry. Copyright (c) 1999. All rights reserved. "
    "ISBN 0-7643-0000-0. Printed in China."
)


def coventry_page(number):
    return (
        f"Chapter {number}. Sarah Coventry jewelry was sold at home parties by "
        f"fashion directors. In 19{50 + number} the company introduced a new line "
        f"of brooches and necklaces. Sarah Coventry pieces are usually marked "
        f"SARAH COV. or SC on the clasp. Collectors look for the original boxes "
        f"and the matching earrings of the set number {number}."
    )


SARAH_JET = (
    "Black glass imitating jet was popular. Sarah Coventry also made black "
    "glass brooches in the style of Victorian jet mourning jewellery, sold at "
    "home parties in the 1960s."
)


class FakeOpenAI:
    calls = []
    inits = []
    answer = (
        "КОРОТКО\nSarah Coventry продавала украшения на домашних вечеринках [1].\n\n"
        "ГЛАВНЫЕ ФАКТЫ\n— Клеймо SARAH COV. на застёжке [2].\n"
        "— Чёрное стекло под гагат [7].\n— Выдуманный факт [99]."
    )
    error = None

    def __init__(self, *args, **kwargs):
        FakeOpenAI.inits.append(kwargs)
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=FakeOpenAI.answer))],
            usage=types.SimpleNamespace(prompt_tokens=1000, completion_tokens=500),
        )


async def _reply_document(self, document, filename=None, **kwargs):
    self.chat.sent.append(("document", (filename, document.getvalue().decode("utf-8")), None))


class LibraryArticleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        library_article.init_tables(path)

        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (7, 'DOC-000007', ?, 'books/telegram/a.pdf', 'eng')",
            (COVENTRY,),
        )
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (2, 'DOC-000002', ?, 'books/BOOK-000001/Book 1.pdf', 'eng')",
            (JET,),
        )
        pages = [(7, 2, COPYRIGHT)] + [(7, 10 + n, coventry_page(n)) for n in range(1, 9)]
        pages.append((2, 21, SARAH_JET))
        for document_id, pdf_page, text in pages:
            conn.execute(
                "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text, "
                "extraction_status) VALUES (?, ?, ?, ?, 'extracted')",
                (document_id, pdf_page, text, library_importer.clean_ocr_text(text)),
            )
        conn.commit()
        conn.close()

        self.env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "LUCY_LIBRARY_AI_ENABLED": "1",
            "LUCY_ARTICLE_DAILY_LIMIT": "20",
        })
        self.env.start()
        self.openai_patch = mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True)
        self.openai_patch.start()
        self.doc_patch = mock.patch.object(
            helpers.FakeMessage, "reply_document", _reply_document, create=True
        )
        self.doc_patch.start()
        FakeOpenAI.calls = []
        FakeOpenAI.inits = []
        FakeOpenAI.error = None
        self.admin = FakeChat(bot, 999)
        self.user = FakeChat(bot, 1)

    def tearDown(self):
        self.doc_patch.stop()
        self.openai_patch.stop()
        self.env.stop()

    def pages_snapshot(self):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return conn.execute("SELECT * FROM library_pages ORDER BY id").fetchall()
        finally:
            conn.close()

    @staticmethod
    def documents(replies):
        return [content for kind, content, _ in replies if kind == "document"]

    # --- доступ и подсказка ---

    async def test_only_admin(self):
        replies = await self.user.say("/article Sarah Coventry")
        self.assertIn("только администратору", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_usage_without_topic(self):
        text = texts(await self.admin.say("/article"))[0]
        self.assertIn("/article Sarah Coventry", text)
        self.assertEqual(FakeOpenAI.calls, [])

    # --- отбор фрагментов ---

    def test_fragments_capped_per_book_and_skip_copyright(self):
        plan = bot.library_search.build_search_plan("Sarah Coventry")
        results = bot.library_search.search_pages(bot.DB_PATH, plan, library_article.SEARCH_LIMIT)
        fragments = library_article.select_fragments(results)
        coventry_pages = {f["pdf_page"] for f in fragments if f["document_code"] == "DOC-000007"}
        self.assertLessEqual(len(coventry_pages), library_article.MAX_PAGES_PER_BOOK)
        self.assertNotIn(2, coventry_pages)  # выходные данные не идут в ИИ
        self.assertIn("DOC-000002", {f["document_code"] for f in fragments})  # вторая книга не потерялась
        self.assertEqual([f["n"] for f in fragments], list(range(1, len(fragments) + 1)))
        self.assertLessEqual(sum(len(f["text"]) for f in fragments), library_article.MAX_CONTEXT_CHARS)

    # --- досье ---

    async def test_dossier_with_real_sources_and_file(self):
        before = self.pages_snapshot()
        replies = await self.admin.say("/article Sarah Coventry")
        text = "\n".join(texts(replies))

        self.assertEqual(len(FakeOpenAI.calls), 1)
        self.assertEqual(FakeOpenAI.inits[0]["timeout"], library_article.AI_TIMEOUT)
        prompt = FakeOpenAI.calls[0]["messages"][1]["content"]
        self.assertIn("Тема статьи: Sarah Coventry", prompt)
        self.assertNotIn("ISBN", prompt)

        self.assertIn("ДОСЬЕ ДЛЯ СТАТЬИ", text)
        self.assertIn("Sarah Coventry продавала украшения на домашних вечеринках [1]", text)
        self.assertNotIn("[99]", text)  # ссылка на несуществующий фрагмент убрана
        self.assertIn(f"{COVENTRY} (DOC-000007) — PDF-стр.", text)
        self.assertIn(f"{JET} (DOC-000002) — PDF-стр. 21", text)  # [7] — страница о гагате

        files = self.documents(replies)
        self.assertEqual(len(files), 1)
        name, body = files[0]
        self.assertEqual(name, "dossier_sarah_coventry.txt")
        self.assertIn("ОРИГИНАЛЫ ФРАГМЕНТОВ", body)
        self.assertIn("SARAH COV.", body)  # точный текст книги для проверки
        self.assertEqual(self.pages_snapshot(), before)  # книги не меняются

    async def test_same_topic_is_cached(self):
        await self.admin.say("/article Sarah Coventry")
        replies = await self.admin.say("/article sarah   coventry")
        self.assertEqual(len(FakeOpenAI.calls), 1)
        self.assertIn("ДОСЬЕ ДЛЯ СТАТЬИ", texts(replies)[0])
        self.assertEqual(len(self.documents(replies)), 1)

    async def test_daily_limit(self):
        with mock.patch.dict(os.environ, {"LUCY_ARTICLE_DAILY_LIMIT": "1"}):
            await self.admin.say("/article Sarah Coventry")
            replies = await self.admin.say("/article jet")
        self.assertEqual(len(FakeOpenAI.calls), 1)
        self.assertIn("досье закончились", texts(replies)[0])

    async def test_nothing_found(self):
        text = texts(await self.admin.say("/article Miriam Haskell"))[0]
        self.assertIn("не нашла ничего по теме «Miriam Haskell»", text)
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_ai_error_is_friendly(self):
        error = openai_module.OpenAIError("сбой sk-SECRET-KEY")
        error.code = None
        FakeOpenAI.error = error
        replies = await self.admin.say("/article Sarah Coventry")
        text = "\n".join(texts(replies))
        self.assertIn("ИИ сейчас не ответил", text)
        self.assertNotIn("sk-SECRET", text)
        self.assertEqual(self.documents(replies), [])
        self.assertEqual(library_article.made_today(bot.DB_PATH), 0)


if __name__ == "__main__":
    unittest.main()
