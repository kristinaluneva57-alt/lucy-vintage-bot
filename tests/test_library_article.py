"""Полное исследование темы по всей библиотеке (/article).

Библиотека: большая книга Sarah Coventry (40 содержательных страниц,
выходные данные, указатель), Jet Jewellery и книга, которая ещё
распознаётся. OpenAI подменяется поддельным клиентом — настоящих
запросов нет.

Запуск: python -m unittest discover -s tests
"""

import os
import re
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

import helpers
from helpers import FakeChat, buttons, load_bot, texts

bot = load_bot()
import library_article  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402

openai_module = sys.modules["openai"]

COVENTRY = "Sarah Coventry Jewelry: An Unauthorized Guide for Collectors"
JET = "Jet Jewellery and Ornaments"
NAPIER = "Napier Jewelry"
CONTENT_PAGES = 40

COPYRIGHT = (
    "Sarah Coventry Jewelry. Copyright (c) 1999. All rights reserved. "
    "ISBN 0-7643-0000-0. Printed in China."
)
INDEX = "Index\nSarah Coventry 5, 12, 40\nSarah Coventry boxes 18\nCoro 7\nTrifari 9"


def coventry_page(number):
    sentence = (
        f"Sarah Coventry jewelry of line {number} was sold at home parties by fashion "
        f"directors. The brooches of set {number} are marked SARAH COV. on the clasp, "
        "and collectors look for the original boxes and matching earrings. "
    )
    return f"Chapter {number}. " + sentence * 12  # ≈ 3 000 знаков: страница целиком


SARAH_JET = (
    "Black glass imitating jet was popular. Sarah Coventry also made black glass "
    "brooches in the style of Victorian jet mourning jewellery, sold at home parties."
)


