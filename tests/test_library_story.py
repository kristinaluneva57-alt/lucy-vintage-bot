"""Статья по теме из книг библиотеки с картинками (library_story).

OpenAI подменяется поддельным клиентом, рендер страниц — заглушкой.
Настоящих запросов нет. Запуск: python -m unittest discover -s tests
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from helpers import FakeChat, buttons, load_bot, photos, texts

bot = load_bot()
import library_db  # noqa: E402
import library_importer  # noqa: E402
import library_ai  # noqa: E402
import library_story  # noqa: E402
import lucy_ai  # noqa: E402
import visual_index  # noqa: E402
import web_search  # noqa: E402
import research_engine  # noqa: E402

from test_library_ai import FakeOpenAI, PAGES, JET_TITLE  # noqa: E402

openai_module = sys.modules["openai"]

ARTICLE = (
    "# Гагат: чёрный камень Уитби\n"
    "Гагат — окаменевшая древесина [1].\n\n"
    "## Откуда он\n"
    "Лучший гагат добывали у Уитби [2]. Ссылка на несуществующий фрагмент [77].\n\n"
    "## Траурные украшения\n"
    "**Броши** и медальоны носили в трауре [3]."
)


def tiny_jpeg(color=(255, 255, 255)):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 600), color)
    ImageDraw.Draw(image).rectangle((100, 150, 300, 450), fill=(20, 20, 20))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


class StoryTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        path = os.path.join(tempfile.mkdtemp(prefix="lucy_story_"), "test.db")
        self.path = path
        bot.DB_PATH = path
        library_db.DB_PATH = path
        bot.init_db()
        library_db.init_library_db()
        lucy_ai.init_ai_usage_table(path)
        library_ai.init_tables(path)
        library_story.init_tables(path)
        visual_index.init_tables(path)

        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO library_documents (id, document_code, title, storage_key, language) "
            "VALUES (2, 'DOC-000002', ?, 'books/BOOK-000001/Book 1.pdf', 'eng')",
            (JET_TITLE,),
        )
        for pdf_page, printed, text in PAGES:
            conn.execute(
                "INSERT INTO library_pages (document_id, pdf_page, printed_page, "
                "original_text, cleaned_text, extraction_status) VALUES (2, ?, ?, ?, ?, 'extracted')",
                (pdf_page, printed, text, library_importer.clean_ocr_text(text)),
            )
        # Страница 19 (Уитби) — иллюстрированная, с подписью про гагат.
        conn.execute(
            "INSERT INTO visual_page_index (document_id, pdf_page, document_code, captions, "
            "illustration_score) VALUES (2, 19, 'DOC-000002', ?, 0.8)",
            (json.dumps(["Fig. 4 Jet brooch, Whitby, about 1870"]),),
        )
        # Страница 5 — почти без картинок.
        conn.execute(
            "INSERT INTO visual_page_index (document_id, pdf_page, document_code, captions, "
            "illustration_score) VALUES (2, 5, 'DOC-000002', '[]', 0.1)"
        )
        conn.commit()
        conn.close()

        self.env = mock.patch.dict(os.environ, {
            "OPENAI_API_KEY": "sk-test",
            "LUCY_LIBRARY_AI_ENABLED": "1",
            "LUCY_LIBRARY_AI_DAILY_LIMIT": "10",
            "LUCY_LIBRARY_AI_DAILY_TOTAL": "200",
            "LUCY_AI_ENABLED": "0",
        })
        self.env.start()
        self.openai_patch = mock.patch.object(openai_module, "OpenAI", FakeOpenAI, create=True)
        self.openai_patch.start()
        FakeOpenAI.calls = []
        FakeOpenAI.error = None
        FakeOpenAI.answer = ARTICLE
        self.render_patch = mock.patch("page_images.page_image", return_value=tiny_jpeg())
        self.render = self.render_patch.start()
        self.chat = FakeChat(bot, 1)

    def tearDown(self):
        self.render_patch.stop()
        self.openai_patch.stop()
        self.env.stop()

    async def ask_topic(self, topic):
        await self.chat.say(bot.BTN_SEARCH)
        return await self.chat.say(topic)

    # --- маршрутизация ---

    def test_topic_or_question(self):
        self.assertTrue(bot.is_topic_request("гранат"))
        self.assertTrue(bot.is_topic_request("чешское стекло"))
        self.assertTrue(bot.is_topic_request("Trifari"))
        self.assertFalse(bot.is_topic_request("как отличить бакелит?"))
        self.assertFalse(bot.is_topic_request("что такое гагат"))
        self.assertFalse(bot.is_topic_request("очень длинный запрос из многих разных слов про брошь"))

    # --- статья ---

    async def test_topic_builds_article_with_sources_and_picture(self):
        replies = await self.ask_topic("гагат")
        all_text = "\n".join(t for t in texts(replies) if t)
        self.assertEqual(len(FakeOpenAI.calls), 1)
        self.assertIn("<b>📖 ГАГАТ: ЧЁРНЫЙ КАМЕНЬ УИТБИ</b>", all_text)
        self.assertIn("<b>Откуда он</b>", all_text)
        self.assertNotIn("[77]", all_text)          # выдуманная ссылка убрана
        self.assertNotIn("**", all_text)            # Markdown убран
        self.assertIn("📚 Источники", all_text)
        self.assertIn(JET_TITLE, all_text)
        # Картинка — иллюстрированная страница с подписью, без ИИ.
        self.assertEqual(len(photos(replies)), 1)
        captions = [c for kind, _, c in replies if kind == "photo"]
        self.assertIn("PDF-стр. 19", captions[0])
        self.assertIn("Jet brooch", captions[0])
        # Только содержательные страницы: оглавление/указатель в ИИ не ушли.
        prompt = FakeOpenAI.calls[0]["messages"][1]["content"]
        self.assertNotIn("CONTENTS", prompt)
        self.assertNotIn("ISBN", prompt)

    async def test_same_topic_second_time_is_free(self):
        await self.ask_topic("гагат")
        replies = await self.ask_topic("Гагат")
        self.assertEqual(len(FakeOpenAI.calls), 1)   # из кэша
        self.assertTrue(any("ГАГАТ" in (t or "") for t in texts(replies)))
        self.assertEqual(library_ai.used_today(self.path, 1), 1)

    async def test_new_pages_rebuild_article(self):
        await self.ask_topic("гагат")
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT INTO library_pages (document_id, pdf_page, original_text, cleaned_text) "
            "VALUES (2, 40, 'jet beads necklace', 'jet beads necklace')"
        )
        conn.commit()
        conn.close()
        await self.ask_topic("гагат")
        self.assertEqual(len(FakeOpenAI.calls), 2)

    async def test_no_info_answer_is_honest(self):
        FakeOpenAI.answer = "НЕТ СВЕДЕНИЙ"
        replies = await self.ask_topic("гагат")
        self.assertTrue(any("мало сведений" in (t or "") for t in texts(replies)))
        self.assertEqual(photos(replies), [])

    async def test_daily_limit_falls_back_to_fragments(self):
        os.environ["LUCY_LIBRARY_AI_DAILY_LIMIT"] = "0"
        replies = await self.ask_topic("гагат")
        self.assertEqual(FakeOpenAI.calls, [])
        self.assertTrue(any("закончились" in (t or "") for t in texts(replies)))

    async def test_ai_failure_falls_back(self):
        FakeOpenAI.error = openai_module.OpenAIError("boom")
        replies = await self.ask_topic("гагат")
        self.assertTrue(any("не получилось" in (t or "") for t in texts(replies)))

    async def test_question_gets_short_answer_with_story_button(self):
        FakeOpenAI.answer = "Гагат — окаменевшая древесина [1]."
        replies = await self.ask_topic("что такое гагат")
        self.assertIn("story:new", buttons(replies))
        FakeOpenAI.answer = ARTICLE
        replies = await self.chat.press("story:new")
        self.assertTrue(any("ГАГАТ" in (t or "") for t in texts(replies)))

    async def test_original_button(self):
        await self.ask_topic("гагат")
        conn = sqlite3.connect(self.path)
        story_id = conn.execute("SELECT MAX(id) FROM library_stories").fetchone()[0]
        conn.close()
        with mock.patch.object(bot, "story_callback", bot.story_callback):
            from types import SimpleNamespace

            sent = []

            async def reply_text(text, **kwargs):
                sent.append(text)

            async def answer():
                return None

            query = SimpleNamespace(
                data=f"story:orig:{story_id}:1", answer=answer,
                message=SimpleNamespace(reply_text=reply_text),
            )
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=1))
            await bot.story_callback(update, self.chat.context)
        self.assertIn("ОРИГИНАЛ [1]", sent[0])

    def test_trim_margins_crops_white(self):
        from PIL import Image

        out = library_story.trim_margins(tiny_jpeg())
        image = Image.open(io.BytesIO(out))
        self.assertLess(image.width, 400)
        self.assertLess(image.height, 600)

    def test_split_long_article(self):
        text = "\n\n".join(["абзац " * 200] * 6)
        parts = library_story.split_messages(text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= library_story.TELEGRAM_LIMIT for p in parts))

    def test_html_is_escaped(self):
        result = library_story.to_html("# <script>\nтекст <b>")
        self.assertIn("&lt;SCRIPT&gt;", result)
        self.assertIn("текст &lt;b&gt;", result)


class FrozenWebSearchTest(unittest.TestCase):
    def test_web_search_off_by_default_even_with_key(self):
        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "tvly-x"}):
            os.environ.pop("LUCY_WEB_SEARCH", None)
            self.assertFalse(web_search.configured_provider().connected)

    def test_research_model_default_is_cheap(self):
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("LUCY_RESEARCH_MODEL", None)
            self.assertEqual(research_engine.research_model(), "gpt-4.1-mini")


if __name__ == "__main__":
    unittest.main()
