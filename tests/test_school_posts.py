"""Редакция школы: досье → статья → правка → публикация в тему группы с оглавлением.

ИИ и Telegram подменяются: настоящих запросов нет.
Запуск: python -m unittest discover -s tests
"""

import asyncio
import types
import unittest
from unittest import mock

import helpers
from helpers import load_bot

bot = load_bot()
import school_posts  # noqa: E402
import library_article  # noqa: E402

FRAGMENTS = [
    {"n": 1, "title": "Garnet Jewelry", "document_code": "DOC-000011", "pdf_page": 12, "printed_page": 10},
    {"n": 2, "title": "Victorian Jewels", "document_code": "DOC-000012", "pdf_page": 40},
]
JOB = {
    "id": 7, "status": "done", "topic": "гранат",
    "dossier": "КОРОТКО\nБогемский гранат добывали в Чехии [1].",
    "research": {"fragments": FRAGMENTS},
}
AI_ANSWER = """ЗАГОЛОВОК: Богемский гранат: как его узнать
ВСТУПЛЕНИЕ: Гранат — главный камень чешских украшений [1].
РАЗДЕЛ: Откуда он
Его добывали в Чехии [1], а в Англии носили в викторианскую эпоху [2].
РАЗДЕЛ: Как узнать
— Мелкие камни плотно друг к другу [1]
— Тёмно-красный цвет [9]
"""

GROUP = -1001234567890


class FakeTelegram:
    """Поддельный Telegram: запоминает отправленное, правленое и закреплённое."""

    def __init__(self):
        self.sent, self.edited, self.pinned, self.deleted = [], [], [], []
        self.next_id = 100

    async def send_message(self, chat_id, text, message_thread_id=None, **kwargs):
        self.next_id += 1
        self.sent.append(("text", chat_id, message_thread_id, text, self.next_id))
        return types.SimpleNamespace(message_id=self.next_id)

    async def send_photo(self, chat_id, photo, caption=None, message_thread_id=None, **kwargs):
        self.next_id += 1
        self.sent.append(("photo", chat_id, message_thread_id, caption, self.next_id))
        return types.SimpleNamespace(message_id=self.next_id)

    async def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.edited.append((message_id, text))

    async def edit_message_caption(self, chat_id, message_id, caption=None, **kwargs):
        self.edited.append((message_id, caption))

    async def pin_chat_message(self, chat_id, message_id, **kwargs):
        self.pinned.append(message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class Chat:
    """Личка Кристины с Люсей + сама группа."""

    def __init__(self):
        self.out = []
        self.tg = FakeTelegram()
        self.context = types.SimpleNamespace(user_data={}, bot=self.tg)

    def message(self, text=None, photo=None, chat_type="private", chat_id=999, thread_id=None, text_html=None):
        out = self.out

        async def reply_text(t, reply_markup=None, **kwargs):
            out.append(("text", t, reply_markup))

        async def reply_photo(p, caption=None, **kwargs):
            out.append(("photo", p, caption))

        return types.SimpleNamespace(
            text=text, text_html=text_html if text_html is not None else text,
            photo=[types.SimpleNamespace(file_id=photo)] if photo else None,
            chat=types.SimpleNamespace(id=chat_id, type=chat_type), message_thread_id=thread_id,
            reply_text=reply_text, reply_photo=reply_photo,
        )

    def update(self, message=None, query=None):
        return types.SimpleNamespace(
            message=message, callback_query=query, effective_message=message or (query and query.message),
            effective_user=types.SimpleNamespace(id=999),
        )

    async def press(self, data, message=None):
        message = message or self.message()
        out = self.out

        async def answer(*a, **k):
            return None

        async def edit(t, reply_markup=None, **k):
            out.append(("edit", t, reply_markup))

        query = types.SimpleNamespace(data=data, message=message, answer=answer, edit_message_text=edit)
        await bot.sp_callback(self.update(query=query), self.context)

    def buttons(self):
        found = []
        for kind, _, markup in self.out:
            if markup is not None and hasattr(markup, "inline_keyboard"):
                for row in markup.inline_keyboard:
                    for b in row:
                        found.append((b.text, b.callback_data))
        return found


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class Units(unittest.TestCase):
    def test_parse_post_and_sources(self):
        title, body = school_posts.parse_post(AI_ANSWER)
        self.assertEqual(title, "Богемский гранат: как его узнать")
        self.assertTrue(body.startswith("Гранат — главный камень"))
        sources = school_posts.sources_for(AI_ANSWER, FRAGMENTS, library_article.source_line)
        # Только существующие номера, без DOC-кода и номера фрагмента; [9] выдуман — его нет.
        self.assertEqual(sources, ["Garnet Jewelry — PDF-стр. 12 (в книге стр. 10)", "Victorian Jewels — PDF-стр. 40"])

    def test_render_hides_citations_and_makes_headings(self):
        _, body = school_posts.parse_post(AI_ANSWER)
        html_body = school_posts.render_body(body)
        self.assertNotIn("[1]", html_body)
        self.assertIn("<b>Откуда он</b>", html_body)
        self.assertIn("▫️ Мелкие камни", html_body)

    def test_long_article_is_split(self):
        article = {"title": "Т", "theme": "т", "body": "\n\n".join(["абзац " * 120] * 12), "sources": ["Книга — стр. 1"]}
        parts = school_posts.render_parts(article)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= school_posts.MAX_MESSAGE for p in parts))
        self.assertIn("OH MY GOD VINTAGE School", parts[-1])

    def test_links_to_private_group(self):
        self.assertEqual(school_posts.message_link(GROUP, 55, 7), "https://t.me/c/1234567890/7/55")
        self.assertEqual(school_posts.message_link(GROUP, 55), "https://t.me/c/1234567890/55")


