"""Русскоязычные ответы по книгам библиотеки.

Модель книги Jet Jewellery and Ornaments: обложка, выходные данные,
оглавление, содержательные страницы, указатель и страница с OCR-мусором.
OpenAI подменяется поддельным клиентом — настоящих запросов нет.

Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

from helpers import FakeChat, buttons, load_bot, texts

bot = load_bot()
import library_ai  # noqa: E402
import library_db  # noqa: E402
import library_search  # noqa: E402
import lucy_ai  # noqa: E402

openai_module = sys.modules["openai"]

JET_TITLE = "Jet Jewellery and Ornaments"

COVER = "SHIRE ARCHAEOLOGY\nJET JEWELLERY\nAND ORNAMENTS\nHelen Muller"

COPYRIGHT = (
    "Published in 1980 by Shire Publications Ltd, Princes Risborough.\n"
    "Copyright (c) Helen Muller 1980. All rights reserved.\n"
    "ISBN 0 85263 518 3\n"
    "Cover: a carved jet brooch from Whitby.\n"
    "Printed in Great Britain."
)

CONTENTS = (
    "CONTENTS\n"
    "List of illustrations 4\n"
    "1. What is jet? 5\n"
    "2. Working jet 9\n"
    "3. Jet in prehistory 13\n"
    "4. The Whitby jet industry 17\n"
    "5. Mourning jewellery 25\n"
    "6. Identifying jet 29\n"
    "Further reading 31\n"
    "Index 32"
)

WHAT_IS_JET = (
    "1\nWhat is jet?\n"
    "Jet is a fossilised wood, the remains of trees similar to the present-day "
    "Araucaria or monkey puzzle tree. Pieces of wood were washed down rivers and "
    "buried in mud on the sea bed, where over millions of years they were "
    "compressed and changed into jet. The finest jet comes from the cliffs on "
    "the Yorkshire coast around Whitby. Jet is light, warm to the touch and can "
    "be carved and polished to a deep black lustre. Hard jet keeps its polish, "
    "while soft jet is more likely to crack."
)

WHITBY = (
    "The Whitby jet industry\n"
    "In the nineteenth century Whitby became the centre of a large jet "
    "industry. In 1850 there were only a few workshops, but by 1870 about two "
    "hundred jet workshops employed some fifteen hundred men and boys. Jet "
    "brooches, lockets and necklaces became fashionable as mourning jewellery "
    "after the death of Prince Albert in 1861. The industry declined after "
    "1880 as fashion changed and cheaper imitations of jet appeared."
)

INDEX = (
    "Index\n"
    "Albert, Prince 25\n"
    "Amber 6, 30\n"
    "Bog oak 29\n"
    "French jet 30\n"
    "Jet, hard 7\n"
    "Jet, soft 7\n"
    "Spanish jet 21\n"
    "Whitby 17-24\n"
    "Vulcanite 30"
)

GARBAGE = "J3t ~~ |l| ,,; ;; 8a9 %% -- jet ..// ll1 ;;;; ^^ jet == ## 1l1 ,, jet"

PAGES = [
    (1, None, COVER),
    (2, None, COPYRIGHT),
    (4, None, CONTENTS),
    (5, "1", WHAT_IS_JET),
    (11, None, GARBAGE),
    (19, "17", WHITBY),
    (34, "32", INDEX),
]


def page_text(pdf_page):
    for number, _, text in PAGES:
        if number == pdf_page:
            return library_search_cleaned(text)
    raise KeyError(pdf_page)


def library_search_cleaned(text):
    import library_importer

    return library_importer.clean_ocr_text(text)


class FakeOpenAI:
    """Поддельный клиент OpenAI: запоминает запросы, отвечает заданным текстом."""

    calls = []
    answer = "Гагат — это окаменевшая древесина [1]. Лучший гагат добывали у Уитби [2]. Его носили как траурные украшения [3] [7]."
    error = None

    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        if FakeOpenAI.error is not None:
            raise FakeOpenAI.error
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=FakeOpenAI.answer))],
            usage=types.SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def make_api_error(code=None):
    error = openai_module.OpenAIError("сбой sk-SECRET-KEY")
    error.code = code
    return error


class LibraryAITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        lucy_ai.init_ai_usage_table(path)
        library_ai.init_tables(path)

        import library_importer

        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (2, 'DOC-000002', ?, 'books/BOOK-000001/Book 1.pdf', 'eng')",
            (JET_TITLE,),
        )
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (9, 'DOC-000009', 'TEST IMPORTER DOCUMENT', 'test://x', 'en')"
        )
        for pdf_page, printed, text in PAGES:
            conn.execute(
                "INSERT INTO library_pages (document_id, pdf_page, printed_page, "
                "original_text, cleaned_text, extraction_status) VALUES (2, ?, ?, ?, ?, 'extracted')",
                (pdf_page, printed, text, library_importer.clean_ocr_text(text)),
            )
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) "
            "VALUES (9, 1, 'jet jet jet test', 'jet jet jet test')"
        )
        conn.commit()
        conn.close()

        self.env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "LUCY_LIBRARY_AI_ENABLED": "1",
            "LUCY_LIBRARY_AI_DAILY_LIMIT": "10",
            "LUCY_LIBRARY_AI_DAILY_TOTAL": "200",
            "LUCY_AI_ENABLED": "0",  # обычный чат выключен — библиотека от него не зависит
        })
        self.env.start()
        self.openai_patch = mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True)
        self.openai_patch.start()
        FakeOpenAI.calls = []
        FakeOpenAI.error = None
        FakeOpenAI.answer = (
            "Гагат — это окаменевшая древесина [1]. Лучший гагат добывали у Уитби [2]. "
            "Его носили как траурные украшения [3] [7]."
        )
        self.chat = FakeChat(bot, 1)

    def tearDown(self):
        self.openai_patch.stop()
        self.env.stop()

    def pages_snapshot(self):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return conn.execute(
                "SELECT document_id, pdf_page, original_text, cleaned_text, russian_text "
                "FROM library_pages ORDER BY id"
            ).fetchall()
        finally:
            conn.close()

    async def search(self, query, chat=None):
        chat = chat or self.chat
        await chat.say(bot.BTN_SEARCH)
        return await chat.say(query)

    # --- ранжирование без ИИ ---

    def test_page_quality_detects_service_pages(self):
        self.assertLess(library_search.page_quality(CONTENTS), 0.3)
        self.assertLess(library_search.page_quality(COPYRIGHT), 0.3)
        self.assertLess(library_search.page_quality(INDEX), 0.3)
        self.assertLess(library_search.page_quality(GARBAGE), 0.3)
        self.assertGreater(library_search.page_quality(WHAT_IS_JET), 0.6)
        self.assertGreater(library_search.page_quality(WHITBY), 0.6)

    def test_gagat_ranks_content_pages_first(self):
        plan = library_search.build_search_plan("гагат")
        results = library_search.search_pages(bot.DB_PATH, plan, limit=6)
        top = [r["pdf_page"] for r in results[:2]]
        self.assertEqual(sorted(top), [5, 19])
        self.assertNotIn("TEST IMPORTER DOCUMENT", [r["title"] for r in results])

    def test_fragments_skip_contents_and_copyright(self):
        plan = library_search.build_search_plan("гагат")
        results = library_search.search_pages(bot.DB_PATH, plan, limit=6)
        fragments = library_ai.select_fragments(results)
        self.assertTrue(fragments)
        self.assertLessEqual(len(fragments), library_ai.MAX_FRAGMENTS)
        self.assertLessEqual(sum(len(f["text"]) for f in fragments), library_ai.MAX_CONTEXT_CHARS)
        self.assertEqual({f["pdf_page"] for f in fragments}, {5, 19})
        joined = " ".join(f["text"] for f in fragments)
        for junk in ("ISBN", "CONTENTS", "All rights reserved", "Index"):
            self.assertNotIn(junk, joined)
        for fragment in fragments:
            # Фрагмент — точный кусок распознанного текста страницы.
            self.assertIn(fragment["text"].strip("…"), page_text(fragment["pdf_page"]))

    # --- ответ ИИ ---

    async def test_gagat_answer_in_russian_with_real_sources(self):
        before = self.pages_snapshot()
        replies = await self.search("гагат")
        answer = texts(replies)[0]

        self.assertIn("ОТВЕТ ПО КНИГАМ БИБЛИОТЕКИ", answer)
        self.assertIn("окаменевшая древесина [1]", answer)
        self.assertIn(f"{JET_TITLE} — PDF-стр. 5 (в книге стр. 1)", answer)
        self.assertIn(f"{JET_TITLE} — PDF-стр. 19 (в книге стр. 17)", answer)
        self.assertNotIn("[7]", answer)  # ссылка на несуществующий фрагмент убрана
        self.assertNotIn("PDF-стр. 4", answer)
        self.assertNotIn("PDF-стр. 2", answer)
        self.assertIn("Пересказ и перевод сделал ИИ", answer)

        # В OpenAI ушли только короткие фрагменты, не книга.
        self.assertEqual(len(FakeOpenAI.calls), 1)
        call = FakeOpenAI.calls[0]
        self.assertEqual(call["model"], "gpt-4.1-mini")
        self.assertEqual(call["max_completion_tokens"], library_ai.MAX_ANSWER_TOKENS)
        system, user = call["messages"][0]["content"], call["messages"][1]["content"]
        self.assertIn("ТОЛЬКО по фрагментам", system)
        self.assertIn("недостаточно сведений", system)
        self.assertIn("гагат", user)
        self.assertLess(len(user), library_ai.MAX_CONTEXT_CHARS + 1000)
        self.assertNotIn("ISBN", user)
        self.assertNotIn("CONTENTS", user)

        self.assertEqual(self.pages_snapshot(), before)  # страницы не изменились
        self.assertEqual(library_ai.used_today(bot.DB_PATH, 1), 1)

    async def test_original_button_shows_exact_text(self):
        replies = await self.search("гагат")
        data = buttons(replies[:1])
        self.assertTrue(data)
        first = data[0]
        self.assertTrue(first.startswith("lorig:"))

        replies = await self.chat.press(first)
        original = texts(replies)[0]
        self.assertIn("ОРИГИНАЛ [1]", original)
        self.assertIn(JET_TITLE, original)
        self.assertIn("PDF-стр.", original)
        body = original.split("опечатки OCR):\n\n", 1)[1]
        page = 5 if "PDF-стр. 5" in original else 19
        self.assertIn(body.strip("…"), page_text(page))

    async def test_repeated_query_uses_cache(self):
        await self.search("гагат")
        await self.search("  Гагат!  ")
        self.assertEqual(len(FakeOpenAI.calls), 1)
        self.assertEqual(library_ai.used_today(bot.DB_PATH, 1), 1)

    async def test_cache_refreshes_when_library_grows(self):
        await self.search("гагат")
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) "
            "VALUES (2, 40, ?, ?)",
            (WHAT_IS_JET + " New page.", WHAT_IS_JET + " New page."),
        )
        conn.commit()
        conn.close()
        await self.search("гагат")
        self.assertEqual(len(FakeOpenAI.calls), 2)

    # --- лимиты ---

    async def test_daily_limit_falls_back_to_plain_search(self):
        os.environ["LUCY_LIBRARY_AI_DAILY_LIMIT"] = "1"
        await self.search("гагат")
        replies = await self.search("брошь гагат")
        joined = "\n".join(texts(replies))
        self.assertIn("пересказы по книгам закончились", joined)
        self.assertIn("Оригинальный текст", joined)
        self.assertIn(JET_TITLE, joined)
        self.assertEqual(len(FakeOpenAI.calls), 1)

    async def test_total_limit(self):
        os.environ["LUCY_LIBRARY_AI_DAILY_TOTAL"] = "0"
        replies = await self.search("гагат")
        self.assertIn("закончились", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_admin_has_no_limit(self):
        os.environ["LUCY_LIBRARY_AI_DAILY_LIMIT"] = "0"
        admin = FakeChat(bot, 999)
        replies = await self.search("гагат", chat=admin)
        self.assertIn("ОТВЕТ ПО КНИГАМ", texts(replies)[0])
        self.assertEqual(library_ai.used_today(bot.DB_PATH, 999), 0)

    async def test_user_limits_are_separate_from_chat_ai(self):
        await self.search("гагат")
        self.assertEqual(lucy_ai.used_today(bot.DB_PATH, 1), 0)

    # --- ошибки и выключатель ---

    async def test_api_error_falls_back_without_secrets(self):
        FakeOpenAI.error = make_api_error()
        replies = await self.search("гагат")
        joined = "\n".join(texts(replies))
        self.assertIn("Пересказ по-русски сейчас недоступен", joined)
        self.assertIn("Оригинальный текст", joined)
        self.assertIn("PDF-стр. 5", joined)
        self.assertNotIn("SECRET", joined)
        self.assertEqual(library_ai.used_today(bot.DB_PATH, 1), 0)

    async def test_quota_error_falls_back(self):
        FakeOpenAI.error = make_api_error("insufficient_quota")
        replies = await self.search("гагат")
        self.assertIn("недоступен", texts(replies)[0])

    async def test_kill_switch_uses_plain_search(self):
        os.environ["LUCY_LIBRARY_AI_ENABLED"] = "0"
        replies = await self.search("гагат")
        joined = "\n".join(texts(replies))
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertIn("Оригинальный текст", joined)
        self.assertNotIn("ОТВЕТ ПО КНИГАМ", joined)

    async def test_no_api_key_uses_plain_search(self):
        os.environ["OPENAI_API_KEY"] = ""
        replies = await self.search("гагат")
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertIn("Оригинальный текст", "\n".join(texts(replies)))

    async def test_nothing_found_does_not_call_ai(self):
        replies = await self.search("бакелит")
        self.assertIn("ничего не нашла", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    # --- режимы раздельны ---

    async def test_chat_mode_does_not_use_library(self):
        os.environ["LUCY_AI_ENABLED"] = "1"
        FakeOpenAI.answer = "Гагат — это поделочный камень."
        replies = await self.chat.say("Что такое гагат?")
        self.assertEqual(len(FakeOpenAI.calls), 1)
        system = FakeOpenAI.calls[0]["messages"][0]["content"]
        self.assertNotIn("ТОЛЬКО по фрагментам", system)
        self.assertIn("ответ ИИ", texts(replies)[0])
        self.assertEqual(library_ai.used_today(bot.DB_PATH, 1), 0)

    async def test_chat_kill_switch_does_not_disable_library(self):
        os.environ["LUCY_AI_ENABLED"] = "0"
        replies = await self.search("гагат")
        self.assertIn("ОТВЕТ ПО КНИГАМ", texts(replies)[0])

    def test_clean_answer_and_sources(self):
        fragments = [
            {"n": 1, "title": "A", "pdf_page": 3, "printed_page": None, "text": "x"},
            {"n": 2, "title": "B", "pdf_page": 8, "printed_page": "6", "text": "y"},
        ]
        answer = library_ai.clean_answer("Факт [2]. Выдумка [5].", fragments)
        self.assertEqual(answer, "Факт [2]. Выдумка.")
        reply, numbers = library_ai.format_reply(answer, fragments)
        self.assertEqual(numbers, [2])
        self.assertIn("[2] B — PDF-стр. 8 (в книге стр. 6)", reply)
        self.assertNotIn("PDF-стр. 3", reply)


if __name__ == "__main__":
    unittest.main()