class FakeOpenAI:
    calls = []
    fail_on = set()      # номера вызовов (с 1), которые падают
    error_code = None

    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        FakeOpenAI.calls.append(kwargs)
        if len(FakeOpenAI.calls) in FakeOpenAI.fail_on:
            error = openai_module.OpenAIError("сбой sk-SECRET-KEY")
            error.code = FakeOpenAI.error_code
            raise error
        system = kwargs["messages"][0]["content"]
        user = kwargs["messages"][1]["content"]
        numbers = re.findall(r"^\[(\d+)\]", user, flags=re.MULTILINE)
        if system == library_article.PART_PROMPT:
            answer = "\n".join(f"— Клеймо SARAH COV. на линии [{n}]" for n in numbers[:3]) + " [9999]"
        else:
            cited = sorted(set(re.findall(r"\[(\d+)\]", user)), key=int)
            answer = (
                "КОРОТКО\n**Sarah Coventry** продавала украшения на домашних вечеринках "
                + " ".join(f"[{n}]" for n in cited[:4]) + ".\n\n## ГЛАВНЫЕ ФАКТЫ\n"
                "— Выдуманный факт [9999]."
            )
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=answer))],
            usage=types.SimpleNamespace(prompt_tokens=10000, completion_tokens=1000),
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
        library_article.RUNNING.clear()

        conn = sqlite3.connect(path)
        for doc_id, code, title, key in (
            (7, "DOC-000007", COVENTRY, "books/telegram/a.pdf"),
            (2, "DOC-000002", JET, "books/BOOK-000001/Book 1.pdf"),
            (11, "DOC-000011", NAPIER, "books/browser/n.pdf"),
        ):
            conn.execute(
                "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
                "VALUES (?, ?, ?, ?, 'eng')",
                (doc_id, code, title, key),
            )
        conn.execute(
            "INSERT INTO library_import_jobs (document_id, status, last_completed_page) "
            "VALUES (11, 'processing', 120)"
        )
        pages = [(7, 2, COPYRIGHT), (7, 200, INDEX)]
        pages += [(7, 10 + n, coventry_page(n)) for n in range(1, CONTENT_PAGES + 1)]
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
            "LUCY_ARTICLE_MAX_PAGES": "400",
            "LUCY_ARTICLE_CONFIRM_USD": "0.02",
            "LUCY_ARTICLE_MAX_USD": "1.00",
            "LUCY_ARTICLE_BUDGET_DAY_USD": "1.00",
            "LUCY_ARTICLE_BUDGET_MONTH_USD": "5.00",
        })
        self.env.start()
        self.openai_patch = mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True)
        self.openai_patch.start()
        self.doc_patch = mock.patch.object(helpers.FakeMessage, "reply_document", _reply_document, create=True)
        self.doc_patch.start()
        FakeOpenAI.calls = []
        FakeOpenAI.fail_on = set()
        FakeOpenAI.error_code = None
        self.admin = FakeChat(bot, 999)
        self.user = FakeChat(bot, 1)

    def tearDown(self):
        self.doc_patch.stop()
        self.openai_patch.stop()
        self.env.stop()

    # --- помощники ---

    def query(self, sql, *args):
        conn = sqlite3.connect(bot.DB_PATH)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def pages_snapshot(self):
        return self.query("SELECT * FROM library_pages ORDER BY id")

    @staticmethod
    def documents(replies):
        return [content for kind, content, _ in replies if kind == "document"]

    def last_job(self):
        return library_article.get_job(bot.DB_PATH, self.query("SELECT MAX(id) FROM library_article_jobs")[0][0])

    async def plan_coventry(self):
        replies = await self.admin.say("/article Sarah Coventry")
        return replies, self.last_job()

    async def run_job(self, job):
        await self.admin.press(f"art:go:{job['id']}")
        return await self.admin.run_tasks()

    # --- доступ ---

    async def test_only_admin(self):
        replies = await self.user.say("/article Sarah Coventry")
        self.assertIn("только администратору", texts(replies)[0])
        _, job = await self.plan_coventry()
        replies = await self.user.press(f"art:go:{job['id']}")
        self.assertIn("только администратору", texts(replies)[0])
        await self.user.run_tasks()
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_usage_without_topic(self):
        text = texts(await self.admin.say("/article"))[0]
        self.assertIn("/article Sarah Coventry", text)
        self.assertEqual(FakeOpenAI.calls, [])

    # --- поиск по всей библиотеке ---

    def test_research_reads_every_content_page(self):
        research = library_article.build_research(bot.DB_PATH, "Sarah Coventry")
        pages = {(f["document_code"], f["pdf_page"]) for f in research["fragments"]}
        self.assertEqual(len([p for p in pages if p[0] == "DOC-000007"]), CONTENT_PAGES)  # нет лимита 5 на книгу
        self.assertIn(("DOC-000002", 21), pages)
        self.assertNotIn(("DOC-000007", 2), pages)    # выходные данные — не в ИИ
        self.assertNotIn(("DOC-000007", 200), pages)  # указатель — не в ИИ
        report = research["report"]
        self.assertEqual(report["content_pages"], CONTENT_PAGES + 1)
        self.assertEqual(report["skipped_by_limit"], 0)
        self.assertEqual({tuple(p[::2]) for p in report["service_pages"]},
                         {("DOC-000007", 2), ("DOC-000007", 200)})
        self.assertGreater(len(research["parts"]), 1)
        for part in research["parts"]:  # этап не больше PART_CHARS
            size = sum(len(research["fragments"][n - 1]["text"]) for n in part)
            self.assertLessEqual(size, library_article.PART_CHARS)
        flat = [n for part in research["parts"] for n in part]
        self.assertEqual(flat, list(range(1, len(research["fragments"]) + 1)))  # каждая страница ровно один раз

    def test_page_limit_is_reported(self):
        with mock.patch.dict(os.environ, {"LUCY_ARTICLE_MAX_PAGES": "10"}):
            research = library_article.build_research(bot.DB_PATH, "Sarah Coventry")
        self.assertEqual(len(research["fragments"]), 10)
        self.assertEqual(research["report"]["skipped_by_limit"], CONTENT_PAGES + 1 - 10)

    # --- план и запуск ---

    async def test_big_research_waits_for_button(self):
        replies, job = await self.plan_coventry()
        text = texts(replies)[0]
        self.assertIn("ПЛАН ИССЛЕДОВАНИЯ: Sarah Coventry", text)
        self.assertIn(f"ИИ прочитает содержательных страниц: {CONTENT_PAGES + 1}.", text)
        self.assertIn("Стоимость: не больше ≈", text)
        self.assertEqual(buttons(replies), [f"art:go:{job['id']}", f"art:no:{job['id']}"])
        await self.admin.run_tasks()
        self.assertEqual(FakeOpenAI.calls, [])  # без кнопки денег не тратим
        self.assertEqual(job["status"], "planned")

    async def test_full_research_with_sources_report_and_file(self):
        before = self.pages_snapshot()
        _, job = await self.plan_coventry()
        replies = await self.run_job(job)
        text = "\n".join(texts(replies))
        parts = len(job["research"]["parts"])

        self.assertEqual(len(FakeOpenAI.calls), parts + 1)  # этапы + итог
        self.assertEqual(self.last_job()["status"], "done")
        self.assertIn("ДОСЬЕ ДЛЯ СТАТЬИ", text)
        self.assertIn("Sarah Coventry продавала украшения", text)
        self.assertNotIn("**", text)
        self.assertNotIn("## ", text)
        self.assertNotIn("[9999]", text)  # несуществующий фрагмент убран
        self.assertIn(f"{COVENTRY} (DOC-000007) — PDF-стр.", text)
        self.assertIn("ПОЛНОТА ИССЛЕДОВАНИЯ", text)
        self.assertIn(f"— прочитано ИИ: {CONTENT_PAGES + 1} (все содержательные)", text)
        self.assertIn(f"DOC-000007 · {COVENTRY} — найдено {CONTENT_PAGES + 2}, прочитано {CONTENT_PAGES}", text)
        self.assertIn("DOC-000007 стр. 2, DOC-000007 стр. 200", text)
        self.assertIn(f"DOC-000011 · {NAPIER} ещё не распознана до конца", text)
        self.assertIn(f"Этапов выполнено: {parts} из {parts}", text)

        name, body = self.documents(replies)[0]
        self.assertEqual(name, "dossier_sarah_coventry.txt")
        self.assertIn("ОРИГИНАЛЫ СТРАНИЦ", body)
        self.assertEqual(body.count("Chapter "), CONTENT_PAGES)  # в файле каждая страница
        self.assertEqual(self.pages_snapshot(), before)  # книги не меняются

        calls = self.query("SELECT kind, status, cost_usd FROM library_article_calls ORDER BY id")
        self.assertEqual([k for k, _, _ in calls], ["article_part"] * parts + ["article_final"])
        self.assertTrue(all(s == "done" and c > 0 for _, s, c in calls))
        self.assertIn("стоимость: ", text)

    async def test_small_research_runs_without_button(self):
        replies = await self.admin.say("/article гагат")
        self.assertIn("Начала исследование", texts(replies)[0])
        replies = await self.admin.run_tasks()
        self.assertEqual(len(FakeOpenAI.calls), 1)  # одна страница — один вызов
        self.assertIn(f"{JET} (DOC-000002) — PDF-стр. 21", "\n".join(texts(replies)))

    async def test_done_research_is_free_next_time(self):
        _, job = await self.plan_coventry()
        await self.run_job(job)
        calls = len(FakeOpenAI.calls)
        replies = await self.admin.say("/article sarah   coventry")
        await self.admin.run_tasks()
        self.assertEqual(len(FakeOpenAI.calls), calls)
        self.assertIn("ДОСЬЕ ДЛЯ СТАТЬИ", texts(replies)[0])
        self.assertEqual(len(self.documents(replies)), 1)

    async def test_cancel(self):
        _, job = await self.plan_coventry()
        replies = await self.admin.press(f"art:no:{job['id']}")
        self.assertIn("отменено", texts(replies)[0])
        await self.admin.run_tasks()
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertEqual(library_article.get_job(bot.DB_PATH, job["id"])["status"], "cancelled")
        replies = await self.admin.say("/article Sarah Coventry")  # новая тема — новый план
        self.assertIn("ПЛАН ИССЛЕДОВАНИЯ", texts(replies)[0])

    # --- сбои, перезапуск, бюджет ---

    async def test_resume_after_failure_skips_done_parts(self):
        _, job = await self.plan_coventry()
        parts = len(job["research"]["parts"])
        FakeOpenAI.fail_on = {3, 4}  # третий этап падает и при повторе
        replies = await self.run_job(job)
        text = "\n".join(texts(replies))
        self.assertIn("остановилось", text)
        self.assertIn("/article Sarah Coventry", text)
        self.assertNotIn("SECRET", text)
        self.assertEqual(self.last_job()["status"], "paused")
        self.assertEqual(len(library_article.saved_parts(bot.DB_PATH, job["id"])), 2)

        FakeOpenAI.fail_on = set()
        before = len(FakeOpenAI.calls)
        replies = await self.admin.say("/article Sarah Coventry")
        self.assertIn("уже начиналось", texts(replies)[0])
        self.assertIn("уже сделано: 2", texts(replies)[0])
        await self.admin.press(f"art:go:{job['id']}")
        await self.admin.run_tasks()
        self.assertEqual(len(FakeOpenAI.calls) - before, parts - 2 + 1)  # только оставшиеся этапы + итог
        self.assertEqual(self.last_job()["status"], "done")

    async def test_restart_does_not_spend_automatically(self):
        _, job = await self.plan_coventry()
        library_article.set_status(bot.DB_PATH, job["id"], "running")  # процесс упал посреди работы
        replies = await self.admin.say("/article Sarah Coventry")
        await self.admin.run_tasks()
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertEqual(buttons(replies), [f"art:go:{job['id']}", f"art:no:{job['id']}"])

    async def test_too_expensive_is_not_started(self):
        _, job = await self.plan_coventry()
        with mock.patch.dict(os.environ, {"LUCY_ARTICLE_MAX_USD": "0.001"}):
            replies = await self.admin.press(f"art:go:{job['id']}")
            await self.admin.run_tasks()
        self.assertIn("Не запускаю", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_budget_too_small_is_not_started(self):
        _, job = await self.plan_coventry()
        with mock.patch.dict(os.environ, {"LUCY_ARTICLE_BUDGET_DAY_USD": "0.012"}):
            replies = await self.admin.press(f"art:go:{job['id']}")
            await self.admin.run_tasks()
        self.assertIn("Не запускаю: дневной бюджет досье", texts(replies)[0])
        self.assertEqual(FakeOpenAI.calls, [])

    async def test_daily_budget_pauses_research(self):
        # Бюджет кончился уже во время работы (например, параллельно потратили).
        _, job = await self.plan_coventry()
        await self.admin.press(f"art:go:{job['id']}")
        with mock.patch.dict(os.environ, {"LUCY_ARTICLE_BUDGET_DAY_USD": "0.012"}):
            replies = await self.admin.run_tasks()
        self.assertIn("дневной бюджет досье", "\n".join(texts(replies)))
        self.assertEqual(self.last_job()["status"], "paused")
        self.assertLessEqual(library_article.spent(bot.DB_PATH, "day"), 0.012)

    async def test_nothing_found(self):
        text = texts(await self.admin.say("/article Miriam Haskell"))[0]
        self.assertIn("не нашла содержательных страниц по теме «Miriam Haskell»", text)
        self.assertEqual(FakeOpenAI.calls, [])


if __name__ == "__main__":
    unittest.main()