class Flow(unittest.TestCase):
    def setUp(self):
        helpers.fresh_database(bot)
        school_posts.init_tables(bot.DB_PATH)
        self.chat = Chat()

    def make_article(self):
        with mock.patch.object(library_article, "get_job", return_value=JOB), \
                mock.patch.object(library_article, "call_ai", return_value=AI_ANSWER) as ai:
            run(self.chat.press("sp:mk:7"))
        self.assertEqual(ai.call_args[0][2], "post")  # учитывается в бюджете досье как вызов «post»
        return school_posts.drafts(bot.DB_PATH)[0]

    def test_whole_path_draft_topic_publish_toc_update(self):
        article = self.make_article()
        self.assertEqual(article["title"], "Богемский гранат: как его узнать")
        self.assertIn(("✅ Опубликовать", f"sp:pb:{article['id']}"), self.chat.buttons())

        # Без темы не публикуем.
        run(self.chat.press(f"sp:pb:{article['id']}"))
        self.assertIn("Сначала выбери тему", self.chat.out[-2][1])

        # Тема выбрана, но Люся ещё не знает, где она в группе.
        run(self.chat.press(f"sp:st:{article['id']}:lab"))
        run(self.chat.press(f"sp:pb:{article['id']}"))
        self.assertIn("/tema", self.chat.out[-2][1])

        # Кристина пишет /tema в теме группы и выбирает «Лаборатория материалов».
        in_group = self.chat.message(chat_type="supergroup", chat_id=GROUP, thread_id=7)
        run(self.chat.press("sp:tm:lab", message=in_group))
        self.assertEqual(school_posts.get_topic(bot.DB_PATH, "lab")["thread_id"], 7)

        run(self.chat.press(f"sp:pb:{article['id']}"))
        tg = self.chat.tg
        posted = [s for s in tg.sent if s[0] == "text"]
        self.assertEqual(posted[0][1:3], (GROUP, 7))           # в нужную тему
        self.assertIn("Богемский гранат", posted[0][3])
        self.assertIn("По книгам библиотеки школы", posted[0][3])
        toc = posted[-1]
        self.assertIn("Статьи темы «Лаборатория материалов»", toc[3])
        self.assertIn(f"https://t.me/c/1234567890/7/{posted[0][4]}", toc[3])
        self.assertEqual(tg.pinned, [toc[4]])
        self.assertEqual(school_posts.get_article(bot.DB_PATH, article["id"])["status"], "published")

        # Правка после публикации: те же сообщения меняются, новых постов нет, оглавление правится.
        sent_before = len(tg.sent)
        run(self.chat.press(f"sp:ad:{article['id']}"))
        reply = self.chat.message(text="Мой совет: смотри на огранку розой.")
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.sp_wait_input(self.chat.update(message=reply), self.chat.context))
        run(self.chat.press(f"sp:pb:{article['id']}"))
        self.assertEqual(len(tg.sent), sent_before)
        self.assertTrue(any("От Кристины" in t for _, t in tg.edited))

    def test_replace_text_keeps_her_formatting_and_drops_copied_sources(self):
        article = self.make_article()
        run(self.chat.press(f"sp:rt:{article['id']}"))
        copied = (
            "<b>Гранат без тайн</b>\nМой текст с <i>курсивом</i>.\n\n"
            "<i>📖 По книгам библиотеки школы:</i>\n<i>· Garnet Jewelry — стр. 10</i>\n\n✦ OH MY GOD VINTAGE School"
        )
        msg = self.chat.message(text="Гранат без тайн\nМой текст", text_html=copied)
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.sp_wait_input(self.chat.update(message=msg), self.chat.context))
        saved = school_posts.get_article(bot.DB_PATH, article["id"])
        self.assertEqual(saved["title"], "Гранат без тайн")
        parts = bot.sp_parts(saved)
        self.assertIn("<i>курсивом</i>", parts[0])
        self.assertEqual(parts[0].count("По книгам библиотеки школы"), 1)  # источники не задвоились

    def test_cover_and_unrelated_messages(self):
        article = self.make_article()
        # Без ожидания сообщение уходит обычной Люсе (обработчик ничего не делает).
        run(bot.sp_wait_input(self.chat.update(message=self.chat.message(text="привет")), self.chat.context))
        run(self.chat.press(f"sp:cv:{article['id']}"))
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.sp_wait_input(self.chat.update(message=self.chat.message(photo="PHOTO-1")), self.chat.context))
        self.assertEqual(school_posts.get_article(bot.DB_PATH, article["id"])["cover_file_id"], "PHOTO-1")

    def test_delete_draft(self):
        article = self.make_article()
        run(self.chat.press(f"sp:dl:{article['id']}"))
        self.assertIsNone(school_posts.get_article(bot.DB_PATH, article["id"]))


