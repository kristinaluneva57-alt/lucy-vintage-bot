"""Быстрое добавление украшения в «Мою шкатулку».

Сценарии: одно фото, два фото, три фото; без клейма, «не знаю», с клеймом
(фото с бесплатной подсказкой Tesseract и текстом); «Исследовать сейчас»
с подтверждением; возвращение к незавершённой карточке; старые карточки.

Запуск: python -m unittest discover -s tests
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import buttons, load_bot, texts

bot = load_bot()
import attribution  # noqa: E402
import library_db  # noqa: E402
import page_images  # noqa: E402
import visual_index  # noqa: E402
import test_visual as tv  # noqa: E402  (вспомогательные функции и FakeOpenAI)

OWNER, OTHER, ADMIN = 1, 2, 999
HAS_TESSERACT = shutil.which("tesseract") is not None


def item(code):
    conn = sqlite3.connect(bot.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM items WHERE item_code = ?", (code,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def items_of(user_id):
    conn = sqlite3.connect(bot.DB_PATH)
    try:
        return [r[0] for r in conn.execute(
            "SELECT item_code FROM items WHERE owner_user_id = ? ORDER BY id", (user_id,)
        )]
    finally:
        conn.close()


class QuickAddTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        attribution.init_tables(path)
        conn = sqlite3.connect(path)
        tv.add_book(conn, 7, "Sarah Coventry Jewelry", tv.SC_PAGES, total_pages=182)
        # Старая карточка из прежней длинной анкеты.
        conn.execute(
            "INSERT INTO items (item_code, measurements, weight, provenance, seller_story, "
            "front_photo, back_photo, mark_photo, details_photo, owner_user_id, mark_text) VALUES "
            "('OMG-000500', '40 мм', '12 г', 'Vinted', 'бабушкина', 'F', 'B', 'M', 'D', ?, 'CORO')",
            (OWNER,),
        )
        conn.commit()
        conn.close()
        visual_index.index_document(path, 7)

        self.r2 = {}
        helper = type("H", (), {"renders": [], "readable_ad": True})()
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
        self.chat = tv.VisualAttributionTest.chat(self, OWNER)
        self.other = tv.VisualAttributionTest.chat(self, OTHER)

    async def start(self):
        return await self.chat.press("box:add")

    def last_code(self):
        return items_of(OWNER)[-1]

    # --- одно фото ---

    async def test_one_photo_card_saved_immediately(self):
        intro = texts(await self.start())[0]
        self.assertIn("НОВОЕ УКРАШЕНИЕ", intro)
        self.assertIn("всё необязательно", intro)
        replies = await self.chat.photo("F1")
        code = self.last_code()
        self.assertIn(f"карточка {code} уже в «Моей шкатулке»", texts(replies)[0])
        self.assertEqual(item(code)["front_photo"], "F1")
        self.assertEqual(item(code)["mark_status"], "not_asked")
        self.assertIn(f"qskip:{code}:back", buttons(replies))
        self.assertIn(f"vpre:{code}", buttons(replies))  # исследовать можно уже сейчас
        self.assertEqual(tv.FakeOpenAI.calls, [])

    async def test_one_photo_then_research_now_with_confirmation(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        preview = await self.chat.press(f"vpre:{code}")
        text = texts(preview)[0]
        self.assertIn(f"ПЕРЕД ИССЛЕДОВАНИЕМ — {code}", text)
        self.assertIn("✅ фото лицевой стороны", text)
        self.assertIn("➖ фото оборота", text)
        self.assertIn("➖ клеймо: не указано", text)
        self.assertIn("Желательно, но необязательно: фото оборота, клеймо", text)
        self.assertIn("осталось 3", text)
        self.assertIn("спишется только после нажатия", text)
        self.assertEqual(buttons(preview), [f"vattr:{code}", f"qmore:{code}"])
        # Предпросмотр ничего не тратит.
        self.assertEqual(tv.FakeOpenAI.calls, [])
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 0)
        # Платный вызов — только после «Начать исследование».
        await self.chat.press(f"vattr:{code}")
        await self.chat.run_tasks()
        self.assertEqual(tv.FakeOpenAI.calls[0]["kind"], "describe")
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, OWNER), 1)

    # --- два фото, без клейма ---

    async def test_two_photos_no_mark(self):
        await self.start()
        await self.chat.photo("F1")
        replies = await self.chat.photo("B1")
        code = self.last_code()
        self.assertIn("Оборот сохранила", texts(replies)[0])
        self.assertIn("Есть ли на украшении клеймо", texts(replies)[1])
        self.assertEqual(
            buttons(replies),
            [f"qmark:{code}:yes", f"qmark:{code}:no", f"qmark:{code}:unknown", f"vpre:{code}"],
        )
        final = await self.chat.press(f"qmark:{code}:no")
        text = texts(final)[0]
        self.assertIn("Я уже могу попробовать исследовать твоё украшение! 🔬", text)
        self.assertIn("Все дополнительные сведения необязательны", text)
        self.assertEqual(buttons(final), [f"vpre:{code}", f"qmore:{code}"])
        row = item(code)
        self.assertEqual((row["back_photo"], row["mark_status"]), ("B1", "no"))
        card = texts(await self.chat.press(f"item:{code}"))[-1]
        self.assertIn("Надпись на клейме: клейма нет", card)
        self.assertIn("Можно дополнить (необязательно): описание, размеры", card)
        self.assertIsNone(self.chat.context.user_data.get("mode"))

    async def test_skip_back_and_dont_know_mark(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        replies = await self.chat.press(f"qskip:{code}:back")
        self.assertIn("клеймо", texts(replies)[0])
        final = await self.chat.press(f"qmark:{code}:unknown")
        self.assertIn("с лупой", texts(final)[0])
        self.assertEqual(item(code)["mark_status"], "unknown")
        card = texts(await self.chat.press(f"item:{code}"))[-1]
        self.assertIn("Надпись на клейме: не проверено", card)
        self.assertIn("фото оборота, клеймо", card)

    # --- три фото, клеймо ---

    @unittest.skipUnless(HAS_TESSERACT, "нужен tesseract")
    async def test_three_photos_mark_suggestion_confirmed(self):
        await self.start()
        await self.chat.photo("F1")
        await self.chat.photo("B1")
        code = self.last_code()
        await self.chat.press(f"qmark:{code}:yes")
        replies = await self.chat.photo("M1")  # фото клейма с надписью SARAH COVENTRY
        text = texts(replies)[0]
        self.assertIn("бесплатно, без ИИ", text)
        self.assertIn("«SARAH COVENTRY»", text)
        self.assertEqual(tv.FakeOpenAI.calls, [])  # без платного вызова
        final = await self.chat.press(f"qmok:{code}")
        self.assertIn("Записала надпись клейма: SARAH COVENTRY", texts(final)[0])
        row = item(code)
        self.assertEqual((row["front_photo"], row["back_photo"], row["mark_photo"]), ("F1", "B1", "M1"))
        self.assertEqual((row["mark_text"], row["mark_status"]), ("SARAH COVENTRY", "yes"))

    async def test_mark_typed_as_text(self):
        await self.start()
        await self.chat.photo("F1")
        await self.chat.photo("B1")
        code = self.last_code()
        await self.chat.say("есть")  # можно ответить и словами
        final = await self.chat.say("TRIFARI ©")
        self.assertIn("Записала надпись клейма: TRIFARI ©", texts(final)[0])
        self.assertEqual(item(code)["mark_text"], "TRIFARI ©")
        self.assertIsNone(item(code)["mark_photo"])

    async def test_mark_photo_without_readable_text(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"qskip:{code}:back")
        await self.chat.press(f"qmark:{code}:yes")
        replies = await self.chat.photo("F9")  # на фото нет текста — подсказки нет
        self.assertIn("напиши её текстом", texts(replies)[0])
        await self.chat.photo("M2")  # второе фото клейма не теряется
        final = await self.chat.press(f"qmskip:{code}")
        self.assertIn("Я уже могу попробовать", texts(final)[0])
        self.assertEqual(item(code)["mark_photo"], "F9")
        self.assertEqual(bot.get_item_photos(code), [("mark", "M2")])

    async def test_album_keeps_all_photos(self):
        await self.start()
        await self.chat.photo("F1", media_group_id="g1")
        await self.chat.photo("F2", media_group_id="g1")
        await self.chat.photo("F3", media_group_id="g1")
        code = self.last_code()
        self.assertEqual(item(code)["front_photo"], "F1")
        self.assertEqual(bot.get_item_photos(code), [("extra", "F2"), ("extra", "F3")])

    # --- прерывание и возвращение ---

    async def test_leaving_midway_keeps_card_and_buttons_resume(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.say(bot.BTN_LEARN)  # ушла в другой раздел
        self.assertIsNone(self.chat.context.user_data.get("mode"))
        self.assertEqual(item(code)["front_photo"], "F1")
        self.assertIn(code, [c for c, *_ in bot.get_items(OWNER)])
        # Вернулась по кнопке из старого сообщения — продолжает ту же карточку.
        await self.chat.press(f"qskip:{code}:back")
        await self.chat.press(f"qmark:{code}:no")
        self.assertEqual(item(code)["mark_status"], "no")
        self.assertEqual(len(items_of(OWNER)), 2)  # новая карточка не создана

    async def test_return_later_via_card_and_add_details(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        self.chat.context.user_data["quick_started"] -= 3 * 3600  # анкета «устарела»
        await self.chat.say("привет")  # обычное сообщение, не попадает в карточку
        self.assertIsNone(item(code)["back_photo"])
        card = await self.chat.press(f"item:{code}")
        self.assertIn(f"edit:{code}", buttons(card))
        # Анкета не закончена — «Добавить подробности» продолжает её с того же шага,
        # а «Другие поля карточки» открывает обычное редактирование.
        resumed = await self.chat.press(f"qmore:{code}")
        self.assertIn("Продолжаем заполнять", texts(resumed)[0])
        self.assertIn(f"edit:{code}", buttons(resumed))
        menu = await self.chat.press(f"edit:{code}")
        self.assertIn(f"ep:{code}:extra", buttons(menu))
        await self.chat.press(f"ef:{code}:measurements")
        await self.chat.say("45 см")
        self.assertEqual(item(code)["measurements"], "45 см")

    async def test_new_add_while_unfinished_starts_new_card(self):
        await self.start()
        await self.chat.photo("F1")
        first = self.last_code()
        await self.start()
        await self.chat.photo("F2")
        second = self.last_code()
        self.assertNotEqual(first, second)
        self.assertEqual(item(first)["front_photo"], "F1")

    # --- права и совместимость ---

    async def test_other_user_cannot_continue_card(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        replies = await self.other.press(f"qmark:{code}:no")
        self.assertIn("не найдена", texts(replies)[0])
        self.assertEqual(item(code)["mark_status"], "not_asked")
        preview = await self.other.press(f"vpre:{code}")
        self.assertIn("только свою карточку", texts(preview)[0])

    async def test_old_card_still_works(self):
        card = texts(await self.chat.press("item:OMG-000500"))[-1]
        self.assertIn("Размер: 40 мм", card)
        self.assertIn("Надпись на клейме: CORO", card)
        self.assertIn("Можно дополнить (необязательно): описание", card)
        replies = await self.chat.press("item:OMG-000500")
        self.assertEqual([d.split(":")[0] for d in buttons(replies)], ["details", "vpre", "edit", "del"])
        preview = texts(await self.chat.press("vpre:OMG-000500"))[0]
        self.assertIn("✅ клеймо: «CORO»", preview)

    def test_no_promises_about_ai_training(self):
        for text in (bot.QUICK_INTRO, bot.QUICK_FINAL, bot.BOX_INTRO_TEXT):
            lower = text.lower()
            for word in ("обуча", "учится на", "тренир"):
                self.assertNotIn(word, lower)


if __name__ == "__main__":
    unittest.main()
