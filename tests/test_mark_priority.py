"""Клеймо, указанное владелицей, — главный факт исследования.

Регрессия OMG-000006 (Railway): в карточке SARAH COVENTRY, модель прочитала
на фото «Sakai Tamaru», и исследование ушло не туда. Проверяем, что клеймо
владелицы используется в поиске, выборе книги по марке, описании,
сравнении и отчёте, а чтение ИИ его не подменяет.

Запуск: python -m unittest discover -s tests
"""

import sqlite3
import unittest

from helpers import texts

import test_visual as tv
from test_visual import FakeOpenAI, attribution, bot, visual_index

OWNER = tv.OWNER

SAKAI = {
    "type_ru": "колье",
    "description_ru": "Серебристое Y-образное колье с пятью бордово-красными овальными вставками.",
    "stones": [{"count": 5, "shape": "oval", "colour": "dark red"}],
    "metal_tone": "silver tone",
    "construction": ["Y-shaped drop"],
    "marks": [{"text": "Sakai Tamaru", "legible": True}],
    "search_terms": ["necklace", "Y necklace", "red stones", "silvertone", "collana", "Sakai Tamaru"],
}

# Книга-«приманка»: много страниц с теми же общими словами, подписями и ценами.
DECOY_PAGES = {
    n: f"Fig. {n} Silvertone Y necklace with red stones, collana. Necklace price $25."
    for n in range(1, 31)
}


def all_none(labels):
    return {
        "pages": [{"label": label, "verdict": "none", "matching_features": [],
                   "differences": ["другая конструкция"], "confidence": 0.1}
                  for label in labels.values()],
        "summary_ru": "Совпадений нет.",
    }