if __name__ == "__main__":
    unittest.main()


class InGroup(unittest.TestCase):
    """Люся в группе: отвечает только когда её позвали, со ссылкой на статью школы."""

    def setUp(self):
        helpers.fresh_database(bot)
        school_posts.init_tables(bot.DB_PATH)
        lucy = __import__("lucy_ai")
        lucy.init_ai_usage_table(bot.DB_PATH)
        self.out = []
        out = self.out

        async def get_me():
            return types.SimpleNamespace(id=4242, username="LucyVintageBot")

        async def noop(*a, **k):
            return None

        self.context = types.SimpleNamespace(user_data={}, bot=types.SimpleNamespace(
            id=4242, username="LucyVintageBot", get_me=get_me, send_chat_action=noop))

        def msg(text, reply_to=None, user=1):
            async def reply_text(t, **k):
                out.append(t)
            return types.SimpleNamespace(
                text=text, chat=types.SimpleNamespace(id=GROUP), message_thread_id=7,
                reply_to_message=reply_to, reply_text=reply_text,
            )
        self.msg = msg

    def say(self, message, user=1):
        update = types.SimpleNamespace(message=message, effective_user=types.SimpleNamespace(id=user))
        with mock.patch.dict("os.environ", {"LUCY_AI_ENABLED": "1"}), \
                mock.patch("lucy_ai.ask_openai", return_value="Богемский гранат — тёмно-красный.") as ai:
            run(bot.group_message(update, self.context))
        return ai

    def test_silent_unless_called(self):
        ai = self.say(self.msg("девочки, смотрите какой гранат"))
        self.assertFalse(ai.called)
        self.assertEqual(self.out, [])

    def test_answers_mention_with_school_article(self):
        school_posts.save_topic(bot.DB_PATH, "lab", GROUP, 7)
        aid = school_posts.create_article(bot.DB_PATH, 1, "гранат", "Богемский гранат", "Гранат из Чехии", [])
        school_posts.update_article(bot.DB_PATH, aid, topic_slug="lab", status="published", messages=[555])
        ai = self.say(self.msg("@LucyVintageBot как узнать богемский гранат?"))
        self.assertTrue(ai.called)
        question = ai.call_args[0][0][-1]["content"]
        self.assertNotIn("@LucyVintageBot", question)
        self.assertIn("тёмно-красный", self.out[-1])
        self.assertIn("https://t.me/c/1234567890/7/555", self.out[-1])

    def test_reply_to_lucy_counts(self):
        lucy_msg = types.SimpleNamespace(from_user=types.SimpleNamespace(id=4242), forum_topic_created=None)
        ai = self.say(self.msg("а янтарь?", reply_to=lucy_msg))
        self.assertTrue(ai.called)

    def test_own_article_from_statya(self):
        chat = Chat()
        run(chat.press("sp:nw:0"))
        art = school_posts.drafts(bot.DB_PATH)[0]
        msg = chat.message(text="Стразы\nТекст", text_html="<b>Почему стразы горят</b>\nТекст про <i>фольгу</i>")
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.sp_wait_input(chat.update(message=msg), chat.context))
        # Telegram порезал длинный текст — второй кусок приклеивается.
        msg2 = chat.message(text="Вторая часть", text_html="Вторая часть про <b>AB</b>")
        with self.assertRaises(bot.ApplicationHandlerStop):
            run(bot.sp_wait_input(chat.update(message=msg2), chat.context))
        run(chat.press(f"sp:dn:{art['id']}"))
        self.assertNotIn(bot.SP_WAIT, chat.context.user_data)
        saved = school_posts.get_article(bot.DB_PATH, art["id"])
        self.assertEqual(saved["title"], "Почему стразы горят")
        self.assertIn("Вторая часть про <b>AB</b>", saved["body"])
        self.assertIn("<i>фольгу</i>", bot.sp_parts(saved)[0])


