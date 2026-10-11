"""Регрессионные тесты «Моей шкатулки» (PR #18):

1. Удаление старых карточек без владелицы (OMG-000001…000003 на Railway):
   видны в шкатулке, но удалить их было нельзя.
2. Клеймо: отсутствие сведений не означает «клейма нет».
3. Незавершённая анкета: «Исследовать сейчас» → «Добавить подробности»
   продолжает ту же карточку; старые кнопки не ломают состояние.

Запуск: python -m unittest discover -s tests
"""

import os
import sqlite3
import tempfile
import time
import unittest

from helpers import FakeChat, buttons, load_bot, texts

bot = load_bot()
import attribution  # noqa: E402
import test_quick_add as tq  # noqa: E402
import test_visual as tv  # noqa: E402

OWNER, STRANGER, ADMIN = 1, 2, 999


def row(code):
    conn = sqlite3.connect(bot.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute("SELECT * FROM items WHERE item_code = ?", (code,)).fetchone()
        return dict(found) if found else None
    finally:
        conn.close()


def all_codes():
    conn = sqlite3.connect(bot.DB_PATH)
    try:
        return [r[0] for r in conn.execute("SELECT item_code FROM items ORDER BY id")]
    finally:
        conn.close()


def railway_like_db(path):
    """База как на Railway: первые карточки созданы до того, как у карточек
    появилась владелица (owner_user_id = NULL), и до новых колонок."""
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_code TEXT UNIQUE,
            measurements TEXT,
            weight TEXT,
            provenance TEXT,
            seller_story TEXT,
            attribution_status TEXT DEFAULT 'не определено'
        )
        """
    )
    for n in (1, 2, 3):
        conn.execute(
            "INSERT INTO items (item_code, measurements, weight, provenance, seller_story) "
            "VALUES (?, '40 мм', '12 г', 'Vinted', 'бабушкина')",
            (f"OMG-{n:06d}",),
        )
    conn.commit()
    conn.close()


class DeleteOldCardsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        railway_like_db(bot.DB_PATH)
        bot.init_db()  # миграция добавляет колонки, данные не трогает
        bot.init_db()
        # Карточка обычной участницы, созданная уже с владелицей.
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute(
            "INSERT INTO items (item_code, front_photo, owner_user_id) VALUES ('OMG-000004', 'F4', ?)",
            (OWNER,),
        )
        conn.commit()
        conn.close()
        self.admin = FakeChat(bot, ADMIN)
        self.owner = FakeChat(bot, OWNER)
        self.stranger = FakeChat(bot, STRANGER)

    def test_migration_keeps_ids_and_has_no_owner(self):
        self.assertEqual(all_codes(), ["OMG-000001", "OMG-000002", "OMG-000003", "OMG-000004"])
        for n in (1, 2, 3):
            data = row(f"OMG-00000{n}")
            self.assertIsNone(data["owner_user_id"])
            self.assertIsNone(data["deleted_at"])
            self.assertEqual(data["measurements"], "40 мм")

    async def test_admin_sees_and_deletes_old_cards(self):
        # Ровно сценарий с Railway: карточки видны…
        listing = await self.admin.press("box:list")
        for n in (1, 2, 3):
            self.assertIn(f"item:OMG-00000{n}", buttons(listing))
        for n in (1, 2, 3):
            code = f"OMG-00000{n}"
            confirm = await self.admin.press(f"del:{code}")
            self.assertIn(f"Удалить карточку {code}", texts(confirm)[0])
            self.assertEqual(buttons(confirm), [f"delok:{code}", f"delno:{code}"])
            # …и теперь удаляются, а не «уже удалена или не твоя».
            done = await self.admin.press(f"delok:{code}")
            self.assertIn(f"Карточка {code} удалена", texts(done)[0])
            data = row(code)
            self.assertIsNotNone(data["deleted_at"])  # мягкое удаление
            self.assertEqual(data["measurements"], "40 мм")  # запись на месте
            self.assertEqual(data["item_code"], code)  # ID не меняется
        listing = await self.admin.press("box:list")
        self.assertEqual(len(all_codes()), 4)  # ничего физически не удалено
        for n in (1, 2, 3):
            self.assertNotIn(f"item:OMG-00000{n}", buttons(listing))

    async def test_second_delete_says_already_deleted(self):
        await self.admin.press("delok:OMG-000001")
        replies = await self.admin.press("delok:OMG-000001")
        self.assertIn("уже удалена", texts(replies)[0])

    async def test_cancel_keeps_card(self):
        await self.admin.press("del:OMG-000002")
        replies = await self.admin.press("delno:OMG-000002")
        self.assertIn("остаётся", texts(replies)[0])
        self.assertIsNone(row("OMG-000002")["deleted_at"])

    async def test_regular_user_cannot_see_or_delete_old_cards(self):
        listing = await self.owner.press("box:list")
        self.assertNotIn("item:OMG-000001", buttons(listing))
        confirm = await self.owner.press("del:OMG-000001")
        self.assertIn("Не смогла найти", texts(confirm)[0])
        forged = await self.owner.press("delok:OMG-000001")  # подделанная кнопка
        self.assertIn("Не нашла эту карточку", texts(forged)[0])
        self.assertIsNone(row("OMG-000001")["deleted_at"])

    async def test_owner_deletes_own_card_others_cannot(self):
        for chat in (self.stranger, self.admin):  # и администратор не удаляет чужое
            replies = await chat.press("delok:OMG-000004")
            self.assertIn("Не нашла", texts(replies)[0])
            self.assertIsNone(row("OMG-000004")["deleted_at"])
        done = await self.owner.press("delok:OMG-000004")
        self.assertIn("удалена", texts(done)[0])
        self.assertEqual(row("OMG-000004")["front_photo"], "F4")

    def test_view_and_delete_use_same_rule(self):
        """Главная регрессия: всё, что видно в шкатулке, можно удалить, и наоборот."""
        for user in (OWNER, STRANGER, ADMIN):
            for code in all_codes():
                visible = bot.get_item(code, user) is not None
                self.assertEqual(bot.owns_item(code, user), visible, (user, code))
                self.assertEqual(bot.item_state(code, user) == "active", visible, (user, code))

    async def test_admin_can_edit_and_preview_old_card(self):
        # Кнопки карточки работают для тех же карточек, что видны.
        await self.admin.press("ef:OMG-000003:notes")
        await self.admin.say("Старая находка")
        self.assertEqual(row("OMG-000003")["notes"], "Старая находка")
        self.assertIsNone(row("OMG-000003")["owner_user_id"])  # владелица не переписана
        card = await self.admin.press("item:OMG-000003")
        self.assertEqual([d.split(":")[0] for d in buttons(card)], ["details", "vpre", "edit", "del"])

    async def test_itemcheck_diagnostics(self):
        replies = await self.admin.say("/itemcheck OMG-000001 OMG-000004 OMG-000099")
        text = texts(replies)[0]
        self.assertIn("без владелицы: 3", text)
        self.assertIn("OMG-000001: владелица — не указана (старая карточка); удалена — нет; "
                      "видна тебе и можно удалить — да", text)
        self.assertIn("OMG-000004: владелица — другая участница", text)
        self.assertIn("OMG-000099: в базе нет", text)
        self.assertNotIn(str(OWNER) + ";", text)
        self.assertNotIn(str(ADMIN), text)  # без ID пользователей
        denied = await self.owner.say("/itemcheck")
        self.assertIn("только администратору", texts(denied)[0])
        self.assertIsNone(row("OMG-000001")["deleted_at"])  # диагностика ничего не меняет


class MarkLabelTest(unittest.TestCase):
    def test_states(self):
        label = bot.mark_text_label
        self.assertEqual(label("CORO", None, None), "CORO")
        self.assertEqual(label("CORO", "M", "no"), "CORO")
        self.assertEqual(label(None, None, "no"), "клейма нет")
        self.assertEqual(label(None, None, "unknown"), "не проверено")
        self.assertEqual(label(None, None, "not_asked"), "не указано")
        self.assertEqual(label(None, None, None), "не указано")  # старые карточки
        self.assertEqual(label(None, None, "yes"), "клеймо есть, надпись не записана")
        self.assertEqual(label(None, "M", None), "клеймо есть, надпись не записана")
        self.assertEqual(label("", None, None), "не указано")

    def test_missing_details_suggests_mark_unless_no(self):
        for status in (None, "not_asked", "unknown", "yes"):
            self.assertIn("клеймо", bot.missing_details("B", None, None, status, "n", "m"), status)
        self.assertNotIn("клеймо", bot.missing_details("B", None, None, "no", "n", "m"))


class MarkOnCardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.DB_PATH = os.path.join(tempfile.mkdtemp(prefix="lucy_test_"), "test.db")
        railway_like_db(bot.DB_PATH)
        bot.init_db()
        conn = sqlite3.connect(bot.DB_PATH)
        conn.execute("UPDATE items SET owner_user_id = ? WHERE item_code = 'OMG-000002'", (OWNER,))
        conn.commit()
        conn.close()
        self.owner = FakeChat(bot, OWNER)

    async def test_old_card_without_info_is_not_no_mark(self):
        card = texts(await self.owner.press("item:OMG-000002"))[-1]
        self.assertIn("Надпись на клейме: не указано", card)
        self.assertNotIn("клейма нет", card)
        self.assertIn("клеймо", card.split("Можно дополнить")[1])
        self.assertIsNone(row("OMG-000002")["mark_status"])  # статус не выдуман

    async def test_mark_photo_added_later_counts(self):
        await self.owner.press("ep:OMG-000002:mark")
        await self.owner.photo("MARK-NEW")
        card = texts(await self.owner.press("item:OMG-000002"))[-1]
        self.assertIn("Надпись на клейме: клеймо есть, надпись не записана", card)


class ResumeQuickTest(unittest.IsolatedAsyncioTestCase):
    setUp = tq.QuickAddTest.setUp
    start = tq.QuickAddTest.start
    last_code = tq.QuickAddTest.last_code

    async def test_research_now_then_add_details_then_continue(self):
        """Первое фото → «Исследовать сейчас» → передумала → «Добавить подробности» →
        продолжает заполнять ту же карточку."""
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"vpre:{code}")
        resumed = await self.chat.press(f"qmore:{code}")
        self.assertIn(f"Продолжаем заполнять {code}", texts(resumed)[0])
        self.assertIn("Есть фото оборотной стороны", texts(resumed)[0])
        self.assertEqual(buttons(resumed), [f"qskip:{code}:back", f"vpre:{code}", f"edit:{code}"])
        replies = await self.chat.photo("B1")
        self.assertIn("Оборот сохранила", texts(replies)[0])
        await self.chat.press(f"qmark:{code}:no")
        data = tq.item(code)
        self.assertEqual((data["front_photo"], data["back_photo"], data["mark_status"]), ("F1", "B1", "no"))
        self.assertEqual(len(tq.items_of(tq.OWNER)), 2)  # старая + эта, новой нет
        self.assertEqual(tv.FakeOpenAI.calls, [])
        self.assertEqual(attribution.paid_this_month(bot.DB_PATH, tq.OWNER), 0)

    async def test_photo_after_preview_goes_to_same_card(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"vpre:{code}")
        await self.chat.photo("B1")  # передумала и просто прислала оборот
        self.assertEqual(tq.item(code)["back_photo"], "B1")
        self.assertEqual(len(tq.items_of(tq.OWNER)), 2)

    async def test_resume_after_long_break_uses_saved_card(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"qskip:{code}:back")
        self.chat.context.user_data["quick_started"] -= 3 * 3600
        await self.chat.say("привет")  # анкета устарела и сброшена
        self.assertIsNone(self.chat.context.user_data.get("quick_code"))
        resumed = await self.chat.press(f"qmore:{code}")
        # Оборота нет и про клеймо не отвечали — бот не помнит пропуск,
        # поэтому снова предлагает оборот (с «Без оборота»).
        self.assertIn("Продолжаем заполнять", texts(resumed)[0])
        self.assertIn(f"qskip:{code}:back", buttons(resumed))
        await self.chat.press(f"qskip:{code}:back")
        await self.chat.press(f"qmark:{code}:yes")
        await self.chat.say("CORO")
        self.assertEqual(tq.item(code)["mark_text"], "CORO")

    async def test_resume_at_mark_input(self):
        await self.start()
        await self.chat.photo("F1")
        await self.chat.photo("B1")
        code = self.last_code()
        await self.chat.press(f"qmark:{code}:yes")
        await self.chat.press(f"vpre:{code}")
        resumed = await self.chat.press(f"qmore:{code}")
        self.assertIn("Пришли фото клейма", texts(resumed)[0])
        await self.chat.say("TRIFARI")
        self.assertEqual(tq.item(code)["mark_text"], "TRIFARI")

    async def test_finished_card_opens_edit_menu(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"qskip:{code}:back")
        await self.chat.press(f"qmark:{code}:no")
        menu = await self.chat.press(f"qmore:{code}")
        self.assertIn(f"ep:{code}:extra", buttons(menu))
        self.assertIsNone(self.chat.context.user_data.get("quick_code"))

    async def test_confirmed_research_closes_questionnaire(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"vpre:{code}")
        await self.chat.press(f"vattr:{code}")
        await self.chat.run_tasks()
        self.assertIsNone(self.chat.context.user_data.get("quick_code"))
        # Следующее фото не прилипает к исследуемой карточке.
        await self.chat.photo("RANDOM")
        self.assertIsNone(tq.item(code)["back_photo"])
        # Но дополнить можно в любой момент.
        resumed = await self.chat.press(f"qmore:{code}")
        self.assertIn("Продолжаем заполнять", texts(resumed)[0])

    async def test_old_skip_button_does_not_reask_mark(self):
        await self.start()
        await self.chat.photo("F1")
        code = self.last_code()
        await self.chat.press(f"qskip:{code}:back")
        await self.chat.press(f"qmark:{code}:no")
        replies = await self.chat.press(f"qskip:{code}:back")  # старое сообщение
        self.assertIn("Я уже могу попробовать", texts(replies)[0])
        self.assertEqual(tq.item(code)["mark_status"], "no")

    async def test_old_suggestion_button_of_other_card(self):
        """Подсказка клейма одной карточки не попадает в другую по старой кнопке."""
        await self.start()
        await self.chat.photo("F1")
        first = self.last_code()
        await self.start()
        await self.chat.photo("F2")
        second = self.last_code()
        self.chat.context.user_data["quick_suggestion"] = {"code": second, "text": "SARAH COVENTRY"}
        replies = await self.chat.press(f"qmok:{first}")
        self.assertIn("Напиши, пожалуйста, надпись", texts(replies)[0])
        self.assertIsNone(tq.item(first)["mark_text"])

    async def test_old_button_switches_to_its_card(self):
        await self.start()
        await self.chat.photo("F1")
        first = self.last_code()
        await self.start()
        await self.chat.photo("F2")
        second = self.last_code()
        await self.chat.press(f"qskip:{first}:back")  # кнопка из сообщения про первую
        await self.chat.press(f"qmark:{first}:yes")
        await self.chat.say("CORO")
        self.assertEqual(tq.item(first)["mark_text"], "CORO")
        self.assertIsNone(tq.item(second)["mark_text"])
        self.assertEqual(tq.item(second)["mark_status"], "not_asked")


if __name__ == "__main__":
    unittest.main()