class MarkPriorityTest(unittest.IsolatedAsyncioTestCase):
    chat = tv.VisualAttributionTest.chat
    fake_render = tv.VisualAttributionTest.fake_render
    research = tv.VisualAttributionTest.research

    def setUp(self):
        tv.VisualAttributionTest.setUp(self)
        conn = sqlite3.connect(bot.DB_PATH)
        tv.add_book(conn, 11, "Costume Jewelry Price Guide", DECOY_PAGES, total_pages=30)
        conn.execute(
            "INSERT INTO items (item_code, front_photo, back_photo, mark_photo, owner_user_id, mark_text, "
            "mark_status) VALUES ('OMG-000006', 'F6', 'B6', 'M6', ?, 'SARAH COVENTRY', 'yes')",
            (OWNER,),
        )
        conn.execute(
            "INSERT INTO items (item_code, front_photo, back_photo, mark_photo, owner_user_id, mark_status) "
            "VALUES ('OMG-000007', 'F7', 'B7', 'M7', ?, 'yes')",
            (OWNER,),
        )
        conn.commit()
        conn.close()
        visual_index.index_document(bot.DB_PATH, 11)
        FakeOpenAI.describe_override = SAKAI

    def calls(self, kind):
        return [c for c in FakeOpenAI.calls if c["kind"] == kind]

    def item(self, code):
        conn = sqlite3.connect(bot.DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            return dict(conn.execute("SELECT * FROM items WHERE item_code = ?", (code,)).fetchone())
        finally:
            conn.close()

    def compared_pages(self):
        pages = []
        for part in self.calls("compare")[-1]["content"]:
            if part.get("type") == "text" and part["text"].startswith("P"):
                pages.append(part["text"].split(",")[0:2])
        return pages

    async def test_omg_000006_owner_mark_is_not_replaced(self):
        _, replies = await self.research(code="OMG-000006")
        report = "\n".join(texts(replies))

        # ① Описание: модели сообщено клеймо владелицы, перечитывать не просят.
        describe = self.calls("describe")[0]["content"][0]["text"]
        self.assertIn("OWNER-RECORDED MARK", describe)
        self.assertIn("«SARAH COVENTRY»", describe)
        self.assertIn("Do NOT re-read", describe)
        self.assertTrue(describe.startswith("Photos of one piece: front, back"))  # фото клейма не первое

        # ② Поиск и выбор книги: страницы Sarah Coventry — первыми, «приманка» не вытесняет их.
        compared = [label for label, _ in self.compared_pages()]  # «P1: DOC-000007»
        self.assertGreaterEqual(len(compared), 2)
        self.assertTrue(all(label.endswith("DOC-000007") for label in compared[:2]), compared)
        self.assertIn("P1: DOC-000007", compared)

        # ③ Сравнение: модели передано клеймо владелицы как факт.
        compare = self.calls("compare")[0]["content"][0]["text"]
        self.assertIn("OWNER-RECORDED MARK on the USER piece: «SARAH COVENTRY»", compare)
        self.assertNotIn("Sakai", compare)

        # ④ Отчёт: клеймо не подменено, расхождение показано отдельно.
        self.assertIn("🔖 Клеймо, указанное владелицей: SARAH COVENTRY", report)
        self.assertIn("📷 Что видно на фотографии:", report)
        self.assertIn("Автоматическое чтение фотографии отличается от указанной надписи: «Sakai Tamaru»", report)
        self.assertIn("📖 Что подтверждено историческим источником:", report)
        self.assertIn("Производитель: Sarah Coventry — по клейму, указанному владелицей", report)
        self.assertNotIn("Производитель: Sakai", report)
        self.assertIn("не доказывает подлинность", report)

        # Карточка не изменена.
        self.assertEqual(self.item("OMG-000006")["mark_text"], "SARAH COVENTRY")

    async def test_ai_reads_other_known_brand(self):
        """Самая опасная подмена: модель читает на фото другую известную марку.
        Раньше её марка шла ПЕРВОЙ, и отчёт писал «Производитель: Coro (по клейму)»."""
        FakeOpenAI.describe_override = dict(SAKAI, marks=[{"text": "CORO", "legible": True}],
                                           search_terms=["necklace", "Coro necklace", "red stones"])
        _, replies = await self.research(code="OMG-000006")
        report = "\n".join(texts(replies))
        self.assertIn("Производитель: Sarah Coventry — по клейму, указанному владелицей", report)
        self.assertNotIn("Производитель: Coro", report)
        self.assertIn("отличается от указанной надписи: «CORO»", report)
        result = attribution.get_attribution(bot.DB_PATH, 1)["result"]
        self.assertEqual(result["brands_from_mark"], ["sarah coventry"])
        self.assertNotIn("coro necklace", result["terms"])

    async def test_ai_guess_never_used_for_search(self):
        await self.research(code="OMG-000006")
        record = attribution.get_attribution(bot.DB_PATH, 1)
        result = record["result"]
        self.assertEqual(result["owner_mark"], "SARAH COVENTRY")
        self.assertEqual(result["brands_from_mark"], ["sarah coventry"])
        self.assertEqual(result["brand_source"], "owner")
        self.assertNotIn("sakai tamaru", [t.lower() for t in result["terms"]])
        self.assertEqual(result["mark_conflict"], ["Sakai Tamaru"])
        self.assertTrue(all("клеймо владелицы" in c["why"] for c in result["candidates"][:2]))

    async def test_no_match_is_not_turned_into_attribution(self):
        FakeOpenAI.compare_override = all_none
        _, replies = await self.research(code="OMG-000006")
        report = "\n".join(texts(replies))
        self.assertIn("🔖 Клеймо, указанное владелицей: SARAH COVENTRY", report)
        self.assertIn("атрибуция не выдаётся", report)
        self.assertIn("Проверены страницы, связанные с клеймом «SARAH COVENTRY»", report)
        self.assertNotIn("Производитель:", report)
        self.assertNotIn("Модель:", report)
        record = attribution.get_attribution(bot.DB_PATH, 1)
        self.assertIsNone(attribution.summary_for_card(record["result"]))  # сохранять нечего
        self.assertEqual(self.item("OMG-000006")["attribution_status"], "не определено")

    async def test_card_without_mark_ai_reading_is_only_a_guess(self):
        FakeOpenAI.describe_override = dict(SAKAI, marks=[{"text": "SARAH COVENTRY", "legible": True}],
                                           search_terms=["necklace", "Y necklace", "red stones"])
        _, replies = await self.research(code="OMG-000007")
        report = "\n".join(texts(replies))
        describe = self.calls("describe")[0]["content"][0]["text"]
        self.assertNotIn("OWNER-RECORDED MARK", describe)
        self.assertTrue(describe.startswith("Photos of one piece: mark"))  # клеймо читается с фото
        self.assertIn("🔖 Клеймо в карточке не указано.", report)
        self.assertIn("Надпись, которую ИИ прочитал на фото (предположение, не подтверждено): «SARAH COVENTRY»",
                      report)
        self.assertIn("по надписи, прочитанной ИИ на фото (не подтверждено владелицей)", report)
        self.assertIn("клеймо прочитано ИИ с фото, в карточке не указано", report)
        record = attribution.get_attribution(bot.DB_PATH, 1)
        self.assertEqual(record["result"]["brand_source"], "photo")
        # В карточку марка «по фото» не записывается.
        summary = attribution.summary_for_card(record["result"])
        self.assertNotIn("Sarah Coventry ", summary.split(" — ")[0])
        self.assertIsNone(self.item("OMG-000007")["mark_text"])

    async def test_changed_mark_is_not_served_from_cache(self):
        await self.research(code="OMG-000007")  # без клейма
        describes = len(self.calls("describe"))
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("UPDATE items SET mark_text = 'SARAH COVENTRY' WHERE item_code = 'OMG-000007'")
        conn.commit()
        conn.close()
        _, replies = await self.research(code="OMG-000007")
        self.assertEqual(len(self.calls("describe")), describes + 1)  # новое исследование, не кэш
        report = "\n".join(texts(replies))
        self.assertNotIn("готовый результат", report)
        self.assertIn("Клеймо, указанное владелицей: SARAH COVENTRY", report)

    async def test_same_card_same_photos_still_cached(self):
        await self.research(code="OMG-000006")
        describes = len(self.calls("describe"))
        _, replies = await self.research(code="OMG-000006")
        self.assertEqual(len(self.calls("describe")), describes)
        self.assertIn("готовый результат", "\n".join(texts(replies)))

    def test_free_mode_uses_owner_mark_first(self):
        result = attribution._run_free(bot.DB_PATH, {"mark_text": "SARAH COVENTRY", "notes": "necklace red stones"})
        self.assertEqual(result["owner_mark"], "SARAH COVENTRY")
        self.assertTrue(result["evidence"])
        self.assertTrue(all(e["document_code"] != "DOC-000011" for e in result["evidence"]))
        report = attribution.format_report(result)
        self.assertIn("Клеймо, указанное владелицей: SARAH COVENTRY — поиск шёл в первую очередь по нему", report)

    def test_same_mark_helper(self):
        self.assertTrue(attribution.same_mark("SARAH COVENTRY", "Sarah Coventry"))
        self.assertTrue(attribution.same_mark("COVENTRY", "SARAH COVENTRY"))  # частичное чтение
        self.assertFalse(attribution.same_mark("Sakai Tamaru", "SARAH COVENTRY"))
        self.assertFalse(attribution.same_mark("R", "red stones"))  # короткое чтение не отсеивает слова
        self.assertIsNone(attribution.owner_mark({"mark_text": "не читается"}))
        self.assertIsNone(attribution.owner_mark({"mark_text": "  "}))
        self.assertEqual(attribution.owner_mark({"mark_text": " SARAH  COVENTRY "}), "SARAH COVENTRY")


if __name__ == "__main__":
    unittest.main()