class ByName(InGroup):
    def test_name_at_start(self):
        ai = self.say(self.msg("Люся, как отличить бакелит?"))
        self.assertTrue(ai.called)
        self.assertEqual(ai.call_args[0][0][-1]["content"], "как отличить бакелит?")

    def test_variants(self):
        for t in ("люсь что такое гагат", "Люсенька! а стразы?", "ЛЮСЯ подскажи"):
            self.assertIsNotNone(bot.strip_lucy_name(t), t)

    def test_name_in_middle_is_silent(self):
        for t in ("а Люся сегодня молодец", "Люсьена прислала брошь", "Люсячка"):
            self.assertIsNone(bot.strip_lucy_name(t), t)
        ai = self.say(self.msg("а Люся сегодня молодец"))
        self.assertFalse(ai.called)


class LongTextIsArticle(unittest.TestCase):
    """Кристина вставила статью без кнопки — Люся не отвечает ИИ, а сохраняет черновик."""

    def test_long_text_becomes_draft(self):
        helpers.fresh_database(bot)
        school_posts.init_tables(bot.DB_PATH)
        chat = Chat()
        body = "Почему стразы горят\n" + "Фольга под камнем. " * 80
        msg = chat.message(text=body)
        with mock.patch.object(bot, "is_admin", return_value=True):
            with self.assertRaises(bot.ApplicationHandlerStop):
                run(bot.sp_wait_input(chat.update(message=msg), chat.context))
        art = school_posts.drafts(bot.DB_PATH)[0]
        self.assertEqual(art["title"], "Почему стразы горят")
        self.assertEqual(chat.context.user_data[bot.SP_WAIT], ("own", art["id"]))

    def test_short_question_goes_on(self):
        helpers.fresh_database(bot)
        school_posts.init_tables(bot.DB_PATH)
        chat = Chat()
        with mock.patch.object(bot, "is_admin", return_value=True):
            run(bot.sp_wait_input(chat.update(message=chat.message(text="что такое бакелит?")), chat.context))
        self.assertEqual(school_posts.drafts(bot.DB_PATH), [])
