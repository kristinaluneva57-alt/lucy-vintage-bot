"""Живой диалог: фото без анкеты, фразы вместо команд, контекст.

Ничего платного не запускается само: «найти похожее» ведёт к плану
исследования с кнопкой подтверждения. Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from helpers import buttons, load_bot, texts

bot = load_bot()
import attribution  # noqa: E402
import library_db  # noqa: E402

OWNER = 501


def items_of(user_id):
    conn = sqlite3.connect(bot.DB_PATH)
    try:
        return [r[0] for r in conn.execute(
            "SELECT item_code FROM items WHERE owner_user_id = ? ORDER BY id", (user_id,)
        )]
    finally:
        conn.close()


def item_photos(code):
    return [file_id for _, file_id in bot.get_item_photos(code)]


class IntentRulesTest(unittest.TestCase):
    def test_detect(self):
        cases = {
            "Люся, сохрани это в мою шкатулку": "save",
            "добавь в шкатулку": "save",
            "найди похожее в книгах": "find",
            "Люся, посмотри мою брошь": "find",
            "что ты уже нашла об этой броши?": "found",
            "Расскажи про гранат": "story",
            "статья о чешском стекле": "story",
            "что такое бакелит?": None,
            "Может, это чешское стекло?": None,
            "привет": None,
        }
        for text, expected in cases.items():
            self.assertEqual(bot.detect_intent(text)[0], expected, text)
        self.assertEqual(bot.detect_intent("Люся, расскажи про гранат")[1], "гранат")


class LiveDialogTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_live_"), "test.db")
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        attribution.init_tables(path)
        self.env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "", "LUCY_AI_ENABLED": "0"})
        self.env.start()
        from helpers import FakeChat
        self.chat = FakeChat(bot, OWNER)

    def tearDown(self):
        self.env.stop()

    async def test_loose_photo_offers_actions_not_scolding(self):
        replies = await self.chat.photo("P1")
        self.assertIn("Вижу украшение", texts(replies)[0])
        self.assertEqual(buttons(replies), ["loose:save", "loose:find", "loose:no"])
        self.assertEqual(items_of(OWNER), [])   # без согласия ничего не сохраняется

    async def test_save_button_creates_card_and_continues(self):
        await self.chat.photo("P1")
        replies = await self.chat.press("loose:save")
        code = items_of(OWNER)[-1]
        self.assertIn(code, texts(replies)[0])
        self.assertEqual(self.chat.context.user_data["mode"], "quick")
        # Следующее фото — оборот той же карточки.
        await self.chat.photo("B1")
        conn = sqlite3.connect(bot.DB_PATH)
        back = conn.execute("SELECT back_photo FROM items WHERE item_code = ?", (code,)).fetchone()[0]
        conn.close()
        self.assertEqual(back, "B1")

    async def test_album_photos_go_to_one_card(self):
        await self.chat.photo("A1", media_group_id="g1")
        replies = await self.chat.photo("A2", media_group_id="g1")
        self.assertEqual(replies, [])           # без второго вопроса
        await self.chat.photo("A3", media_group_id="g1")
        await self.chat.press("loose:save")
        code = items_of(OWNER)[-1]
        self.assertEqual(item_photos(code), ["A2", "A3"])

    async def test_find_button_shows_plan_not_paid_run(self):
        await self.chat.photo("P1")
        with mock.patch.object(bot, "research_preview") as preview:
            async def fake(message, context, item_code, user_id):
                await message.reply_text(f"PLAN {item_code}")
            preview.side_effect = fake
            replies = await self.chat.press("loose:find")
        code = items_of(OWNER)[-1]
        self.assertTrue(any(f"PLAN {code}" in t for t in texts(replies)))

    async def test_phrase_after_photo(self):
        await self.chat.photo("P1")
        replies = await self.chat.say("Люся, сохрани в шкатулку")
        self.assertEqual(len(items_of(OWNER)), 1)
        self.assertIn("уже в «Моей шкатулке»", texts(replies)[0])

    async def test_caption_works_like_phrase(self):
        replies = await self.chat.photo("P1", caption="сохрани в шкатулку")
        self.assertEqual(len(items_of(OWNER)), 1)
        self.assertNotIn("loose:save", buttons(replies))

    async def test_find_phrase_uses_last_opened_item(self):
        await self.chat.photo("P1")
        await self.chat.press("loose:save")
        code = items_of(OWNER)[-1]
        bot.clear_quick_state(self.chat.context)
        await self.chat.press(f"item:{code}")
        with mock.patch.object(bot, "research_preview") as preview:
            async def fake(message, context, item_code, user_id):
                await message.reply_text(f"PLAN {item_code}")
            preview.side_effect = fake
            replies = await self.chat.say("найди похожее в книгах")
        self.assertTrue(any(f"PLAN {code}" in t for t in texts(replies)))

    async def test_find_phrase_without_photo_asks_for_photo(self):
        replies = await self.chat.say("найди похожее")
        self.assertIn("Пришли фото", texts(replies)[0])

    async def test_no_button(self):
        await self.chat.photo("P1")
        await self.chat.press("loose:no")
        replies = await self.chat.say("сохрани в шкатулку")
        self.assertEqual(items_of(OWNER), [])
        self.assertIn("Пришли фото", texts(replies)[0])

    async def test_anketa_photo_still_goes_to_anketa(self):
        await self.chat.press("box:add")
        await self.chat.photo("F1")
        self.assertEqual(len(items_of(OWNER)), 1)
        self.assertNotIn("loose_photos", self.chat.context.user_data)


class PersistenceTest(unittest.TestCase):
    def test_state_file_next_to_database(self):
        try:
            from telegram.ext import PicklePersistence  # noqa: F401
        except ImportError:
            self.skipTest("python-telegram-bot не установлен")
        bot.DB_PATH = "/data/lucy_vintage.db"
        persistence = bot.make_persistence()
        self.assertTrue(str(persistence.filepath).endswith("/data/lucy_dialog_state.pickle"))


if __name__ == "__main__":
    unittest.main()
