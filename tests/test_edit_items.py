"""Редактирование украшений в «Моей шкатулке».

Запуск: python -m unittest discover -s tests
"""

import sqlite3
import time
import unittest

from helpers import (
    FakeChat,
    buttons,
    create_old_schema_item,
    load_bot,
    photos,
    texts,
)

bot = load_bot()

OWNER = 1
STRANGER = 2
ADMIN = 999


def item_row(code):
    conn = sqlite3.connect(bot.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM items WHERE item_code = ?", (code,)).fetchone())
    finally:
        conn.close()


def count_items():
    conn = sqlite3.connect(bot.DB_PATH)
    try:
        return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    finally:
        conn.close()


class EditItemsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Старая база без новых колонок: проверяем, что миграция её не ломает.
        import os
        import tempfile

        bot.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        create_old_schema_item(bot.DB_PATH)
        bot.init_db()
        self.code = "OMG-000001"
        self.owner = FakeChat(bot, OWNER)
        self.stranger = FakeChat(bot, STRANGER)
        self.admin = FakeChat(bot, ADMIN)

    # --- база ---

    def test_migration_keeps_old_card(self):
        row = item_row(self.code)
        self.assertEqual(row["measurements"], "40 мм")
        self.assertEqual(row["front_photo"], "FRONT-OLD")
        self.assertEqual(row["mark_photo"], "MARK-OLD")
        self.assertIsNone(row["notes"])
        bot.init_db()  # повторная инициализация ничего не ломает
        self.assertEqual(item_row(self.code)["seller_story"], "бабушкина")

    # --- кнопка в карточке ---

    async def test_card_has_edit_button(self):
        replies = await self.owner.press(f"item:{self.code}")
        self.assertIn(f"edit:{self.code}", buttons(replies))

    async def test_edit_menu_lists_all_fields(self):
        replies = await self.owner.press(f"edit:{self.code}")
        data = buttons(replies)
        for field in bot.EDITABLE_FIELDS:
            self.assertIn(f"ef:{self.code}:{field}", data)
        self.assertIn(f"ep:{self.code}:extra", data)
        self.assertIn(f"ep:{self.code}:mark", data)
        self.assertIn(f"edone:{self.code}", data)

    # --- текстовые поля ---

    async def test_edit_measurements_keeps_other_fields(self):
        before = item_row(self.code)
        replies = await self.owner.press(f"ef:{self.code}:measurements")
        self.assertIn("40 мм", texts(replies)[0])  # показывает текущее значение

        replies = await self.owner.say("55 × 40 мм")
        self.assertIn("Сохранила", texts(replies)[0])

        after = item_row(self.code)
        self.assertEqual(after["measurements"], "55 × 40 мм")
        for field in ("weight", "provenance", "seller_story", "front_photo", "mark_photo"):
            self.assertEqual(after[field], before[field], field)
        self.assertIsNotNone(after["updated_at"])
        self.assertEqual(count_items(), 1)  # новая карточка не создаётся
        self.assertIsNone(self.owner.context.user_data.get("mode"))

    async def test_edit_mark_text(self):
        await self.owner.press(f"ef:{self.code}:mark_text")
        await self.owner.say("TRIFARI © PAT. PEND.")
        self.assertEqual(item_row(self.code)["mark_text"], "TRIFARI © PAT. PEND.")

    async def test_every_text_field_can_be_edited(self):
        for field in bot.EDITABLE_FIELDS:
            await self.owner.press(f"ef:{self.code}:{field}")
            await self.owner.say(f"новое значение {field}")
            self.assertEqual(item_row(self.code)[field], f"новое значение {field}")

    async def test_description_replace_and_append(self):
        await self.owner.press(f"ef:{self.code}:notes")
        await self.owner.say("Брошь-цветок, позолота.")
        replies = await self.owner.press(f"ef:{self.code}:notes")
        self.assertIn(f"ef:{self.code}:notes_add", buttons(replies))

        await self.owner.press(f"ef:{self.code}:notes_add")
        replies = await self.owner.say("Нашла похожую в каталоге 1955 года.")
        self.assertIn("Дописала", texts(replies)[0])
        self.assertEqual(
            item_row(self.code)["notes"],
            "Брошь-цветок, позолота.\n\nНашла похожую в каталоге 1955 года.",
        )

    async def test_card_shows_description(self):
        await self.owner.press(f"ef:{self.code}:notes")
        await self.owner.say("Брошь-цветок")
        replies = await self.owner.press(f"edone:{self.code}")
        self.assertIn("Брошь-цветок", texts(replies)[-1])

    async def test_cancel_changes_nothing(self):
        await self.owner.press(f"ef:{self.code}:weight")
        replies = await self.owner.say("отмена")
        self.assertIn("ничего не меняю", texts(replies)[0])
        self.assertEqual(item_row(self.code)["weight"], "12 г")
        self.assertIsNone(self.owner.context.user_data.get("mode"))

    async def test_too_long_text_is_rejected(self):
        await self.owner.press(f"ef:{self.code}:weight")
        replies = await self.owner.say("x" * 500)
        self.assertIn("Слишком длинно", texts(replies)[0])
        self.assertEqual(item_row(self.code)["weight"], "12 г")
        await self.owner.say("13 г")  # можно сразу прислать короче
        self.assertEqual(item_row(self.code)["weight"], "13 г")

    async def test_menu_button_leaves_edit_mode(self):
        await self.owner.press(f"ef:{self.code}:weight")
        await self.owner.say(bot.BTN_LEARN)
        self.assertEqual(item_row(self.code)["weight"], "12 г")
        self.assertIsNone(self.owner.context.user_data.get("mode"))

    async def test_stale_edit_mode_expires(self):
        await self.owner.press(f"ef:{self.code}:weight")
        self.owner.context.user_data["edit_started"] = time.time() - 3 * 3600
        await self.owner.say("Что такое бакелит?")
        self.assertEqual(item_row(self.code)["weight"], "12 г")

    async def test_photo_while_waiting_for_text(self):
        await self.owner.press(f"ef:{self.code}:weight")
        replies = await self.owner.photo("PHOTO-X")
        self.assertIn("жду текст", texts(replies)[0])
        self.assertEqual(bot.get_item_photos(self.code), [])

    # --- фото ---

    async def test_add_photos_keeps_old_ones(self):
        await self.owner.press(f"ep:{self.code}:extra")
        await self.owner.photo("NEW-1")
        await self.owner.photo("NEW-2")
        row = item_row(self.code)
        self.assertEqual(row["front_photo"], "FRONT-OLD")
        self.assertEqual(row["mark_photo"], "MARK-OLD")
        self.assertEqual(
            bot.get_item_photos(self.code), [("extra", "NEW-1"), ("extra", "NEW-2")]
        )
        self.assertEqual(count_items(), 1)

    async def test_album_gets_one_reply(self):
        await self.owner.press(f"ep:{self.code}:extra")
        first = await self.owner.photo("A-1", media_group_id="album")
        second = await self.owner.photo("A-2", media_group_id="album")
        self.assertEqual(len(texts(first)), 1)
        self.assertEqual(texts(second), [])
        self.assertEqual(len(bot.get_item_photos(self.code)), 2)

    async def test_new_mark_photo_does_not_replace_old(self):
        await self.owner.press(f"ep:{self.code}:mark")
        await self.owner.photo("MARK-NEW")
        self.assertEqual(item_row(self.code)["mark_photo"], "MARK-OLD")
        replies = await self.owner.press(f"details:{self.code}")
        self.assertEqual(photos(replies), ["MARK-OLD", "MARK-NEW"])

    async def test_text_while_waiting_for_photos(self):
        await self.owner.press(f"ep:{self.code}:extra")
        replies = await self.owner.say("вот")
        self.assertIn("жду фото", texts(replies)[0])

    async def test_card_counts_extra_photos(self):
        await self.owner.press(f"ep:{self.code}:extra")
        await self.owner.photo("NEW-1")
        replies = await self.owner.press(f"edone:{self.code}")
        self.assertIn("Дополнительных фото: 1", texts(replies)[-1])

    # --- права ---

    async def test_stranger_cannot_open_edit_menu(self):
        replies = await self.stranger.press(f"edit:{self.code}")
        self.assertIn("нельзя", texts(replies)[0])
        self.assertEqual(buttons(replies), [])

    async def test_stranger_cannot_edit_with_forged_button(self):
        replies = await self.stranger.press(f"ef:{self.code}:weight")
        self.assertIn("нельзя", texts(replies)[0])
        await self.stranger.say("1000 г")
        self.assertEqual(item_row(self.code)["weight"], "12 г")

    async def test_stranger_cannot_add_photo(self):
        await self.stranger.press(f"ep:{self.code}:extra")
        await self.stranger.photo("HACK")
        self.assertEqual(bot.get_item_photos(self.code), [])

    async def test_admin_cannot_edit_someone_elses_card(self):
        replies = await self.admin.press(f"ef:{self.code}:weight")
        self.assertIn("нельзя", texts(replies)[0])

    def test_database_functions_check_owner(self):
        self.assertFalse(bot.update_item_field(self.code, STRANGER, "weight", "1 г"))
        self.assertFalse(bot.add_item_photo(self.code, STRANGER, "extra", "X"))
        with self.assertRaises(ValueError):
            bot.update_item_field(self.code, OWNER, "owner_user_id", "2")
        with self.assertRaises(ValueError):
            bot.update_item_field(self.code, OWNER, "attribution_status", "Trifari")
        self.assertEqual(item_row(self.code)["owner_user_id"], OWNER)

    async def test_deleted_card_cannot_be_edited(self):
        await self.owner.press(f"ef:{self.code}:weight")
        self.assertTrue(bot.delete_item(self.code, OWNER))
        replies = await self.owner.say("13 г")
        self.assertIn("нельзя", texts(replies)[0])
        self.assertEqual(item_row(self.code)["weight"], "12 г")

    # --- анкета не сломана ---

    async def test_new_anketa_still_creates_card(self):
        chat = FakeChat(bot, 7)
        await chat.press("box:add")
        await chat.photo("F")
        await chat.photo("B")
        await chat.photo("M")
        await chat.say("CORO")
        await chat.photo("D")
        await chat.say("30 мм")
        await chat.say("нет")
        await chat.say("Флохмаркт")
        replies = await chat.say("нет")
        self.assertIn("КАРТОЧКА НАХОДКИ", texts(replies)[0])
        self.assertEqual(count_items(), 2)
        new_code = bot.get_items(7)[0][0]
        await chat.press(f"ef:{new_code}:notes")
        await chat.say("Моя первая брошь")
        self.assertEqual(item_row(new_code)["notes"], "Моя первая брошь")
        self.assertEqual(item_row(new_code)["mark_text"], "CORO")


if __name__ == "__main__":
    unittest.main()
