"""Исправления по первому тесту на Railway (карточка OMG-000006, Sarah Coventry).

1. Сравнение падало с ошибкой 400: в промпте сравнения не было слова «JSON»,
   а JSON-режим OpenAI его требует. Поддельный OpenAI в тестах теперь
   проверяет это правило так же, как настоящий.
2. Кандидаты: книга «Sarah Coventry Jewelry» должна находиться по марке
   с клейма, а оглавление и указатель книги Judith Miller — исключаться.
3. Ошибка сравнения: честный частичный результат, попытка не списана,
   расход на описание учтён.

Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import tempfile
import types
import unittest
from unittest import mock

from helpers import FakeChat, buttons, load_bot, texts

bot = load_bot()
import attribution  # noqa: E402
import library_db  # noqa: E402
import library_importer  # noqa: E402
import page_images  # noqa: E402
import visual_ai  # noqa: E402
import visual_index  # noqa: E402
import test_visual as tv  # noqa: E402  (только вспомогательные функции и FakeOpenAI)

OWNER, ADMIN = 1, 999

MILLER = {
    3: "Published by Miller's, 2003\nISBN 1 84000 000 0\nAll rights reserved\nPrinted in China",
    9: "CONTENTS\nIntroduction 6\nThe 1940s 40\nThe 1950s 88\nSarah Coventry 207\nThe 1960s 210\nIndex 258",
    207: "Sarah Coventry jewellery was sold at home parties from 1949.\n"
         "A brooch by Sarah Coventry with clear rhinestones, c.1960.",
    120: "A Trifari necklace with red glass stones, 1955. Many American firms "
         "made Y-shaped necklaces in the 1950s.",
    258: "INDEX\nAvon 45, 61\nBoucher 102\nCoro 12-14\nCoventry, Sarah 207, 211\n"
         "Dior 88\nHaskell, Miriam 30\nTrifari 120, 152",
}
COVENTRY = {
    2: "Sarah Coventry Jewelry\nCopyright 2005\nISBN 0-000-00000-0\nAll rights reserved",
    5: "Contents\nIntroduction 7\nThe home party plan 12\nNecklaces 40\nBrooches 90\nAdvertising 118",
    54: "Plate 54\nNecklace and earrings, 1964\nY-shaped necklace with five red stones\nMatching earrings",
    120: tv.SC_PAGES[120],
}


def add_book(conn, doc_id, title, pages, total):
    tv.add_book(conn, doc_id, title, pages, total_pages=total)


class SarahCoventryLibraryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        attribution.init_tables(path)

        conn = sqlite3.connect(path)
        add_book(conn, 5, "Costume Jewelry (Judith Miller)", MILLER, 260)
        add_book(conn, 7, "Sarah Coventry Jewelry", COVENTRY, 182)
        add_book(conn, 2, "Jet Jewellery and Ornaments", tv.JET_PAGES, 30)
        conn.execute(
            "INSERT INTO items (item_code, front_photo, back_photo, mark_photo, owner_user_id, mark_text, notes) "
            "VALUES ('OMG-000006', 'F6', 'B6', 'M6', ?, 'SARAH COVENTRY', 'Серебристое Y-образное колье')",
            (OWNER,),
        )
        conn.execute(
            "INSERT INTO items (item_code, front_photo, mark_photo, owner_user_id, mark_text) "
            "VALUES ('OMG-000009', 'F9', 'M9', ?, 'SARAH COVENTRY')",
            (ADMIN,),
        )
        conn.commit()
        conn.close()
        for doc_id in (5, 7, 2):
            visual_index.index_document(path, doc_id)

        self.r2 = {}
        helper = types.SimpleNamespace(renders=[], readable_ad=True)
        patches = [
            mock.patch.object(page_images, "r2_get", lambda key: self.r2.get(key)),
            mock.patch.object(page_images, "r2_put", lambda key, data: self.r2.__setitem__(key, data)),
            mock.patch.object(page_images, "_render",
                              lambda *a, **k: tv.VisualAttributionTest.fake_render(helper, *a, **k)),
            mock.patch.object(tv.openai_module, "OpenAI", tv.FakeOpenAI, create=True),
            mock.patch.dict(os.environ, {
                "OPENAI_API_KEY": "sk-test", "LUCY_VISUAL_MODE": "all",
                "LUCY_VISUAL_FREE_PER_MONTH": "3", "LUCY_VISUAL_BUDGET_DAY_USD": "1",
                "LUCY_VISUAL_BUDGET_MONTH_USD": "10",
            }),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        tv.FakeOpenAI.calls = []
        tv.FakeOpenAI.error = None
        tv.FakeOpenAI.fail_kind = None
        tv.FakeOpenAI.compare_override = None
        self.owner = tv.VisualAttributionTest.chat(self, OWNER)
        self.admin = tv.VisualAttributionTest.chat(self, ADMIN)

    async def research(self, chat, code):
        await chat.press(f"vattr:{code}")
        return await chat.run_tasks()

    # --- причина ошибки сравнения ---

    def test_every_prompt_mentions_json(self):
        for prompt in (attribution.DESCRIBE_SYSTEM, attribution.COMPARE_SYSTEM, attribution.READ_SYSTEM):
            self.assertIn("json", prompt.lower())

    def test_json_word_guard_for_any_prompt(self):
        # Промпт без «JSON» (как сравнение в PR #16): настоящий OpenAI вернул бы 400.
        data = visual_ai.call_json(
            bot.DB_PATH, kind="read_text", system="Read the text.",
            content=[{"type": "text", "text": "crop"}], images=1, max_tokens=200,
        )
        self.assertIsInstance(data, dict)
        sent_system = tv.FakeOpenAI.calls[-1]["messages"][0]["content"]
        self.assertIn("JSON", sent_system)

    def test_error_diagnostics_are_safe(self):
        details = visual_ai.describe_error(tv.JsonModeError())
        self.assertIn("JsonModeError", details)
        self.assertIn("HTTP 400", details)
        self.assertIn("type=invalid_request_error", details)
        self.assertIn("param=messages", details)
        self.assertIn("must contain the word 'json'", details)

        error = tv.openai_module.OpenAIError("bad key sk-abcdef123456 data:image/jpeg;base64," + "A" * 200)
        scrubbed = visual_ai.describe_error(error)
        self.assertNotIn("sk-abcdef", scrubbed)
        self.assertNotIn("AAAA" * 10, scrubbed)

    # --- подбор кандидатов ---

    def test_brand_book_found_and_service_pages_excluded(self):
        candidates, excluded = visual_index.search_candidates(
            # «sarah» и «coventry» отдельными словами — как в бесплатном поиске
            # на Railway: они совпадают с указателем «Coventry, Sarah 207».
            bot.DB_PATH, ["necklace", "y necklace", "red stones", "sarah", "coventry"], ["sarah coventry"],
            limit=8, with_excluded=True,
        )
        top4 = [(c["document_code"], c["pdf_page"]) for c in candidates[:4]]
        # Первыми — страницы книги о марке: реклама и подпись без названия марки.
        self.assertEqual({top4[0], top4[1]}, {("DOC-000007", 54), ("DOC-000007", 120)})
        # Страница другой книги, где марка упомянута, — тоже кандидат.
        self.assertIn(("DOC-000005", 207), top4)
        # Страница о другой марке (Trifari) не занимает место среди сравниваемых.
        self.assertNotIn(("DOC-000005", 120), top4)
        pages = {(c["document_code"], c["pdf_page"]) for c in candidates}
        for service in [("DOC-000005", 9), ("DOC-000005", 258), ("DOC-000005", 3),
                        ("DOC-000007", 2), ("DOC-000007", 5)]:
            self.assertNotIn(service, pages)
        reasons = {(code, page): reason for code, page, reason in excluded}
        self.assertEqual(reasons[("DOC-000005", 9)], "оглавление")
        self.assertEqual(reasons[("DOC-000005", 258)], "указатель")
        self.assertEqual(reasons[("DOC-000007", 2)], "выходные данные")
        self.assertEqual(reasons[("DOC-000007", 5)], "оглавление")
        why = next(c for c in candidates if c["pdf_page"] == 54)["why"]
        self.assertIn("книга о марке Sarah Coventry", why)

    def test_service_reason_keeps_captions(self):
        self.assertIsNone(visual_index.service_reason(COVENTRY[54]))
        self.assertIsNone(visual_index.service_reason(
            "Necklace and earrings, 1964\nBracelet, 1965\nBrooch, 1962\nNecklace, 1963\nEarrings, 1961\nPin, 1966"
        ))
        self.assertIsNone(visual_index.service_reason(
            "Fig. 12\nA necklace\nFig. 13\nEarrings\nFig. 14\nBracelet"
        ))

    def test_free_search_by_mark_finds_brand_book(self):
        result = attribution._run_free(bot.DB_PATH, {"mark_text": "SARAH COVENTRY", "notes": ""})
        codes = [(e["document_code"], e["pdf_page"]) for e in result["evidence"]]
        self.assertTrue(codes)
        self.assertEqual(codes[0][0], "DOC-000007")
        self.assertEqual(sum(1 for code, _ in codes if code == "DOC-000007"), 2)
        self.assertNotIn(("DOC-000005", 258), codes)
        self.assertNotIn(("DOC-000005", 9), codes)

    # --- полное исследование и частичный результат ---

    async def test_full_research_now_compares(self):
        replies = await self.research(self.owner, "OMG-000006")
        kinds = [c["kind"] for c in tv.FakeOpenAI.calls]
        self.assertEqual(kinds[:2], ["describe", "compare"])
        report = texts(replies)[0]
        self.assertIn("ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ", report)
        self.assertIn("DOC-000007 · Sarah Coventry Jewelry · PDF-стр. 120", report)
        self.assertNotIn("DOC-000005 · Costume Jewelry (Judith Miller) · PDF-стр. 258", report)
        self.assertNotIn("DOC-000005 · Costume Jewelry (Judith Miller) · PDF-стр. 9", report)
        compare = next(c for c in tv.FakeOpenAI.calls if c["kind"] == "compare")
        labels = [p["text"] for p in compare["content"] if p.get("type") == "text" and p["text"].startswith("P")]
        self.assertTrue(labels)
        self.assertTrue(labels[0].startswith("P1: DOC-000007") and labels[1].startswith("P2: DOC-000007"), labels)
        for wrong in ("DOC-000005, PDF page 258", "DOC-000005, PDF page 9.", "DOC-000005, PDF page 120"):
            self.assertFalse(any(wrong in label for label in labels), labels)

    async def test_compare_error_gives_honest_partial_result(self):
        tv.FakeOpenAI.fail_kind = "compare"
        replies = await self.research(self.admin, "OMG-000009")
        report = texts(replies)[0]
        self.assertIn("ИССЛЕДОВАНИЕ НЕ ЗАВЕРШЕНО", report)
        self.assertIn("ошибка OpenAI", report)
        self.assertIn("Бесплатная попытка не списана", report)
        self.assertIn("Серебристое Y-образное колье", report)          # описание сохранено
        self.assertIn("Клеймо, указанное владелицей: SARAH COVENTRY", report)
        self.assertIn("с фото НЕ сравнивались", report)
        self.assertIn("DOC-000007 · Sarah Coventry Jewelry · PDF-стр.", report)
        self.assertNotIn("Производитель: Sarah", report)
        self.assertNotIn("совпадение модели", report)
        data = buttons(replies[:1])
        self.assertTrue(any(d.startswith("vsrc:") for d in data))
        self.assertFalse(any(d.startswith("vsave:") for d in data))

        # Расходы: описание оплачено по факту, сравнение — ошибка, 0 $, с диагностикой.
        rows = visual_ai.calls_for(bot.DB_PATH, 1)
        self.assertEqual([(r[0], r[6]) for r in rows], [("describe", "done"), ("compare", "failed")])
        self.assertAlmostEqual(rows[0][4], (5200 * 0.40 + 420 * 1.60) / 1e6, places=10)
        self.assertEqual(rows[1][4], 0)
        self.assertIn("HTTP 400", rows[1][7])
        self.assertIn("param=messages", rows[1][7])
        self.assertIn("kind=compare", rows[1][8])
        self.assertIn("images=", rows[1][8])
        self.assertIn("payload_kb=", rows[1][8])

        breakdown = texts(replies)[-1]
        self.assertIn("ошибка: JsonModeError · HTTP 400", breakdown)
        self.assertIn("запрос: kind=compare", breakdown)
        self.assertIn("Кандидаты для сравнения", breakdown)
        self.assertIn("книга о марке Sarah Coventry", breakdown)

        record = attribution.get_attribution(bot.DB_PATH, 1)
        self.assertEqual(record["mode"], "partial")

    async def test_failed_research_does_not_use_free_attempt(self):
        os.environ["LUCY_VISUAL_FREE_PER_MONTH"] = "1"
        tv.FakeOpenAI.fail_kind = "compare"
        await self.research(self.owner, "OMG-000006")
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 0)
        # Следующая попытка снова с ИИ, а не бесплатный поиск.
        tv.FakeOpenAI.fail_kind = None
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("UPDATE items SET front_photo = 'F7' WHERE item_code = 'OMG-000006'")
        conn.commit()
        conn.close()
        replies = await self.research(self.owner, "OMG-000006")
        self.assertIn("ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ", texts(replies)[0])
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 1)

    async def test_partial_result_is_not_cached(self):
        tv.FakeOpenAI.fail_kind = "compare"
        await self.research(self.owner, "OMG-000006")
        tv.FakeOpenAI.fail_kind = None
        calls = len(tv.FakeOpenAI.calls)
        replies = await self.research(self.owner, "OMG-000006")
        self.assertGreater(len(tv.FakeOpenAI.calls), calls)  # повтор с теми же фото — новая попытка
        self.assertIn("ИССЛЕДОВАНИЕ ПО БИБЛИОТЕКЕ", texts(replies)[0])

    async def test_vstats_after_failed_compare(self):
        tv.FakeOpenAI.fail_kind = "compare"
        await self.research(self.admin, "OMG-000009")
        stats = texts(await self.admin.say("/vstats"))[0]
        paid = (5200 * 0.40 + 420 * 1.60) / 1e6
        self.assertIn(f"Месяц: {paid:.4f} $ оплачено + 0.0000 $ возможно + 0.0000 $ в работе", stats)
        self.assertIn("Ошибок OpenAI (не оплачено): 1", stats)

    async def test_card_buttons_unchanged(self):
        replies = await self.owner.press("item:OMG-000006")
        self.assertEqual(
            [d.split(":")[0] for d in buttons(replies)],
            ["details", "vpre", "edit", "del"],
        )


if __name__ == "__main__":
    unittest.main()
